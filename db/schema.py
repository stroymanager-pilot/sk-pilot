# SK-pilot (СК-пилот) — система ежедневных сводок строительного контроля.
# Автор: Vladislav Nikonenko (идея и разработка). © 2026. Версия 1.5.

import sqlite3, os, re

# ─────────────────────────────────────────────────────────────────────────
# ВЫБОР СУБД
#
# SK_DB_TYPE=postgres  → PostgreSQL (psycopg2)
# иначе                → SQLite, поведение полностью прежнее
#
# Переключение и откат делаются переменной окружения, без правки кода.
# ─────────────────────────────────────────────────────────────────────────
DB_TYPE = (os.environ.get('SK_DB_TYPE') or 'sqlite').strip().lower()
IS_POSTGRES = DB_TYPE in ('postgres', 'postgresql', 'pg')

# Путь к базе SQLite. Приоритет — переменная окружения SK_DB_PATH: она
# позволяет автотестам работать на отдельной базе и не иметь доступа к боевой.
# Без неё поведение прежнее: /var/data на Render, иначе папка db/.
_RENDER_DISK = '/var/data'
if os.environ.get('SK_DB_PATH'):
    DB_PATH = os.environ['SK_DB_PATH']
elif os.path.isdir(_RENDER_DISK):
    DB_PATH = os.path.join(_RENDER_DISK, 'pilot.db')
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), 'pilot.db')

# Целевая схема PostgreSQL — единственный источник правды по структуре
POSTGRES_DDL = os.path.join(os.path.dirname(__file__), 'schema_postgres.sql')


def _pg_dsn():
    """Параметры подключения к PostgreSQL из окружения."""
    if os.environ.get('SK_PG_DSN'):
        return os.environ['SK_PG_DSN']
    return (
        f"host={os.environ.get('SK_PG_HOST', 'localhost')} "
        f"port={os.environ.get('SK_PG_PORT', '5432')} "
        f"dbname={os.environ.get('SK_PG_DB', 'sk_pilot')} "
        f"user={os.environ.get('SK_PG_USER', 'sk')} "
        f"password={os.environ.get('SK_PG_PASSWORD', '')}"
    )


# ── Перевод SQL из диалекта SQLite в диалект PostgreSQL ──────────────────
_GROUP_CONCAT = re.compile(r'\bGROUP_CONCAT\s*\(', re.IGNORECASE)


def translate_sql(sql):
    """Готовит запрос, написанный под SQLite, к исполнению в PostgreSQL.

    Порядок операций важен: сначала экранируем литеральные '%' (иначе
    psycopg2 примет их за подстановку — например в "LIKE '%Окла%'"),
    и только потом меняем плейсхолдеры '?' на '%s'.

    В запросах приложения литеральных '?' нет — это проверено; единственный
    знак вопроса вне SQL находится в регулярном выражении safe_name(),
    которое в execute() не передаётся.
    """
    sql = sql.replace('%', '%%')
    sql = sql.replace('?', '%s')
    # GROUP_CONCAT(x, ', ') → string_agg(x, ', ')
    sql = _GROUP_CONCAT.sub('string_agg(', sql)
    return sql


def _sqlite_like_cursor():
    """Курсор, чьи строки ведут себя как sqlite3.Row.

    RealDictRow — словарь и доступа по числовому индексу не имеет, а
    sqlite3.Row поддерживает и row['имя'], и row[0]. Чтобы обёртка была
    полноценной заменой, добавляем второе: иначе код вида
    .fetchone()[0] молча ломается только на PostgreSQL.
    """
    from psycopg2.extras import RealDictCursor, RealDictRow

    class Row(RealDictRow):
        def __getitem__(self, key):
            if isinstance(key, int):
                return list(self.values())[key]
            return super().__getitem__(key)

    class Cursor(RealDictCursor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.row_factory = Row

    return Cursor


class PgConnection:
    """Обёртка над psycopg2, повторяющая интерфейс sqlite3.Connection.

    Благодаря ей все вызовы вида db.execute(sql, params).fetchone() в app.py
    работают без изменений: курсор заводится внутри, строки возвращаются
    с доступом и по имени поля, и по индексу.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor(cursor_factory=_sqlite_like_cursor())
        cur.execute(translate_sql(sql), params)
        return cur

    def executescript(self, script):
        """В PostgreSQL используется только при первичном создании схемы.
        Пути приложения, которые звали executescript (auto_migrate,
        /api/migrate, /api/migrate_v2), под PostgreSQL не исполняются."""
        cur = self._raw.cursor()
        cur.execute(script)
        cur.close()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    @property
    def raw(self):
        return self._raw


def get_db():
    if IS_POSTGRES:
        import psycopg2
        conn = psycopg2.connect(_pg_dsn())
        return PgConnection(conn)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    if IS_POSTGRES:
        _init_postgres()
        return
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
    -- ─────────────────────────────────────────────
    -- СПРАВОЧНИКИ (настраиваются администратором)
    -- ─────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS objects (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tj_object_id    TEXT,           -- ID объекта в TeamJect (для будущей интеграции)
        name            TEXT NOT NULL,
        address         TEXT,
        client_name     TEXT,           -- наименование заказчика
        contract_number TEXT,           -- номер договора
        project_id      INTEGER,        -- ссылка на проект
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id   INTEGER NOT NULL REFERENCES objects(id),
        name        TEXT NOT NULL,      -- "Корпус 5.3.1", "Блок 2", "Секция А" и т.д.
        is_active   INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS contractors (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id       INTEGER NOT NULL REFERENCES objects(id),
        name            TEXT NOT NULL,  -- "ООО «Пинстрой»"
        work_type       TEXT,           -- вид работ по умолчанию
        partner_id      INTEGER,        -- ссылка на partners.id (без жёсткого FK)
        hidden_manually INTEGER NOT NULL DEFAULT 0,  -- 1 = скрыт администратором вручную
        is_active       INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tj_user_id  TEXT,               -- ID пользователя в TeamJect
        full_name   TEXT NOT NULL,
        email       TEXT UNIQUE NOT NULL,
        role        TEXT DEFAULT 'engineer',  -- engineer / senior / admin
        password_hash TEXT,
        organization_id INTEGER,        -- ссылка на organizations.id (без жёсткого FK)
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS object_users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id   INTEGER NOT NULL REFERENCES objects(id),
        user_id     INTEGER NOT NULL REFERENCES users(id),
        date_from   TEXT,
        date_to     TEXT,
        UNIQUE(object_id, user_id)
    );

    -- ─────────────────────────────────────────────
    -- ЕЖЕДНЕВНАЯ СВОДКА
    -- ─────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS daily_reports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id       INTEGER NOT NULL REFERENCES objects(id),
        user_id         INTEGER NOT NULL REFERENCES users(id),
        report_date     TEXT NOT NULL,   -- YYYY-MM-DD, обязательно в день работ
        status          TEXT DEFAULT 'draft',  -- draft / submitted
        created_at      TEXT DEFAULT (datetime('now')),
        submitted_at    TEXT,
        UNIQUE(object_id, user_id, report_date)
    );

    -- ─────────────────────────────────────────────
    -- РАЗДЕЛЫ СВОДКИ
    -- ─────────────────────────────────────────────

    -- Численность персонала (по корпусам)
    CREATE TABLE IF NOT EXISTS personnel_entries (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id           INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
        contractor_id       INTEGER NOT NULL,  -- нет FK: подрядчик может быть удалён, не ломаем сохранение
        section_id          INTEGER,           -- нет FK: допускает личные участки (user_sections)
        headcount           INTEGER DEFAULT 0,
        work_description    TEXT    -- фактические работы за день
    );

    -- Входной контроль
    CREATE TABLE IF NOT EXISTS input_control (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
        material_name   TEXT,
        quantity        TEXT,
        document_name   TEXT,   -- наименование сопроводительного документа
        deviation_note  TEXT,   -- отметка об отклонениях / дефектах
        engineer_id     INTEGER REFERENCES users(id)
    );

    -- Операционный контроль (схемы)
    CREATE TABLE IF NOT EXISTS operational_control (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id               INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
        section_id              INTEGER,  -- нет FK: допускает личные участки (user_sections)
        work_stage              TEXT,   -- этап работ
        controlled_operations   TEXT,   -- контролируемые операции
        control_method          TEXT,   -- метод и объём контроля
        engineer_id             INTEGER REFERENCES users(id)
    );

    -- Устные замечания (хранятся в пилоте, без TeamJect)
    CREATE TABLE IF NOT EXISTS verbal_remarks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
        section_id      INTEGER,  -- нет FK: допускает личные участки (user_sections)
        description     TEXT NOT NULL,
        deadline        TEXT,           -- срок устранения (YYYY-MM-DD)
        status          TEXT DEFAULT 'open',  -- open / closed
        issued_by       INTEGER REFERENCES users(id),
        closed_at       TEXT,           -- дата фактического закрытия
        closed_note     TEXT            -- примечание при закрытии
    );

    -- Предписания — только ссылка на TeamJect, не полный документ
    CREATE TABLE IF NOT EXISTS prescriptions_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id           INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
        tj_prescription_id  TEXT,       -- ID предписания в TeamJect
        number              TEXT,       -- номер предписания ("№ 26")
        issue_date          TEXT,
        section_id          INTEGER,    -- нет FK: допускает личные участки (user_sections)
        deadline            TEXT,
        status              TEXT        -- статус из TeamJect (вносится вручную)
    );

    -- Совещания
    CREATE TABLE IF NOT EXISTS meetings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
        location        TEXT,
        time            TEXT,
        participants    TEXT,   -- список участников
        agenda          TEXT,   -- тематика и вопросы
        engineer_id     INTEGER REFERENCES users(id)
    );

    -- Фотофиксация
    CREATE TABLE IF NOT EXISTS photos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id       INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
        file_path       TEXT NOT NULL,
        caption         TEXT,           -- подпись к фото
        sort_order      INTEGER DEFAULT 0,
        remark_id       INTEGER REFERENCES verbal_remarks(id),  -- привязка к замечанию (необязательно)
        uploaded_at     TEXT DEFAULT (datetime('now'))
    );

    -- УЧАСТКИ (персональные разделы инженера по объекту)
    CREATE TABLE IF NOT EXISTS user_sections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
            object_id   INTEGER NOT NULL REFERENCES objects(id),
                user_id     INTEGER NOT NULL REFERENCES users(id),
                    name        TEXT NOT NULL,
                        is_active   INTEGER DEFAULT 1
                        );

    -- ─────────────────────────────────────────────
    -- ПРОЕКТЫ и ПАРТНЁРЫ
    -- ─────────────────────────────────────────────

    -- Организации (SaaS-изоляция). Пока одна — 'Стройменеджер'.
    CREATE TABLE IF NOT EXISTS organizations (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        name                TEXT NOT NULL,
        subscription_until  TEXT,           -- NULL = без ограничения
        is_active           INTEGER NOT NULL DEFAULT 1,
        created_at          TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS projects (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        description     TEXT,
        tj_project_id   TEXT,
        organization_id INTEGER,        -- ссылка на organizations.id (без жёсткого FK)
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS partners (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        type            TEXT,
        address         TEXT,
        contact_name    TEXT,
        contact_role    TEXT,
        inn             TEXT,
        phone           TEXT,
        email           TEXT,
        notes           TEXT,
        organization_id INTEGER,        -- ссылка на organizations.id (без жёсткого FK)
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now'))
    );

    -- Связь партнёров с проектами (many-to-many).
    -- Без жёстких FK — логическая связь по id.
    CREATE TABLE IF NOT EXISTS partner_projects (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        partner_id INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        UNIQUE(partner_id, project_id)
    );

    -- ─────────────────────────────────────────────
    -- ИНДЕКСЫ для быстрых запросов
    -- ─────────────────────────────────────────────

    CREATE INDEX IF NOT EXISTS idx_reports_object_date ON daily_reports(object_id, report_date);
    CREATE INDEX IF NOT EXISTS idx_reports_user ON daily_reports(user_id);
    CREATE INDEX IF NOT EXISTS idx_personnel_report ON personnel_entries(report_id);
    CREATE INDEX IF NOT EXISTS idx_photos_report ON photos(report_id);
    CREATE INDEX IF NOT EXISTS idx_remarks_status ON verbal_remarks(status);
    """)

    conn.commit()
    conn.close()
    print(f"✅ База данных инициализирована: {DB_PATH}")


def _init_postgres():
    """Создаёт схему PostgreSQL из целевого DDL — один раз, идемпотентно.

    auto_migrate() под PostgreSQL не выполняется: накопленная история
    ALTER TABLE относилась к конкретному файлу pilot.db и после переезда
    неактуальна. Дальнейшие изменения схемы — отдельными миграциями.
    """
    if not os.path.exists(POSTGRES_DDL):
        raise RuntimeError(f'Не найден DDL схемы PostgreSQL: {POSTGRES_DDL}')
    with open(POSTGRES_DDL, encoding='utf-8') as f:
        ddl = f.read()
    conn = get_db()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    print('✅ Схема PostgreSQL готова (db/schema_postgres.sql)')


if __name__ == '__main__':
    init_db()

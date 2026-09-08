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


def pg_params():
    """Параметры подключения к PostgreSQL словарём.

    Единственный источник правды: им пользуются и psycopg2, и pg_dump
    при выгрузке резервной копии. Если задан SK_PG_DSN — разбираем его,
    иначе собираем из отдельных переменных.
    """
    if os.environ.get('SK_PG_DSN'):
        from psycopg2.extensions import parse_dsn
        d = parse_dsn(os.environ['SK_PG_DSN'])
        return {
            'host': d.get('host', 'localhost'),
            'port': str(d.get('port', '5432')),
            'dbname': d.get('dbname', ''),
            'user': d.get('user', ''),
            'password': d.get('password', ''),
        }
    return {
        'host': os.environ.get('SK_PG_HOST', 'localhost'),
        'port': os.environ.get('SK_PG_PORT', '5432'),
        'dbname': os.environ.get('SK_PG_DB', 'sk_pilot'),
        'user': os.environ.get('SK_PG_USER', 'sk'),
        'password': os.environ.get('SK_PG_PASSWORD', ''),
    }


def _pg_dsn():
    """Строка подключения для psycopg2."""
    if os.environ.get('SK_PG_DSN'):
        return os.environ['SK_PG_DSN']
    p = pg_params()
    return (f"host={p['host']} port={p['port']} dbname={p['dbname']} "
            f"user={p['user']} password={p['password']}")


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
        report_name               TEXT, -- реквизиты для ежемесячного отчёта (2-1)
        object_type               TEXT,
        contract_date             TEXT,
        contract_amendment        TEXT,
        client_partner_id         INTEGER,
        client_signatory          TEXT,
        client_signatory_role     TEXT,
        contractor_signatory      TEXT,
        contractor_signatory_role TEXT,
        project_id      INTEGER,        -- ссылка на проект
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id   INTEGER NOT NULL REFERENCES objects(id),
        name        TEXT NOT NULL,      -- "Корпус 5.3.1", "Блок 2", "Секция А" и т.д.
        section_type TEXT,               -- корпус / зона / общая площадка (2-1)
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
        status              TEXT,       -- статус из TeamJect (вносится вручную)
        issued_by_name      TEXT        -- кем выдано (2-1)
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
    -- ГЕНЕРАТОР ЕЖЕМЕСЯЧНОГО ОТЧЁТА (шаг 2-1)
    -- ─────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS report_groups (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,              -- внутреннее имя группы
        client_name  TEXT,                       -- заказчик
        report_title TEXT,                       -- наименование для заказчика
        is_active    INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT DEFAULT (datetime('now')),
        UNIQUE (name)
    );

    -- Уникальности по (группа, объект) намеренно НЕТ: объект может входить
    -- в группу несколько раз с разными периодами.
    CREATE TABLE IF NOT EXISTS report_group_objects (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_group_id INTEGER NOT NULL,
        object_id       INTEGER NOT NULL,
        date_from       TEXT,                    -- YYYY-MM-DD, NULL = с начала
        date_to         TEXT                     -- YYYY-MM-DD, NULL = бессрочно
    );

    -- ─────────────────────────────────────────────────────────────
    -- 2.2. ШАБЛОНЫ
    -- Набор блоков фиксирован, различается полнота, а не состав.
    -- ─────────────────────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS report_templates (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        UNIQUE (name)
    );

    CREATE TABLE IF NOT EXISTS report_template_blocks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        template_id   INTEGER NOT NULL,
        block_key     TEXT NOT NULL,             -- фиксированный перечень, см. ниже
        is_enabled    INTEGER NOT NULL DEFAULT 1,
        fill_mode     TEXT NOT NULL DEFAULT 'auto',   -- auto / manual / external
        sort_order    INTEGER NOT NULL DEFAULT 0,
        settings_json TEXT,
        UNIQUE (template_id, block_key)
    );

    -- ─────────────────────────────────────────────────────────────
    -- 2.3. ОТЧЁТ
    -- ─────────────────────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS monthly_reports (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        report_group_id INTEGER NOT NULL,
        year            INTEGER NOT NULL,
        month           INTEGER NOT NULL,
        template_id     INTEGER,
        status          TEXT NOT NULL DEFAULT 'черновик',
                        -- черновик / на_проверке / утверждён / выпущен
        created_by      INTEGER,
        submitted_at    TEXT,
        reviewed_by     INTEGER,
        approved_at     TEXT,
        version         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now')),
        -- version в ключе: версии допускаются, случайный дубль черновика — нет
        UNIQUE (report_group_id, year, month, version)
    );

    -- Один блок на ключ в отчёте: повторная сборка обновляет строку (upsert),
    -- поэтому ручные правки в content_json не задваиваются.
    CREATE TABLE IF NOT EXISTS monthly_report_blocks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id    INTEGER NOT NULL REFERENCES monthly_reports(id) ON DELETE CASCADE,
        block_key    TEXT NOT NULL,
        content_json TEXT,
        is_edited    INTEGER NOT NULL DEFAULT 0,
        updated_by   INTEGER,
        updated_at   TEXT,
        UNIQUE (report_id, block_key)
    );

    -- ─────────────────────────────────────────────────────────────
    -- 2.4. РЕЕСТРЫ РУЧНЫХ БЛОКОВ
    -- Ручной блок — список записей с жизненным циклом, а не текст.
    -- ─────────────────────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS author_supervision (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id  INTEGER NOT NULL,
        issue_date TEXT,
        author     TEXT,
        essence    TEXT,
        status     TEXT,
        closed_at  TEXT,
        note       TEXT
    );

    CREATE TABLE IF NOT EXISTS volume_changes (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id        INTEGER NOT NULL,
        contractor_id    INTEGER,
        contract_ref     TEXT,
        work_name        TEXT,
        volume_estimate  TEXT,
        volume_fact      TEXT,
        project_sheet    TEXT,
        note             TEXT,
        created_at       TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS schedule_notes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id  INTEGER NOT NULL,
        essence    TEXT,
        event_date TEXT,
        status     TEXT,
        closed_at  TEXT,
        note       TEXT
    );

    CREATE TABLE IF NOT EXISTS design_approvals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id       INTEGER NOT NULL,
        decision        TEXT,
        initiated_at    TEXT,
        issued_at       TEXT,
        approval_status TEXT,
        note            TEXT
    );

    -- ─────────────────────────────────────────────────────────────
    -- 2.5. ВРЕМЕННАЯ ТАБЛИЦА ПРОЦЕНТОВ
    -- До реализации ведомости объёмов (шаг 4) проценты вводятся вручную.
    -- Справочник видов работ в этой подзадаче НЕ наполняется.
    -- ─────────────────────────────────────────────────────────────

    CREATE TABLE IF NOT EXISTS work_types (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        object_type TEXT,
        group_name  TEXT,
        name        TEXT NOT NULL,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        is_active   INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS object_work_types (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id    INTEGER NOT NULL,
        work_type_id INTEGER NOT NULL,
        sort_order   INTEGER NOT NULL DEFAULT 0,
        is_active    INTEGER NOT NULL DEFAULT 1,
        UNIQUE (object_id, work_type_id)
    );

    CREATE TABLE IF NOT EXISTS monthly_progress (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id    INTEGER NOT NULL,
        section_id   INTEGER,
        work_type_id INTEGER,
        year         INTEGER NOT NULL,
        month        INTEGER NOT NULL,
        percent      INTEGER,
        note         TEXT,
        updated_by   INTEGER,
        updated_at   TEXT
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

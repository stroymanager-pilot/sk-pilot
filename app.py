# SK-pilot (СК-пилот) — система ежедневных сводок строительного контроля.
# Автор: Vladislav Nikonenko (идея и разработка). © 2026. Версия 1.5.

APP_VERSION = '1.5'

from flask import Flask, request, jsonify, send_from_directory, send_file, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager
import os, sys, hashlib, uuid, secrets
from datetime import datetime, date, timedelta

try:
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

sys.path.insert(0, os.path.dirname(__file__))
from db.schema import get_db, init_db, IS_POSTGRES

# ── ОБРАБОТКА ИЗОБРАЖЕНИЙ ────────────────────────────────────
_MAX_PX = 2000   # максимальная длинная сторона после ресайза
_JPEG_Q = 80     # качество JPEG при сохранении

def _process_image(src_path: str) -> str:
    """Конвертирует HEIC→JPEG и/или уменьшает слишком большие снимки.
    Возвращает путь к итоговому файлу (может совпадать с src_path для jpg/png).
    Если Pillow недоступен — возвращает src_path без изменений."""
    if not _PIL_AVAILABLE:
        return src_path
    try:
        img = Image.open(src_path)
        # Приводим к RGB — нужно для HEIC (RGBA/P/CMYK не сохраняются в JPEG)
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        # Ресайз если нужен
        w, h = img.size
        if max(w, h) > _MAX_PX:
            ratio = _MAX_PX / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        # Целевой путь всегда .jpg
        base = os.path.splitext(src_path)[0]
        dst_path = base + '.jpg'
        img.save(dst_path, 'JPEG', quality=_JPEG_Q, optimize=True)
        img.close()
        # Удаляем оригинал только если он отличается от результата (т.е. был HEIC/PNG/…)
        if src_path != dst_path:
            try:
                os.remove(src_path)
            except OSError:
                pass
        return dst_path
    except Exception:
        # Если обработка не удалась — оставляем исходный файл как есть
        return src_path
# ─────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder='static', static_url_path='')
init_db()  # Ensure DB exists on startup

# ─────────────────────────────────────────────────────────────
# РОЛИ
#   platform — оператор платформы (управление организациями; прав внутри
#              организации не имеет)
#   root     — главный админ организации
#   admin    — админ организации
#   senior   — главный инженер (при can_view_all=1 видит сводки всех объектов)
#   engineer — инженер (только свои объекты)
# ─────────────────────────────────────────────────────────────
ADMIN_ROLES = ('root', 'admin')          # административный доступ внутри организации
ALL_REPORTS_ROLES = ('root', 'admin')    # + senior при can_view_all=1

# Auto-migrate: создать таблицы и добавить новые колонки если их нет
def auto_migrate():
    # В PostgreSQL схема создаётся один раз из db/schema_postgres.sql.
    # Накопленная история ALTER TABLE относилась к конкретному файлу
    # pilot.db; разовые миграции данных (partner_projects, partner_id,
    # organizations, root-роль, привязки админов) уже отработали, их
    # результат содержится в перенесённых данных.
    if IS_POSTGRES:
        return
    db = get_db()
    try:
        # Создаём таблицы, которые могут отсутствовать в старых БД
        db.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            subscription_until TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, description TEXT,
            tj_project_id TEXT, is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, type TEXT, address TEXT,
            contact_name TEXT, contact_role TEXT, inn TEXT,
            phone TEXT, email TEXT, notes TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS acceptance_control (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
            section_id INTEGER,
            work_stage TEXT, controlled_operations TEXT,
            control_method TEXT, status TEXT DEFAULT '',
            deviation_note TEXT DEFAULT '',
            engineer_id INTEGER REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS ks2_check (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
            contractor_id INTEGER REFERENCES contractors(id),
            object_work TEXT, ks2_number TEXT,
            has_ks6a INTEGER DEFAULT 0, has_id INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS partner_projects (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            UNIQUE(partner_id, project_id)
        );
        """)
        db.commit()
        migrations = [
            ("ALTER TABLE operational_control ADD COLUMN status TEXT DEFAULT ''", "operational_control.status"),
            ("ALTER TABLE operational_control ADD COLUMN deviation_note TEXT DEFAULT ''", "operational_control.deviation_note"),
            ("ALTER TABLE acceptance_control ADD COLUMN status TEXT DEFAULT ''", "acceptance_control.status"),
            ("ALTER TABLE acceptance_control ADD COLUMN deviation_note TEXT DEFAULT ''", "acceptance_control.deviation_note"),
            ("ALTER TABLE input_control ADD COLUMN section_id INTEGER", "input_control.section_id"),
            ("ALTER TABLE meetings ADD COLUMN protocol_path TEXT", "meetings.protocol_path"),
            ("ALTER TABLE meetings ADD COLUMN protocol_name TEXT", "meetings.protocol_name"),
            ("ALTER TABLE objects ADD COLUMN project_id INTEGER", "objects.project_id"),
            ("ALTER TABLE ks2_check ADD COLUMN has_ks3 INTEGER DEFAULT 0", "ks2_check.has_ks3"),
            ("ALTER TABLE ks2_check ADD COLUMN ks3_number TEXT", "ks2_check.ks3_number"),
            ("ALTER TABLE ks2_check ADD COLUMN contractor_name TEXT", "ks2_check.contractor_name"),
            ("ALTER TABLE ks2_check ADD COLUMN engineer_id INTEGER", "ks2_check.engineer_id"),
            ("ALTER TABLE meetings ADD COLUMN protocol_path TEXT", "meetings.protocol_path2"),
            ("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1", "users.is_active"),
            ("ALTER TABLE users ADD COLUMN can_view_all INTEGER DEFAULT 0", "users.can_view_all"),
            ("ALTER TABLE partners ADD COLUMN project_id INTEGER", "partners.project_id"),
            ("ALTER TABLE partners ADD COLUMN work_type TEXT", "partners.work_type"),
            ("ALTER TABLE operational_control ADD COLUMN contractor_id INTEGER", "operational_control.contractor_id"),
            ("ALTER TABLE acceptance_control ADD COLUMN contractor_id INTEGER", "acceptance_control.contractor_id"),
            ("ALTER TABLE input_control ADD COLUMN contractor_id INTEGER", "input_control.contractor_id"),
            ("ALTER TABLE input_control ADD COLUMN status TEXT DEFAULT ''", "input_control.status"),
            ("ALTER TABLE contractors ADD COLUMN partner_id INTEGER", "contractors.partner_id"),
            ("ALTER TABLE contractors ADD COLUMN hidden_manually INTEGER NOT NULL DEFAULT 0", "contractors.hidden_manually"),
            ("ALTER TABLE users ADD COLUMN organization_id INTEGER", "users.organization_id"),
            ("ALTER TABLE projects ADD COLUMN organization_id INTEGER", "projects.organization_id"),
            ("ALTER TABLE partners ADD COLUMN organization_id INTEGER", "partners.organization_id"),
        ]
        for sql, label in migrations:
            try:
                db.execute(sql)
                db.commit()
            except Exception:
                pass  # колонка уже существует

        # Идемпотентная починка: убрать FK REFERENCES sections(id) у operational_control и acceptance_control.
        # Личные участки (user_sections) имеют свои id, которые не совпадают с sections → constraint failed.
        _fix_section_id_fk(db)
        _fix_personnel_contractor_fk(db)
        _migrate_partner_projects(db)
        _migrate_contractor_partner_id(db)
        _migrate_organizations(db)
        _migrate_root_role(db)
        _migrate_drop_admin_object_links(db)
    finally:
        db.close()


def _migrate_drop_admin_object_links(db):
    """ШАГ 4: администраторы видят всю организацию, привязка к объекту для них
    бессмысленна. Удаляет рудиментные строки object_users для ролей root/admin.
    Идемпотентна: повторный запуск не находит таких строк и ничего не делает."""
    cur = db.execute("""
        DELETE FROM object_users
        WHERE user_id IN (SELECT id FROM users WHERE role IN ('root','admin'))
    """)
    if cur.rowcount:
        db.commit()
        print(f"🧹 object_users: удалено {cur.rowcount} привязок админов к объектам")


def _migrate_root_role(db):
    """ШАГ 2а: перевод главного админа организации в роль 'root'.
    Идемпотентна: условие role='admin' не даёт повторно тронуть уже
    переведённую (или изменённую вручную) учётку."""
    cur = db.execute("UPDATE users SET role='root' WHERE id=3 AND role='admin'")
    if cur.rowcount:
        db.commit()
        print(f"👤 users.role: учётка #3 переведена в роль 'root'")


def _migrate_organizations(db):
    """ШАГ 1 SaaS: создаёт организацию по умолчанию и привязывает к ней
    существующие users / projects / partners.
    Идемпотентна: организация создаётся только если таблица пуста;
    organization_id проставляется только строкам, где он NULL.
    Логику приложения не затрагивает — колонка пока нигде не читается."""
    DEFAULT_ORG = 'Стройменеджер'

    cnt = db.execute("SELECT COUNT(*) AS c FROM organizations").fetchone()['c']
    if cnt == 0:
        db.execute(
            "INSERT INTO organizations (name, subscription_until, is_active) VALUES (?, NULL, 1)",
            (DEFAULT_ORG,)
        )
        db.commit()
        print(f"🏢 organizations: создана организация по умолчанию «{DEFAULT_ORG}»")

    org = db.execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
    if not org:
        return
    org_id = org['id']

    total = 0
    for table in ('users', 'projects', 'partners'):
        cur = db.execute(
            f"UPDATE {table} SET organization_id=? WHERE organization_id IS NULL",
            (org_id,)
        )
        if cur.rowcount:
            print(f"🏢 {table}.organization_id: проставлено {cur.rowcount} строк → org #{org_id}")
        total += cur.rowcount
    if total:
        db.commit()
    else:
        print(f"🏢 organization_id: все строки уже привязаны к организации #{org_id}")

def _fix_personnel_contractor_fk(db):
    """Убирает REFERENCES contractors(id) из personnel_entries.contractor_id.
    Сохраняет report_id ON DELETE CASCADE. Данные не теряются."""
    # Проверяем через sqlite_master (надёжнее PRAGMA на той же сессии)
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='personnel_entries'").fetchone()
    if row is None:
        return  # таблицы нет — ничего делать
    if 'REFERENCES contractors' not in (row['sql'] or ''):
        return  # FK уже снят — идемпотентно
    create_sql = (
        "CREATE TABLE personnel_entries_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,"
        "contractor_id INTEGER NOT NULL,"
        "section_id INTEGER,"
        "headcount INTEGER DEFAULT 0,"
        "work_description TEXT)"
    )
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("BEGIN")
    try:
        db.execute(create_sql)
        cols_old = [r['name'] for r in db.execute("PRAGMA table_info(personnel_entries)").fetchall()]
        cols_new = [r['name'] for r in db.execute("PRAGMA table_info(personnel_entries_new)").fetchall()]
        shared = [c for c in cols_old if c in cols_new]
        col_list = ', '.join(shared)
        db.execute(f"INSERT INTO personnel_entries_new ({col_list}) SELECT {col_list} FROM personnel_entries")
        db.execute("DROP TABLE personnel_entries")
        db.execute("ALTER TABLE personnel_entries_new RENAME TO personnel_entries")
        db.execute("CREATE INDEX IF NOT EXISTS idx_personnel_report ON personnel_entries(report_id)")
        db.execute("COMMIT")
        print("✅ _fix_personnel_contractor_fk: FK снят, данные сохранены")
    except Exception as e:
        db.execute("ROLLBACK")
        print(f"⚠️  _fix_personnel_contractor_fk failed: {e}")
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def _migrate_partner_projects(db):
    """Переносит привязки partners.project_id → partner_projects (ШАГ 1 many-to-many).
    Идемпотентна: ON CONFLICT DO NOTHING по UNIQUE(partner_id, project_id)."""
    rows = db.execute("SELECT id, name, project_id FROM partners").fetchall()
    with_proj = [r for r in rows if r['project_id'] is not None]
    without_proj = [r for r in rows if r['project_id'] is None]
    migrated = 0
    for r in with_proj:
        cur = db.execute(
            "INSERT INTO partner_projects (partner_id, project_id) VALUES (?,?) ON CONFLICT DO NOTHING",
            (r['id'], r['project_id'])
        )
        migrated += cur.rowcount
    if migrated or with_proj:
        db.commit()
    print(f"📋 partner_projects: перенесено {migrated} привязок из {len(with_proj)} партнёров с project_id")
    if without_proj:
        names = ', '.join(r['name'] for r in without_proj)
        print(f"⚠️  Партнёры без project_id ({len(without_proj)} шт.) — потребуется ручная привязка: {names}")
    else:
        print("✅ Все партнёры имели project_id — ручная привязка не нужна")


def _migrate_contractor_partner_id(db):
    """Заполняет contractors.partner_id по совпадению имён с partners.
    Идемпотентна: обновляет только строки с partner_id IS NULL."""
    # Строим словарь name → partner.id (берём первого активного с таким именем)
    partner_rows = db.execute("SELECT id, name FROM partners WHERE is_active=1").fetchall()
    name_to_pid = {}
    for r in partner_rows:
        if r['name'] not in name_to_pid:
            name_to_pid[r['name']] = r['id']
    # Подрядчики без partner_id
    nulls = db.execute("SELECT id, name FROM contractors WHERE partner_id IS NULL").fetchall()
    updated = 0
    for c in nulls:
        pid = name_to_pid.get(c['name'])
        if pid:
            db.execute("UPDATE contractors SET partner_id=? WHERE id=?", (pid, c['id']))
            updated += 1
    remaining = len(nulls) - updated
    if updated:
        db.commit()
    print(f"📋 contractors.partner_id: заполнено {updated} строк; осталось NULL (ручные): {remaining}")


def _fix_section_id_fk(db):
    """Перестраивает все таблицы инженера без жёстких FK на sections(id) или contractors(id).
    Личные участки (user_sections) и удалённые подрядчики не должны блокировать сохранение."""
    # Целевые DDL без FK на sections/contractors (report_id CASCADE — сохраняется везде)
    tables = {
        'operational_control': (
            "CREATE TABLE operational_control_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,"
            "section_id INTEGER,"
            "work_stage TEXT, controlled_operations TEXT, control_method TEXT,"
            "status TEXT DEFAULT '', deviation_note TEXT DEFAULT '',"
            "engineer_id INTEGER REFERENCES users(id),"
            "contractor_id INTEGER)"
        ),
        'acceptance_control': (
            "CREATE TABLE acceptance_control_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,"
            "section_id INTEGER,"
            "work_stage TEXT, controlled_operations TEXT, control_method TEXT,"
            "status TEXT DEFAULT '', deviation_note TEXT DEFAULT '',"
            "engineer_id INTEGER REFERENCES users(id),"
            "contractor_id INTEGER)"
        ),
        'prescriptions_log': (
            "CREATE TABLE prescriptions_log_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,"
            "tj_prescription_id TEXT, number TEXT, issue_date TEXT,"
            "section_id INTEGER, deadline TEXT, status TEXT)"
        ),
        'verbal_remarks': (
            "CREATE TABLE verbal_remarks_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,"
            "section_id INTEGER, description TEXT NOT NULL, deadline TEXT,"
            "status TEXT DEFAULT 'open',"
            "issued_by INTEGER REFERENCES users(id),"
            "closed_at TEXT, closed_note TEXT)"
        ),
        'input_control': (
            "CREATE TABLE input_control_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,"
            "material_name TEXT, quantity TEXT, document_name TEXT,"
            "deviation_note TEXT, engineer_id INTEGER REFERENCES users(id),"
            "section_id INTEGER, contractor_id INTEGER)"
        ),
    }
    for tbl, create_sql in tables.items():
        fk_list = db.execute(f"PRAGMA foreign_key_list({tbl})").fetchall()
        # Ищем жёсткий FK на sections или contractors по полям section_id / contractor_id
        has_bad_fk = any(
            row['table'] in ('sections', 'contractors') and
            row['from'] in ('section_id', 'contractor_id')
            for row in fk_list
        )
        if not has_bad_fk:
            continue
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN")
        try:
            db.execute(create_sql)
            cols = [r['name'] for r in db.execute(f"PRAGMA table_info({tbl})").fetchall()]
            new_cols = [r['name'] for r in db.execute(f"PRAGMA table_info({tbl}_new)").fetchall()]
            shared = [c for c in cols if c in new_cols]
            col_list = ', '.join(shared)
            db.execute(f"INSERT INTO {tbl}_new ({col_list}) SELECT {col_list} FROM {tbl}")
            db.execute(f"DROP TABLE {tbl}")
            db.execute(f"ALTER TABLE {tbl}_new RENAME TO {tbl}")
            db.execute("COMMIT")
            print(f"✅ _fix_section_id_fk({tbl}): FK снят, данные сохранены")
        except Exception as e:
            db.execute("ROLLBACK")
            print(f"⚠️  _fix_section_id_fk({tbl}) failed: {e}")
        finally:
            db.execute("PRAGMA foreign_keys = ON")

auto_migrate()
CORS(app)

# ─────────────────────────────────────────────────────────────
# АУТЕНТИФИКАЦИЯ (ШАГ 2б) — переходный период
#
# Пароль считается заданным ТОЛЬКО если password_hash в формате werkzeug
# (scrypt:.../pbkdf2:... — содержит '$'). Старые sha256-хеши (64 hex-символа,
# без '$') остались от тестового пароля и паролем НЕ считаются: такой
# пользователь входит по-старому, выбором из списка, и по паролю не пускается
# ни при каких условиях.
# ─────────────────────────────────────────────────────────────

def _is_real_password_hash(h):
    """True только для хешей werkzeug. Legacy sha256 (без '$') → False."""
    return bool(h) and '$' in h

def _load_secret_key():
    """Ключ подписи сессий. Приоритет — переменная окружения SK_SECRET_KEY.
    Иначе генерируется один раз в .secret_key рядом с app.py (в .gitignore).
    Постоянство ключа обязательно: иначе рестарт разлогинивает всех."""
    env = os.environ.get('SK_SECRET_KEY')
    if env:
        return env
    path = os.path.join(os.path.dirname(__file__), '.secret_key')
    try:
        if os.path.exists(path):
            with open(path) as f:
                key = f.read().strip()
            if key:
                return key
        key = secrets.token_hex(32)
        with open(path, 'w') as f:
            f.write(key)
        os.chmod(path, 0o600)
        print('🔑 Создан новый ключ подписи сессий: .secret_key')
        return key
    except Exception as e:
        print(f'⚠️  Не удалось сохранить .secret_key ({e}) — ключ на время работы процесса')
        return secrets.token_hex(32)

app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # По умолчанию cookie только по HTTPS. Для локальной отладки по http
    # запускать с SK_COOKIE_INSECURE=1
    SESSION_COOKIE_SECURE=(os.environ.get('SK_COOKIE_INSECURE') != '1'),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

#: значение, которое заведомо не совпадёт ни с одним id — для фильтров
#: с нечисловым значением: показать ничего, а не всё
NO_MATCH = -1


def arg_int(name, invalid=None):
    """Числовой параметр запроса, приведённый к int.

    HTTP приносит всё строками. SQLite сравнивал строку с числовой колонкой
    молча и ничего не находил, PostgreSQL на нечисловом значении вернул бы
    ошибку 500. Приводим явно: '4' → 4.

    Отсутствующий параметр — всегда None: вызывающий код проверяет его
    через `if`, и подстановка значения здесь включила бы фильтр, которого
    пользователь не просил.

    Аргумент invalid — что вернуть, если параметр ПЕРЕДАН, но не число:
      NO_MATCH для фильтров (показать пусто, а не всё, как делал SQLite),
      None для параметров действующего пользователя (сработает проверка
      прав и вернётся 403).
    """
    v = request.args.get(name)
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return invalid


def current_user_id():
    """id вошедшего пользователя из серверной сессии (или None)."""
    return session.get('uid')

@app.before_request
def _session_identity_priority():
    """Для вошедших ПО ПАРОЛЮ сессия важнее user_id из запроса: клиент не может
    выдать себя за другого. Вошедшие по-старому работают как прежде —
    обратная совместимость на переходный период."""
    uid = session.get('uid')
    if not uid or session.get('auth') != 'password':
        return
    args = request.args.to_dict(flat=False)
    touched = False
    for key in ('user_id', 'requester_id'):
        if key in args:
            args[key] = [str(uid)]
            touched = True
    if touched:
        from werkzeug.datastructures import ImmutableMultiDict
        request.args = ImmutableMultiDict(
            [(k, v) for k, vals in args.items() for v in vals]
        )

def _start_session(user_row, how, remember):
    session.clear()
    session['uid'] = user_row['id']
    session['auth'] = how          # 'password' | 'legacy'
    session.permanent = bool(remember)

def _public_user(row):
    return {'id': row['id'], 'full_name': row['full_name'], 'email': row['email'],
            'role': row['role'], 'can_view_all': row['can_view_all']}

@app.get('/api/auth/login_users')
def auth_login_users():
    """Список для старого входа: только активные и БЕЗ настоящего пароля.
    Пользователи с паролем из списка исчезают — для них форма email+пароль."""
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, full_name, email, role, password_hash, "
            "COALESCE(can_view_all,0) as can_view_all "
            "FROM users WHERE COALESCE(is_active,1)=1 ORDER BY full_name"
        ).fetchall()
        return ok([_public_user(r) for r in rows if not _is_real_password_hash(r['password_hash'])])

@app.post('/api/auth/login')
def auth_login():
    """Вход по email + паролю."""
    d = request.json or {}
    email = (d.get('email') or '').strip().lower()
    password = d.get('password') or ''
    if not email or not password:
        return err('Укажите email и пароль')
    with db_conn() as db:
        row = db.execute(
            "SELECT id, full_name, email, role, password_hash, "
            "COALESCE(is_active,1) as is_active, COALESCE(can_view_all,0) as can_view_all "
            "FROM users WHERE lower(email)=?", (email,)
        ).fetchone()
        # Единое сообщение — не раскрываем, существует ли учётка
        if not row or not row['is_active']:
            return err('Неверный email или пароль', 401)
        # Legacy sha256 паролем не считается: вход по паролю невозможен
        if not _is_real_password_hash(row['password_hash']):
            return err('Неверный email или пароль', 401)
        if not check_password_hash(row['password_hash'], password):
            return err('Неверный email или пароль', 401)
        _start_session(row, 'password', d.get('remember'))
        return ok(_public_user(row))

@app.post('/api/auth/login_legacy')
def auth_login_legacy():
    """Старый вход — выбором из списка, без пароля. Только для активных
    пользователей без настоящего пароля. Переходный период."""
    d = request.json or {}
    uid = d.get('user_id')
    if not uid:
        return err('user_id обязателен')
    with db_conn() as db:
        row = db.execute(
            "SELECT id, full_name, email, role, password_hash, "
            "COALESCE(is_active,1) as is_active, COALESCE(can_view_all,0) as can_view_all "
            "FROM users WHERE id=?", (uid,)
        ).fetchone()
        if not row or not row['is_active']:
            return err('Пользователь не найден или деактивирован', 403)
        if _is_real_password_hash(row['password_hash']):
            return err('Для этой учётной записи требуется вход по паролю', 403)
        _start_session(row, 'legacy', d.get('remember'))
        return ok(_public_user(row))

@app.post('/api/auth/logout')
def auth_logout():
    session.clear()
    return ok({'logged_out': True})

@app.get('/api/auth/me')
def auth_me():
    """Кто вошёл. Нужен для «запомнить меня»: sessionStorage не переживает
    закрытие браузера, а cookie — да."""
    uid = current_user_id()
    if not uid:
        return ok(None)
    with db_conn() as db:
        row = db.execute(
            "SELECT id, full_name, email, role, COALESCE(is_active,1) as is_active, "
            "COALESCE(can_view_all,0) as can_view_all FROM users WHERE id=?", (uid,)
        ).fetchone()
        if not row or not row['is_active']:
            session.clear()
            return ok(None)
        data = _public_user(row)
        data['auth'] = session.get('auth')
        return ok(data)

@app.post('/api/auth/change_password')
def auth_change_password():
    """Смена собственного пароля вошедшим пользователем."""
    uid = current_user_id()
    if not uid:
        return err('Требуется вход', 401)
    d = request.json or {}
    current = d.get('current_password') or ''
    new = d.get('new_password') or ''
    confirm = d.get('confirm_password') or ''
    if len(new) < 8:
        return err('Новый пароль — минимум 8 символов')
    if new != confirm:
        return err('Новый пароль и подтверждение не совпадают')
    with db_conn() as db:
        row = db.execute("SELECT id, password_hash FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return err('Пользователь не найден', 404)
        if not _is_real_password_hash(row['password_hash']):
            # Первый пароль выдаёт root — иначе любой, кто выбрал имя из
            # списка, мог бы поставить пароль и запереть настоящего владельца
            return err('Пароль для этой учётной записи выдаёт администратор', 403)
        if not check_password_hash(row['password_hash'], current):
            return err('Текущий пароль неверен', 403)
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(new), uid))
        db.commit()
    return ok({'changed': True})

_PWD_ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # без 0/O/1/l/I

def _generate_password(length=14):
    return ''.join(secrets.choice(_PWD_ALPHABET) for _ in range(length))

# ── Права на управление учётными записями (ШАГ 4) ──
#   root  — всё, включая учётки администраторов
#   admin — всё, КРОМЕ учёток администраторов
#   учётку root не может изменить или деактивировать никто, включая её саму
ENGINEER_ROLES = ('engineer', 'senior')

def _actor_row(db):
    """Кто выполняет действие: приоритет у сессии, иначе user_id из запроса
    (обратная совместимость на переходный период)."""
    aid = current_user_id() or arg_int('user_id')
    if not aid:
        return None
    return db.execute(
        "SELECT id, role, COALESCE(is_active,1) as is_active FROM users WHERE id=?",
        (aid,)
    ).fetchone()

def _can_manage(actor, target_role):
    """Может ли actor управлять учёткой с ролью target_role.
    Возвращает (True, None) или (False, 'причина')."""
    if not actor or not actor['is_active']:
        return False, 'Требуется вход'
    if actor['role'] not in ADMIN_ROLES:
        return False, 'Доступ запрещён'
    if target_role == 'root':
        return False, 'Учётную запись главного администратора изменить нельзя'
    if target_role == 'admin' and actor['role'] != 'root':
        return False, 'Управлять администраторами может только главный администратор'
    return True, None

@app.post('/api/auth/admin_set_password')
def auth_admin_set_password():
    """Только роль root: выдать пользователю сгенерированный пароль.
    Пароль возвращается в ответе ОДИН раз — в БД лежит только хеш."""
    actor_id = current_user_id() or arg_int('user_id')
    d = request.json or {}
    target_id = d.get('user_id')
    if not actor_id:
        return err('Требуется вход', 401)
    if not target_id:
        return err('user_id (кому выдать пароль) обязателен')
    with db_conn() as db:
        actor = db.execute(
            "SELECT role, COALESCE(is_active,1) as is_active FROM users WHERE id=?",
            (actor_id,)
        ).fetchone()
        if not actor or not actor['is_active'] or actor['role'] != 'root':
            return err('Доступ запрещён', 403)
        target = db.execute(
            "SELECT id, full_name, email FROM users WHERE id=?", (target_id,)
        ).fetchone()
        if not target:
            return err('Пользователь не найден', 404)
        password = _generate_password()
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(password), target['id']))
        db.commit()
        return ok({'user_id': target['id'], 'full_name': target['full_name'],
                   'email': target['email'], 'password': password,
                   'note': 'Пароль показан один раз — передайте пользователю и не сохраняйте.'})

@app.get('/')
def index():
    return send_from_directory('static', 'login.html')

# Каталог загрузок. SK_UPLOAD_DIR позволяет автотестам писать во временную
# папку и не трогать боевые фотографии.
UPLOAD_FOLDER = os.environ.get('SK_UPLOAD_DIR') or os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024  # 30 MB max на файл

# ─────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────────────────

def rows_to_list(rows):
    return [dict(r) for r in rows]

def ok(data=None, **kwargs):
    resp = {'ok': True}
    if data is not None:
        resp['data'] = data
    resp.update(kwargs)
    return jsonify(resp)

def err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code

@contextmanager
def db_conn():
    """Контекст-менеджер: открывает соединение с БД и гарантирует db.close()
    в любом случае (успех, исключение, ранний return)."""
    db = get_db()
    try:
        yield db
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()

# ─────────────────────────────────────────────────────────
# ОБЪЕКТЫ
# ─────────────────────────────────────────────────────────

@app.get('/api/objects')
def list_objects():
    user_id = arg_int('user_id')
    with db_conn() as db:
        if user_id:
            user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if user and user['role'] == 'engineer':
                rows = db.execute(
                    "SELECT o.* FROM objects o "
                    "JOIN object_users ou ON ou.object_id=o.id "
                    "WHERE ou.user_id=? AND o.is_active=1 ORDER BY o.name",
                    (user_id,)
                ).fetchall()
                return ok(rows_to_list(rows))
        # Объект активен, только если активен и он сам, и его проект.
        # Объекты без проекта (project_id IS NULL) считаются активными —
        # деактивированного проекта у них нет.
        rows = db.execute("""
            SELECT o.* FROM objects o
            LEFT JOIN projects p ON p.id = o.project_id
            WHERE o.is_active=1 AND (o.project_id IS NULL OR p.is_active=1)
            ORDER BY o.name
        """).fetchall()
        return ok(rows_to_list(rows))

@app.post('/api/objects')
def create_object():
    d = request.json or {}
    if not d.get('name'):
        return err('name обязателен')
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO objects (name, address, client_name, contract_number, tj_object_id, project_id) VALUES (?,?,?,?,?,?) RETURNING id",
            (d['name'], d.get('address'), d.get('client_name'), d.get('contract_number'), d.get('tj_object_id'), d.get('project_id'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM objects WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.get('/api/objects/<int:obj_id>')
def get_object(obj_id):
    with db_conn() as db:
        row = db.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
        if not row:
            return err('Объект не найден', 404)
        sections = db.execute("SELECT * FROM sections WHERE object_id=? AND is_active=1", (obj_id,)).fetchall()

        # Автосинхронизация: партнёры проекта ↔ подрядчики объекта (по partner_id)
        obj_project_id = row['project_id']
        # Активные партнёры этого проекта — по partner_projects (many-to-many)
        if obj_project_id:
            proj_partners = db.execute("""
                SELECT p.id, p.name, p.type, p.work_type
                FROM partners p
                JOIN partner_projects pp ON pp.partner_id = p.id
                WHERE pp.project_id = ? AND p.is_active = 1
            """, (obj_project_id,)).fetchall()
        else:
            proj_partners = []
        proj_partner_ids = {p['id'] for p in proj_partners}
        # Все записи contractors этого объекта с partner_id (партнёрские)
        existing = db.execute(
            "SELECT id, name, work_type, is_active, partner_id, hidden_manually FROM contractors WHERE object_id=?",
            (obj_id,)
        ).fetchall()
        # Словарь partner_id → запись (только для тех, у кого partner_id IS NOT NULL)
        existing_by_pid = {r['partner_id']: r for r in existing if r['partner_id'] is not None}

        # 1. Партнёры, убранные из проекта → деактивировать (hidden_manually не трогаем)
        for pid, rec in existing_by_pid.items():
            if pid not in proj_partner_ids and rec['is_active']:
                db.execute("UPDATE contractors SET is_active=0 WHERE id=?", (rec['id'],))

        # 2. Партнёры проекта → создать запись если нет, обновить name/work_type если изменились
        #    hidden_manually=1 → запись скрыта администратором вручную, НЕ трогаем вообще
        #    hidden_manually=0 и is_active=0 → реактивировать (синх вернула партнёра в проект)
        for p in proj_partners:
            wt = p['work_type'] or p['type']
            if p['id'] in existing_by_pid:
                rec = existing_by_pid[p['id']]
                if rec['hidden_manually']:
                    continue  # скрыт вручную — не реактивировать, не трогать
                updates = {}
                if not rec['is_active']:
                    updates['is_active'] = 1
                if p['name'] != rec['name']:
                    updates['name'] = p['name']
                if wt and wt != rec['work_type']:
                    updates['work_type'] = wt
                if updates:
                    sql = ', '.join(f"{k}=?" for k in updates)
                    db.execute(f"UPDATE contractors SET {sql} WHERE id=?",
                               list(updates.values()) + [rec['id']])
            else:
                db.execute(
                    "INSERT INTO contractors (object_id, name, work_type, partner_id) VALUES (?,?,?,?)",
                    (obj_id, p['name'], wt, p['id'])
                )
        db.commit()

        contractors = db.execute("SELECT * FROM contractors WHERE object_id=? AND is_active=1", (obj_id,)).fetchall()
        engineers = db.execute("""
            SELECT u.id, u.full_name, u.email, u.role
            FROM users u JOIN object_users ou ON u.id=ou.user_id
            WHERE ou.object_id=?
        """, (obj_id,)).fetchall()
        result = dict(row)
        result['sections'] = rows_to_list(sections)
        result['contractors'] = rows_to_list(contractors)
        result['engineers'] = rows_to_list(engineers)
        return ok(result)

@app.patch('/api/objects/<int:obj_id>')
def update_object(obj_id):
    d = request.json or {}
    with db_conn() as db:
        fields = ['name', 'address', 'client_name', 'contract_number', 'tj_object_id', 'is_active', 'project_id']
        updates = {k: v for k, v in d.items() if k in fields}
        if not updates:
            return err('Нет полей для обновления')
        sql = ', '.join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE objects SET {sql} WHERE id=?", list(updates.values()) + [obj_id])
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# СЕКЦИИ (корпуса / блоки)
# ─────────────────────────────────────────────────────────

@app.post('/api/objects/<int:obj_id>/sections')
def add_section(obj_id):
    d = request.json or {}
    if not d.get('name'):
        return err('name обязателен')
    with db_conn() as db:
        cur = db.execute("INSERT INTO sections (object_id, name) VALUES (?,?) RETURNING id", (obj_id, d['name']))
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM sections WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.patch('/api/sections/<int:sec_id>')
def update_section(sec_id):
    d = request.json or {}
    with db_conn() as db:
        if 'name' in d:
            db.execute("UPDATE sections SET name=? WHERE id=?", (d['name'], sec_id))
        if 'is_active' in d:
            db.execute("UPDATE sections SET is_active=? WHERE id=?", (d['is_active'], sec_id))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ПОДРЯДЧИКИ
# ─────────────────────────────────────────────────────────

@app.get('/api/objects/<int:obj_id>/contractors/hidden')
def get_hidden_contractors(obj_id):
    with db_conn() as db:
        rows = db.execute(
            "SELECT id, name, work_type FROM contractors WHERE object_id=? AND hidden_manually=1",
            (obj_id,)
        ).fetchall()
        return ok(rows_to_list(rows))

@app.post('/api/objects/<int:obj_id>/contractors')
def add_contractor(obj_id):
    d = request.json or {}
    if not d.get('name'):
        return err('name обязателен')
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO contractors (object_id, name, work_type) VALUES (?,?,?) RETURNING id",
            (obj_id, d['name'], d.get('work_type'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM contractors WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.patch('/api/contractors/<int:c_id>')
def update_contractor(c_id):
    d = request.json or {}
    with db_conn() as db:
        fields = ['name', 'work_type', 'is_active', 'hidden_manually']
        updates = {k: v for k, v in d.items() if k in fields}
        if updates:
            sql = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE contractors SET {sql} WHERE id=?", list(updates.values()) + [c_id])
            db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ПОЛЬЗОВАТЕЛИ
# ─────────────────────────────────────────────────────────

@app.get('/api/users')
def list_users():
    with db_conn() as db:
        rows = db.execute("SELECT id, full_name, email, role, tj_user_id, COALESCE(is_active,1) as is_active, COALESCE(can_view_all,0) as can_view_all FROM users ORDER BY full_name").fetchall()
        return ok(rows_to_list(rows))

@app.get('/api/users/<int:user_id>')
def get_user(user_id):
    with db_conn() as db:
        row = db.execute("SELECT id, full_name, email, role, is_active, can_view_all FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return err('Пользователь не найден', 404)
        return ok(dict(row))

@app.patch('/api/users/<int:user_id>')
def update_user(user_id):
    d = request.json or {}
    with db_conn() as db:
        target = db.execute("SELECT id, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            return err('Пользователь не найден', 404)
        actor = _actor_row(db)
        # Право на текущую роль учётки
        can, why = _can_manage(actor, target['role'])
        if not can:
            return err(why, 403)
        allowed = ['full_name', 'email', 'role', 'is_active', 'can_view_all']
        updates = {k: v for k, v in d.items() if k in allowed}
        new_role = updates.get('role')
        if new_role is not None and new_role != target['role']:
            if new_role not in ENGINEER_ROLES + ('admin',):
                return err('Недопустимая роль')
            # Назначить или снять роль администратора может только root
            can2, why2 = _can_manage(actor, new_role)
            if not can2:
                return err(why2, 403)
        if updates:
            sql = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE users SET {sql} WHERE id=?", list(updates.values()) + [user_id])
            # Администратору объекты не нужны — снимаем привязки, если роль сменилась
            if new_role in ADMIN_ROLES:
                db.execute("DELETE FROM object_users WHERE user_id=?", (user_id,))
            db.commit()
        return ok()

@app.post('/api/users')
def create_user():
    """Создание учётной записи. Пароль генерируется ВСЕГДА и показывается
    один раз — учётка без настоящего пароля не создаётся ни при каких
    условиях (иначе она попадёт в открытый список входа)."""
    d = request.json or {}
    if not d.get('email') or not d.get('full_name'):
        return err('email и full_name обязательны')
    role = d.get('role', 'engineer')
    if role not in ENGINEER_ROLES + ('admin',):
        return err('Недопустимая роль')
    with db_conn() as db:
        actor = _actor_row(db)
        allowed, why = _can_manage(actor, role)
        if not allowed:
            return err(why, 403)
        password = _generate_password()
        try:
            cur = db.execute(
                "INSERT INTO users (full_name, email, role, password_hash, tj_user_id) VALUES (?,?,?,?,?) RETURNING id",
                (d['full_name'], d['email'], role, generate_password_hash(password), d.get('tj_user_id'))
            )
            _nid = cur.fetchone()['id']
            db.commit()
        except Exception:
            return err('Пользователь с таким email уже существует')
        row = db.execute("SELECT id, full_name, email, role FROM users WHERE id=?",
                         (_nid,)).fetchone()
        data = dict(row)
        data['password'] = password
        data['note'] = 'Пароль показан один раз — передайте пользователю и не сохраняйте.'
        return ok(data), 201

@app.post('/api/users/<int:user_id>/reset_password')
def reset_user_password(user_id):
    """Сброс пароля учётной записи. Инженеров сбрасывает любой админ,
    администраторов — только root. Свою учётку root сбрасывает сам."""
    with db_conn() as db:
        actor = _actor_row(db)
        if not actor or not actor['is_active']:
            return err('Требуется вход', 401)
        target = db.execute("SELECT id, full_name, email, role FROM users WHERE id=?",
                            (user_id,)).fetchone()
        if not target:
            return err('Пользователь не найден', 404)
        if target['role'] == 'root':
            # Единственное исключение: root сбрасывает пароль сам себе
            if not (actor['role'] == 'root' and int(actor['id']) == int(target['id'])):
                return err('Сбросить пароль главного администратора может только он сам', 403)
        else:
            allowed, why = _can_manage(actor, target['role'])
            if not allowed:
                return err(why, 403)
        password = _generate_password()
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(password), target['id']))
        db.commit()
        return ok({'id': target['id'], 'full_name': target['full_name'],
                   'email': target['email'], 'password': password,
                   'note': 'Пароль показан один раз — передайте пользователю и не сохраняйте.'})

@app.post('/api/objects/<int:obj_id>/assign_user')
def assign_user(obj_id):
    d = request.json or {}
    if not d.get('user_id'):
        return err('user_id обязателен')
    with db_conn() as db:
        # Администраторы видят всю организацию — привязка к объекту не имеет
        # смысла и оставляет рудиментные записи в object_users
        u = db.execute(
            "SELECT role, COALESCE(is_active,1) as is_active FROM users WHERE id=?",
            (d['user_id'],)
        ).fetchone()
        if u and u['role'] in ADMIN_ROLES:
            return err('Администратора не назначают на объект — он видит всю организацию')
        # Архивного пользователя на объект не назначаем. Уже существующие
        # назначения при этом сохраняются — они часть истории.
        if u and not u['is_active']:
            return err('Нельзя назначить архивного пользователя — сначала восстановите его')
        try:
            db.execute(
                "INSERT INTO object_users (object_id, user_id, date_from) VALUES (?,?,?) "
                "ON CONFLICT (object_id, user_id) DO UPDATE SET date_from=EXCLUDED.date_from",
                (obj_id, d['user_id'], d.get('date_from', date.today().isoformat()))
            )
            db.commit()
            return ok()
        except Exception as e:
            return err(str(e))

@app.post('/api/objects/<int:obj_id>/unassign_user')
def unassign_user(obj_id):
    d = request.json or {}
    if not d.get('user_id'):
        return err('user_id обязателен')
    with db_conn() as db:
        db.execute(
            "DELETE FROM object_users WHERE object_id=? AND user_id=?",
            (obj_id, d['user_id'])
        )
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ЕЖЕДНЕВНЫЕ СВОДКИ
# ─────────────────────────────────────────────────────────

@app.get('/api/objects/<int:obj_id>/reports')
def list_reports(obj_id):
    with db_conn() as db:
        rows = db.execute("""
            SELECT dr.*, u.full_name as engineer_name
            FROM daily_reports dr
            JOIN users u ON u.id = dr.user_id
            WHERE dr.object_id=?
            ORDER BY dr.report_date DESC
            LIMIT 90
        """, (obj_id,)).fetchall()
        return ok(rows_to_list(rows))

@app.post('/api/reports')
def create_report():
    d = request.json or {}
    required = ['object_id', 'user_id', 'report_date']
    for f in required:
        if not d.get(f):
            return err(f'{f} обязателен')
    with db_conn() as db:
        # Инженер и главный инженер (senior) могут создавать сводки только по назначенным объектам
        user = db.execute("SELECT role FROM users WHERE id=?", (d['user_id'],)).fetchone()
        if user and user['role'] in ('engineer', 'senior'):
            assigned = db.execute(
                "SELECT 1 FROM object_users WHERE user_id=? AND object_id=?",
                (d['user_id'], d['object_id'])
            ).fetchone()
            if not assigned:
                return err('Объект не назначен инженеру', 403)
        try:
            cur = db.execute(
                "INSERT INTO daily_reports (object_id, user_id, report_date) VALUES (?,?,?) RETURNING id",
                (d['object_id'], d['user_id'], d['report_date'])
            )
            _nid = cur.fetchone()['id']
            db.commit()
            row = db.execute("SELECT * FROM daily_reports WHERE id=?", (_nid,)).fetchone()
            return ok(dict(row)), 201
        except Exception as e:
            return err(f'Сводка за эту дату уже существует: {e}')

@app.get('/api/reports/<int:report_id>')
def get_report(report_id):
    with db_conn() as db:
        report = db.execute("""
            SELECT dr.*, u.full_name as engineer_name, o.name as object_name
            FROM daily_reports dr
            JOIN users u ON u.id=dr.user_id
            JOIN objects o ON o.id=dr.object_id
            WHERE dr.id=?
        """, (report_id,)).fetchone()
        if not report:
            return err('Сводка не найдена', 404)
        r = dict(report)
        r['personnel'] = rows_to_list(db.execute("""
            SELECT pe.*, c.name as contractor_name, s.name as section_name
            FROM personnel_entries pe
            LEFT JOIN contractors c ON c.id=pe.contractor_id
            LEFT JOIN sections s ON s.id=pe.section_id
            WHERE pe.report_id=?
            ORDER BY c.name
        """, (report_id,)).fetchall())
        r['input_control'] = rows_to_list(db.execute(
            "SELECT ic.*, s.name as section_name, c.name as contractor_name FROM input_control ic LEFT JOIN sections s ON s.id=ic.section_id LEFT JOIN contractors c ON c.id=ic.contractor_id WHERE ic.report_id=?", (report_id,)).fetchall())
        r['operational_control'] = rows_to_list(db.execute(
            "SELECT oc.*, s.name as section_name, c.name as contractor_name FROM operational_control oc LEFT JOIN sections s ON s.id=oc.section_id LEFT JOIN contractors c ON c.id=oc.contractor_id WHERE oc.report_id=?", (report_id,)).fetchall())
        r['verbal_remarks'] = rows_to_list(db.execute(
            "SELECT vr.*, s.name as section_name, u.full_name as issued_by_name FROM verbal_remarks vr LEFT JOIN sections s ON s.id=vr.section_id LEFT JOIN users u ON u.id=vr.issued_by WHERE vr.report_id=?", (report_id,)).fetchall())
        r['prescriptions_log'] = rows_to_list(db.execute(
            "SELECT pl.*, s.name as section_name FROM prescriptions_log pl LEFT JOIN sections s ON s.id=pl.section_id WHERE pl.report_id=?", (report_id,)).fetchall())
        r['meetings'] = rows_to_list(db.execute(
            "SELECT * FROM meetings WHERE report_id=?", (report_id,)).fetchall())
        r['photos'] = rows_to_list(db.execute(
            "SELECT * FROM photos WHERE report_id=? ORDER BY sort_order", (report_id,)).fetchall())
        r['acceptance_control'] = rows_to_list(db.execute(
            "SELECT ac.*, s.name as section_name, c.name as contractor_name FROM acceptance_control ac LEFT JOIN sections s ON s.id=ac.section_id LEFT JOIN contractors c ON c.id=ac.contractor_id WHERE ac.report_id=?", (report_id,)).fetchall())
        r['ks2_check'] = rows_to_list(db.execute(
            "SELECT * FROM ks2_check WHERE report_id=?", (report_id,)).fetchall())
        return ok(r)

@app.post('/api/reports/<int:report_id>/submit')
def submit_report(report_id):
    with db_conn() as db:
        db.execute(
            "UPDATE daily_reports SET status='submitted', submitted_at=? WHERE id=?",
            (datetime.now().isoformat(), report_id)
        )
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ПЕРСОНАЛ
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/personnel')
def add_personnel(report_id):
    d = request.json or {}
    with db_conn() as db:
        entries = d if isinstance(d, list) else [d]
        # Атомарная замена: сначала удаляем ВСЕ строки ТОЛЬКО этой сводки,
        # затем вставляем переданные. Чужие сводки не затрагиваются.
        db.execute("DELETE FROM personnel_entries WHERE report_id=?", (report_id,))
        for e in entries:
            if not e.get('contractor_id'):
                continue
            db.execute(
                "INSERT INTO personnel_entries (report_id, contractor_id, section_id, headcount, work_description) VALUES (?,?,?,?,?)",
                (report_id, e['contractor_id'], e.get('section_id'),
                 int(e.get('headcount') or 0), e.get('work_description') or '')
            )
        db.commit()
        return ok()

@app.delete('/api/reports/<int:report_id>/personnel/<int:entry_id>')
def delete_personnel(report_id, entry_id):
    with db_conn() as db:
        db.execute("DELETE FROM personnel_entries WHERE id=? AND report_id=?", (entry_id, report_id))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ВХОДНОЙ КОНТРОЛЬ
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/input_control')
def add_input_control(report_id):
    d = request.json or {}
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO input_control (report_id, material_name, quantity, document_name, section_id, status, deviation_note, engineer_id, contractor_id) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
            (report_id, d.get('material_name'), d.get('quantity'), d.get('document_name'), d.get('section_id'), d.get('status',''), d.get('deviation_note',''), d.get('engineer_id'), d.get('contractor_id'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT ic.*, s.name as section_name, c.name as contractor_name FROM input_control ic LEFT JOIN sections s ON s.id=ic.section_id LEFT JOIN contractors c ON c.id=ic.contractor_id WHERE ic.id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.delete('/api/input_control/<int:ic_id>')
def delete_input_control(ic_id):
    with db_conn() as db:
        db.execute("DELETE FROM input_control WHERE id=?", (ic_id,))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# УСТНЫЕ ЗАМЕЧАНИЯ
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/remarks')
def add_remark(report_id):
    d = request.json or {}
    if not d.get('description'):
        return err('description обязателен')
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO verbal_remarks (report_id, section_id, description, deadline, issued_by) VALUES (?,?,?,?,?) RETURNING id",
            (report_id, d.get('section_id'), d['description'], d.get('deadline'), d.get('issued_by'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM verbal_remarks WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.patch('/api/remarks/<int:remark_id>/close')
def close_remark(remark_id):
    d = request.json or {}
    with db_conn() as db:
        db.execute(
            "UPDATE verbal_remarks SET status='closed', closed_at=?, closed_note=? WHERE id=?",
            (d.get('closed_at', date.today().isoformat()), d.get('closed_note'), remark_id)
        )
        db.commit()
        return ok()

@app.delete('/api/remarks/<int:remark_id>')
def delete_remark(remark_id):
    with db_conn() as db:
        db.execute("DELETE FROM verbal_remarks WHERE id=?", (remark_id,))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ПРЕДПИСАНИЯ (журнал ссылок на TeamJect)
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/prescriptions')
def add_prescription(report_id):
    d = request.json or {}
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO prescriptions_log (report_id, tj_prescription_id, number, issue_date, section_id, deadline, status) VALUES (?,?,?,?,?,?,?) RETURNING id",
            (report_id, d.get('tj_prescription_id'), d.get('number'), d.get('issue_date'), d.get('section_id'), d.get('deadline'), d.get('status'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM prescriptions_log WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

# ─────────────────────────────────────────────────────────
# ОПЕРАЦИОННЫЙ КОНТРОЛЬ
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/operational_control')
def add_operational_control(report_id):
    d = request.json or {}
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO operational_control (report_id, section_id, work_stage, controlled_operations, control_method, status, deviation_note, engineer_id, contractor_id) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
            (report_id, d.get('section_id'), d.get('work_stage'), d.get('controlled_operations'), d.get('control_method'), d.get('status',''), d.get('deviation_note',''), d.get('engineer_id'), d.get('contractor_id'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT oc.*, s.name as section_name, c.name as contractor_name FROM operational_control oc LEFT JOIN sections s ON s.id=oc.section_id LEFT JOIN contractors c ON c.id=oc.contractor_id WHERE oc.id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.delete('/api/operational_control/<int:oc_id>')
def delete_operational_control(oc_id):
    with db_conn() as db:
        db.execute("DELETE FROM operational_control WHERE id=?", (oc_id,))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# СОВЕЩАНИЯ
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/meetings')
def add_meeting(report_id):
    d = request.json or {}
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO meetings (report_id, location, time, participants, agenda, engineer_id) VALUES (?,?,?,?,?,?) RETURNING id",
            (report_id, d.get('location'), d.get('time'), d.get('participants'), d.get('agenda'), d.get('engineer_id'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM meetings WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.patch('/api/meetings/<int:meeting_id>')
def update_meeting(meeting_id):
    d = request.json or {}
    with db_conn() as db:
        fields = ['location', 'time', 'participants', 'agenda']
        updates = {k: v for k, v in d.items() if k in fields}
        if updates:
            sql = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE meetings SET {sql} WHERE id=?", list(updates.values()) + [meeting_id])
            db.commit()
        return ok()

@app.delete('/api/prescriptions/<int:pre_id>')
def delete_prescription(pre_id):
    with db_conn() as db:
        db.execute("DELETE FROM prescriptions_log WHERE id=?", (pre_id,))
        db.commit()
        return ok()

@app.delete('/api/meetings/<int:meeting_id>')
def delete_meeting(meeting_id):
    with db_conn() as db:
        db.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ПРОТОКОЛ СОВЕЩАНИЯ
# ─────────────────────────────────────────────────────────

@app.post('/api/meetings/<int:meeting_id>/protocol')
def upload_meeting_protocol(meeting_id):
    if 'file' not in request.files:
        return err('Файл не найден')
    f = request.files['file']
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ['.pdf', '.doc', '.docx']:
        return err('Допустимые форматы: pdf, doc, docx')
    original_name = f.filename
    filename = f"protocol_{meeting_id}_{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    # 1. Сохраняем файл на диск — БД ещё не открыта
    f.save(filepath)
    # 2. Только после записи на диск открываем БД и делаем короткий UPDATE
    db = get_db()
    try:
        db.execute("UPDATE meetings SET protocol_path=?, protocol_name=? WHERE id=?",
                   (filename, original_name, meeting_id))
        db.commit()
    except Exception:
        db.rollback()
        # Убираем осиротевший файл, если запись в БД не удалась
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise
    finally:
        db.close()
    return ok({'file_path': filename, 'original_name': original_name}), 201

@app.delete('/api/meetings/<int:meeting_id>/protocol')
def delete_meeting_protocol(meeting_id):
    with db_conn() as db:
        row = db.execute("SELECT protocol_path FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        if row and row['protocol_path']:
            fpath = os.path.join(UPLOAD_FOLDER, row['protocol_path'])
            if os.path.exists(fpath):
                os.remove(fpath)
            db.execute("UPDATE meetings SET protocol_path=NULL, protocol_name=NULL WHERE id=?", (meeting_id,))
            db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ФОТО
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/photos')
def upload_photo(report_id):
    if 'file' not in request.files:
        return err('Файл не найден')
    f = request.files['file']
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.heic', '.webp']:
        return err('Допустимые форматы: jpg, png, heic, webp')
    remark_id = request.form.get('remark_id')
    caption = request.form.get('caption', '')
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    # 1. Тяжёлая операция — сохранение файла на диск; БД ещё не открыта
    f.save(filepath)
    # 1b. Конвертация HEIC→JPEG и/или ресайз (всё ещё до открытия БД)
    final_path = _process_image(filepath)
    filename = os.path.basename(final_path)
    # 2. Файл обработан и лежит на диске — открываем БД только на короткую вставку
    db = get_db()
    try:
        sort_order = db.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 AS next_order FROM photos WHERE report_id=?",
            (report_id,)
        ).fetchone()['next_order']
        cur = db.execute(
            "INSERT INTO photos (report_id, file_path, caption, sort_order, remark_id) VALUES (?,?,?,?,?) RETURNING id",
            (report_id, filename, caption, sort_order, remark_id)
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM photos WHERE id=?", (_nid,)).fetchone()
        result = dict(row)
    except Exception:
        db.rollback()
        # Убираем осиротевший файл, если запись в БД не удалась
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise
    finally:
        db.close()
    return ok(result), 201

@app.patch('/api/photos/<int:photo_id>')
def update_photo(photo_id):
    d = request.json or {}
    with db_conn() as db:
        fields = ['caption', 'sort_order', 'remark_id']
        updates = {k: v for k, v in d.items() if k in fields}
        if updates:
            sql = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE photos SET {sql} WHERE id=?", list(updates.values()) + [photo_id])
            db.commit()
        return ok()

@app.delete('/api/photos/<int:photo_id>')
def delete_photo(photo_id):
    with db_conn() as db:
        row = db.execute("SELECT file_path FROM photos WHERE id=?", (photo_id,)).fetchone()
        if row:
            fpath = os.path.join(UPLOAD_FOLDER, row['file_path'])
            if os.path.exists(fpath):
                os.remove(fpath)
            db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
            db.commit()
        return ok()

@app.get('/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.get('/api/admin/photo_check')
def photo_check():
    """Диагностика: проверяем какие фото есть в БД и существуют ли файлы на диске"""
    user_id = arg_int('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] not in ADMIN_ROLES:
            return err('Доступ запрещён', 403)
        rows = db.execute("""
            SELECT ph.id, ph.file_path, ph.caption, dr.report_date,
                   u.full_name as engineer, o.name as object_name
            FROM photos ph
            JOIN daily_reports dr ON dr.id=ph.report_id
            JOIN users u ON u.id=dr.user_id
            JOIN objects o ON o.id=dr.object_id
            ORDER BY ph.id
        """).fetchall()
        result = []
        for r in rows:
            fpath = os.path.join(UPLOAD_FOLDER, r['file_path']) if r['file_path'] else None
            exists = os.path.isfile(fpath) if fpath else False
            result.append({**dict(r), 'file_exists': exists})
        return ok(result)

# ─────────────────────────────────────────────────────────
# ОТКРЫТЫЕ ЗАМЕЧАНИЯ по объекту (для руководителя)
# ─────────────────────────────────────────────────────────

@app.get('/api/users/<int:user_id>/objects')
def user_objects(user_id):
    with db_conn() as db:
        # Б5: блокируем доступ для архивных пользователей на уровне сервера
        user = db.execute("SELECT COALESCE(is_active,1) as is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user['is_active'] == 0:
            return err('Пользователь деактивирован. Обратитесь к администратору.', 403)
        rows = db.execute("""
            SELECT o.* FROM objects o
            JOIN object_users ou ON ou.object_id=o.id
            WHERE ou.user_id=? AND o.is_active=1
            ORDER BY o.name
        """, (user_id,)).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# ПРИЁМОЧНЫЙ КОНТРОЛЬ
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/acceptance_control')
def add_acceptance_control(report_id):
    d = request.json or {}
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO acceptance_control (report_id, section_id, work_stage, controlled_operations, control_method, status, deviation_note, engineer_id, contractor_id) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
            (report_id, d.get('section_id'), d.get('work_stage'), d.get('controlled_operations'), d.get('control_method'), d.get('status',''), d.get('deviation_note',''), d.get('engineer_id'), d.get('contractor_id'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT ac.*, s.name as section_name, c.name as contractor_name FROM acceptance_control ac LEFT JOIN sections s ON s.id=ac.section_id LEFT JOIN contractors c ON c.id=ac.contractor_id WHERE ac.id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.delete('/api/acceptance_control/<int:ac_id>')
def delete_acceptance_control(ac_id):
    with db_conn() as db:
        db.execute("DELETE FROM acceptance_control WHERE id=?", (ac_id,))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# ПРОВЕРКА ИД / КС-2
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/ks2_check')
def add_ks2_check(report_id):
    d = request.json or {}
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO ks2_check (report_id, contractor_name, object_work, ks2_number, ks3_number, has_ks3, has_ks6a, has_id, engineer_id) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
            (report_id, d.get('contractor_name'), d.get('object_work'), d.get('ks2_number'),
             d.get('ks3_number'), 1 if d.get('has_ks3') else 0,
             1 if d.get('has_ks6a') else 0, 1 if d.get('has_id') else 0, d.get('engineer_id'))
        )
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM ks2_check WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.delete('/api/ks2_check/<int:ks2_id>')
def delete_ks2_check(ks2_id):
    with db_conn() as db:
        db.execute("DELETE FROM ks2_check WHERE id=?", (ks2_id,))
        db.commit()
        return ok()


@app.post('/api/migrate')
def run_migration():
    user_id = arg_int('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] not in ADMIN_ROLES:
            return err('Доступ запрещён', 403)
        if IS_POSTGRES:
            return ok('Не требуется: схема PostgreSQL создаётся из db/schema_postgres.sql')
        try:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS acceptance_control (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                section_id INTEGER,
                work_stage TEXT,
                controlled_operations TEXT,
                control_method TEXT,
                status TEXT DEFAULT \'\',
                deviation_note TEXT DEFAULT \'\',
                engineer_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS ks2_check (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                contractor_name TEXT,
                object_work TEXT,
                ks2_number TEXT,
                has_ks6a INTEGER DEFAULT 0,
                has_id INTEGER DEFAULT 0,
                engineer_id INTEGER
            );
            """)
            pwd = hashlib.sha256(b'password123').hexdigest()
            db.execute("INSERT INTO users (full_name,email,role,tj_user_id,password_hash) VALUES ('Ухов Илья Викторович','uhov@stroymanager.ru','engineer','uhov_tj_001','"+pwd+"') ON CONFLICT DO NOTHING")
            db.execute("INSERT INTO objects (name,address,client_name,tj_object_id) VALUES ('IQ Гатчина (участок 6)','Ленинградская обл., г. Гатчина','ЛСТ Генподряд','tj_gatchina_006') ON CONFLICT DO NOTHING")
            gid = db.execute("SELECT id FROM objects WHERE tj_object_id='tj_gatchina_006'").fetchone()[0]
            uid = db.execute("SELECT id FROM users WHERE email='uhov@stroymanager.ru'").fetchone()[0]
            aid_row = db.execute("SELECT id FROM users WHERE role IN ('root','admin')").fetchone()
            aid = aid_row[0] if aid_row else None
            for name in ['Пятно застройки','Блок 3','ПОС']:
                db.execute("INSERT INTO sections (object_id,name) VALUES (?,?) ON CONFLICT DO NOTHING",(gid,name))
            for name,wt in [('ООО Гелиос','ПОС, замещение грунта'),('ООО Фортес','Лидерное бурение, сваи')]:
                db.execute("INSERT INTO contractors (object_id,name,work_type) VALUES (?,?,?) ON CONFLICT DO NOTHING",(gid,name,wt))
            db.execute("INSERT INTO object_users (object_id,user_id) VALUES (?,?) ON CONFLICT DO NOTHING",(gid,uid))
            if aid: db.execute("INSERT INTO object_users (object_id,user_id) VALUES (?,?) ON CONFLICT DO NOTHING",(gid,aid))
            db.commit()
            return ok('Миграция выполнена успешно')
        except Exception as e:
            return err(str(e))


@app.post('/api/fix_duplicates')
def fix_duplicates():
    user_id = arg_int('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] not in ADMIN_ROLES:
            return err('Доступ запрещён', 403)
        try:
            # Remove duplicate sections — keep only the one with min id per (object_id, name)
            db.execute("""
                DELETE FROM sections WHERE id NOT IN (
                    SELECT MIN(id) FROM sections GROUP BY object_id, name
                )
            """)
            # Remove duplicate contractors — keep only min id per (object_id, name)
            db.execute("""
                DELETE FROM contractors WHERE id NOT IN (
                    SELECT MIN(id) FROM contractors GROUP BY object_id, name
                )
            """)
            db.commit()
            return ok('Дубликаты удалены')
        except Exception as e:
            return err(str(e))


@app.get('/api/objects/<int:obj_id>/open_remarks')
def open_remarks(obj_id):
    with db_conn() as db:
        rows = db.execute("""
            SELECT vr.*, dr.report_date, s.name as section_name, u.full_name as engineer_name
            FROM verbal_remarks vr
            JOIN daily_reports dr ON dr.id=vr.report_id
            LEFT JOIN sections s ON s.id=vr.section_id
            LEFT JOIN users u ON u.id=vr.issued_by
            WHERE dr.object_id=? AND vr.status='open'
            ORDER BY dr.report_date DESC
        """, (obj_id,)).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# СТАТУС АКТИВНОСТИ ИНЖЕНЕРОВ (для руководителя)
# ─────────────────────────────────────────────────────────

@app.get('/api/objects/<int:obj_id>/activity')
def engineer_activity(obj_id):
    with db_conn() as db:
        today = date.today().isoformat()
        rows = db.execute("""
            SELECT u.id, u.full_name, u.email,
                   dr.report_date as last_report_date,
                   dr.status as last_report_status,
                   dr.id as last_report_id
            FROM object_users ou
            JOIN users u ON u.id=ou.user_id
            LEFT JOIN daily_reports dr ON dr.user_id=u.id AND dr.object_id=? AND dr.report_date=?
            WHERE ou.object_id=?
        """, (obj_id, today, obj_id)).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# ПРОЕКТЫ (настраивает Админ)
# ─────────────────────────────────────────────────────────

@app.get('/api/projects')
def list_projects():
    with db_conn() as db:
        rows = db.execute("SELECT * FROM projects WHERE is_active=1 ORDER BY name").fetchall()
        return ok(rows_to_list(rows))

@app.post('/api/projects')
def create_project():
    d = request.json or {}
    if not d.get('name'): return err('name обязателен')
    with db_conn() as db:
        cur = db.execute("INSERT INTO projects (name, description, tj_project_id) VALUES (?,?,?) RETURNING id",
            (d['name'], d.get('description'), d.get('tj_project_id')))
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM projects WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.patch('/api/projects/<int:proj_id>')
def update_project(proj_id):
    d = request.json or {}
    with db_conn() as db:
        fields = ['name', 'description', 'tj_project_id', 'is_active']
        updates = {k: v for k, v in d.items() if k in fields}
        if updates:
            sql = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE projects SET {sql} WHERE id=?", list(updates.values()) + [proj_id])
            db.commit()
        return ok()

@app.get('/api/projects/<int:proj_id>/objects')
def project_objects(proj_id):
    with db_conn() as db:
        rows = db.execute("""
            SELECT o.*, GROUP_CONCAT(u.full_name, ', ') as engineers
            FROM objects o
            LEFT JOIN object_users ou ON ou.object_id = o.id
            LEFT JOIN users u ON u.id = ou.user_id
            WHERE o.project_id=? AND o.is_active=1
            GROUP BY o.id ORDER BY o.name
        """, (proj_id,)).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# ПАРТНЁРЫ (настраивает Админ)
# ─────────────────────────────────────────────────────────

def _attach_partner_projects(db, partners_list):
    """Добавляет поле project_ids (список) к каждому партнёру."""
    if not partners_list:
        return partners_list
    ids = [p['id'] for p in partners_list]
    placeholders = ','.join('?' * len(ids))
    rows = db.execute(
        f"SELECT partner_id, project_id FROM partner_projects WHERE partner_id IN ({placeholders})",
        ids
    ).fetchall()
    proj_map = {}
    for r in rows:
        proj_map.setdefault(r['partner_id'], []).append(r['project_id'])
    for p in partners_list:
        p['project_ids'] = proj_map.get(p['id'], [])
    return partners_list

def _save_partner_projects(db, partner_id, project_ids):
    """Атомарно обновляет связи партнёра с проектами.
    Синхронизирует partners.project_id с первым выбранным (для совместимости с шагом 2)."""
    db.execute("DELETE FROM partner_projects WHERE partner_id=?", (partner_id,))
    for pid in project_ids:
        db.execute("INSERT INTO partner_projects (partner_id, project_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                   (partner_id, pid))
    compat_project_id = project_ids[0] if project_ids else None
    db.execute("UPDATE partners SET project_id=? WHERE id=?", (compat_project_id, partner_id))

@app.get('/api/partners')
def list_partners():
    with db_conn() as db:
        project_id = arg_int('project_id', NO_MATCH)
        if project_id:
            rows = db.execute(
                "SELECT * FROM partners WHERE is_active=1 AND (project_id=? OR project_id IS NULL) ORDER BY name",
                (project_id,)
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM partners WHERE is_active=1 ORDER BY name").fetchall()
        partners = rows_to_list(rows)
        return ok(_attach_partner_projects(db, partners))

@app.post('/api/partners')
def create_partner():
    d = request.json or {}
    if not d.get('name'): return err('name обязателен')
    project_ids = [int(x) for x in (d.get('project_ids') or []) if x]
    compat_pid = project_ids[0] if project_ids else None
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO partners (name, type, address, contact_name, contact_role, inn, phone, email, notes, work_type, project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (d['name'], d.get('type'), d.get('address'), d.get('contact_name'),
             d.get('contact_role'), d.get('inn'), d.get('phone'), d.get('email'),
             d.get('notes'), d.get('work_type'), compat_pid))
        _nid = cur.fetchone()['id']
        pid = _nid
        _save_partner_projects(db, pid, project_ids)
        db.commit()
        row = db.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
        result = dict(row)
        result['project_ids'] = project_ids
        return ok(result), 201

@app.patch('/api/partners/<int:pid>')
def update_partner(pid):
    d = request.json or {}
    with db_conn() as db:
        fields = ['name', 'type', 'address', 'contact_name', 'contact_role', 'inn', 'phone', 'email', 'notes', 'is_active', 'work_type']
        updates = {k: v for k, v in d.items() if k in fields}
        if updates:
            sql = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE partners SET {sql} WHERE id=?", list(updates.values()) + [pid])
        if 'project_ids' in d:
            project_ids = [int(x) for x in (d['project_ids'] or []) if x]
            _save_partner_projects(db, pid, project_ids)
        db.commit()
        return ok()

@app.delete('/api/partners/<int:pid>')
def delete_partner(pid):
    with db_conn() as db:
        db.execute("UPDATE partners SET is_active=0 WHERE id=?", (pid,))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# УЧАСТКИ (настраивает инженер сам)
# ─────────────────────────────────────────────────────────

@app.get('/api/objects/<int:obj_id>/my_sections')
def list_my_sections(obj_id):
    user_id = arg_int('user_id')
    with db_conn() as db:
        if user_id:
            rows = db.execute("""
                SELECT * FROM user_sections
                WHERE object_id=? AND user_id=? AND is_active=1 ORDER BY name
            """, (obj_id, user_id)).fetchall()
        else:
            rows = db.execute("""
                SELECT us.*, u.full_name as engineer_name FROM user_sections us
                JOIN users u ON u.id=us.user_id
                WHERE us.object_id=? AND us.is_active=1 ORDER BY us.name
            """, (obj_id,)).fetchall()
        return ok(rows_to_list(rows))

@app.post('/api/objects/<int:obj_id>/my_sections')
def add_my_section(obj_id):
    d = request.json or {}
    if not d.get('name') or not d.get('user_id'): return err('name и user_id обязательны')
    with db_conn() as db:
        cur = db.execute("INSERT INTO user_sections (object_id, user_id, name) VALUES (?,?,?) RETURNING id",
            (obj_id, d['user_id'], d['name']))
        _nid = cur.fetchone()['id']
        db.commit()
        row = db.execute("SELECT * FROM user_sections WHERE id=?", (_nid,)).fetchone()
        return ok(dict(row)), 201

@app.patch('/api/my_sections/<int:sec_id>')
def update_my_section(sec_id):
    d = request.json or {}
    with db_conn() as db:
        if 'name' in d:
            db.execute("UPDATE user_sections SET name=? WHERE id=?", (d['name'], sec_id))
        if 'is_active' in d:
            db.execute("UPDATE user_sections SET is_active=? WHERE id=?", (d['is_active'], sec_id))
        db.commit()
        return ok()

@app.delete('/api/my_sections/<int:sec_id>')
def delete_my_section(sec_id):
    with db_conn() as db:
        db.execute("UPDATE user_sections SET is_active=0 WHERE id=?", (sec_id,))
        db.commit()
        return ok()

# ─────────────────────────────────────────────────────────
# СВОДКИ — история инженера (только свои)
# ─────────────────────────────────────────────────────────

@app.get('/api/my_reports')
def my_reports():
    user_id = arg_int('user_id')
    if not user_id: return err('user_id обязателен')
    with db_conn() as db:
        # Только сводки по объектам, на которые инженер сейчас назначен
        rows = db.execute("""
            SELECT dr.*, o.name as object_name
            FROM daily_reports dr
            JOIN objects o ON o.id=dr.object_id
            WHERE dr.user_id=?
              AND dr.object_id IN (
                  SELECT object_id FROM object_users WHERE user_id=?
              )
            ORDER BY dr.report_date DESC
        """, (user_id, user_id)).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# СВОДКИ — все (только для Админа)
# ─────────────────────────────────────────────────────────

@app.get('/api/all_reports')
def all_reports():
    with db_conn() as db:
        requester_id = arg_int('requester_id')
        project_id = arg_int('project_id', NO_MATCH)
        object_id = arg_int('object_id', NO_MATCH)
        # Фильтр по инженеру называется engineer_id, а не user_id: имя user_id
        # перезаписывается из сессии защитой от подмены действующего
        # пользователя (_session_identity_priority), и фильтр обнулялся бы.
        engineer_id = arg_int('engineer_id', NO_MATCH)
        # Проверка прав: admin или senior с can_view_all=1
        if requester_id:
            req = db.execute("SELECT role, can_view_all FROM users WHERE id=?", (requester_id,)).fetchone()
            if not req or (req['role'] not in ALL_REPORTS_ROLES and not (req['role']=='senior' and req['can_view_all'])):
                return err('Доступ запрещён', 403)
        else:
            return err('Доступ запрещён', 403)
        query = """
            SELECT dr.*, u.full_name as engineer_name,
                   COALESCE(u.is_active,1) as engineer_active,
                   o.name as object_name, p.name as project_name
            FROM daily_reports dr
            JOIN users u ON u.id=dr.user_id
            JOIN objects o ON o.id=dr.object_id
            LEFT JOIN projects p ON p.id=o.project_id
            WHERE 1=1
        """
        params = []
        if project_id: query += " AND o.project_id=?"; params.append(project_id)
        if object_id: query += " AND o.id=?"; params.append(object_id)
        if engineer_id: query += " AND dr.user_id=?"; params.append(engineer_id)
        query += " ORDER BY dr.report_date DESC LIMIT 200"
        rows = db.execute(query, params).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# ЭКСПОРТ — ZIP архив со сводками и фото
# ─────────────────────────────────────────────────────────

@app.get('/api/admin/export_zip')
def export_zip():
    import traceback
    user_id = arg_int('user_id')
    project_id = arg_int('project_id', NO_MATCH)
    with db_conn() as db:
        # Проверка прав
        user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user['role'] not in ADMIN_ROLES:
            return err('Доступ запрещён', 403)
        try:
            return _export_zip_inner(db, user_id, project_id)
        except Exception as e:
            return err(f'Ошибка экспорта: {traceback.format_exc()}', 500)

def _export_zip_inner(db, user_id, project_id):
    import zipfile, io, csv, os

    # Загружаем сводки
    q = """SELECT dr.id, dr.report_date, dr.status, dr.submitted_at,
                  u.full_name as engineer, o.name as object_name,
                  COALESCE(p.name,'') as project_name
           FROM daily_reports dr
           JOIN users u ON u.id=dr.user_id
           JOIN objects o ON o.id=dr.object_id
           LEFT JOIN projects p ON p.id=o.project_id
           WHERE 1=1"""
    params = []
    if project_id: q += " AND o.project_id=?"; params.append(project_id)
    q += " ORDER BY dr.report_date DESC"
    reports = db.execute(q, params).fetchall()

    # Загружаем фото
    pq = """SELECT ph.file_path, ph.caption, u.full_name as engineer,
                   o.name as object_name, COALESCE(p.name,'') as project_name,
                   dr.report_date, dr.id as report_id
            FROM photos ph
            JOIN daily_reports dr ON dr.id=ph.report_id
            JOIN users u ON u.id=dr.user_id
            JOIN objects o ON o.id=dr.object_id
            LEFT JOIN projects p ON p.id=o.project_id
            WHERE 1=1"""
    pparams = []
    if project_id: pq += " AND o.project_id=?"; pparams.append(project_id)
    photos = db.execute(pq, pparams).fetchall()
    # db will be closed by the caller (export_zip db_conn context manager)

    UPLOAD_FOLDER = os.environ.get('SK_UPLOAD_DIR') or os.path.join(os.path.dirname(__file__), 'uploads')

    # Загружаем детали каждой сводки (контроль, персонал, замечания)
    report_details = {}
    for r in reports:
        det = get_db()
        try:
            ic  = det.execute("SELECT ic.*, s.name as section_name, c.name as contractor_name FROM input_control ic LEFT JOIN sections s ON s.id=ic.section_id LEFT JOIN contractors c ON c.id=ic.contractor_id WHERE ic.report_id=?", (r['id'],)).fetchall()
            oc  = det.execute("SELECT oc.*, s.name as section_name, c.name as contractor_name FROM operational_control oc LEFT JOIN sections s ON s.id=oc.section_id LEFT JOIN contractors c ON c.id=oc.contractor_id WHERE oc.report_id=?", (r['id'],)).fetchall()
            ac  = det.execute("SELECT ac.*, s.name as section_name, c.name as contractor_name FROM acceptance_control ac LEFT JOIN sections s ON s.id=ac.section_id LEFT JOIN contractors c ON c.id=ac.contractor_id WHERE ac.report_id=?", (r['id'],)).fetchall()
            pe  = det.execute("SELECT pe.*, c.name as contractor_name FROM personnel_entries pe LEFT JOIN contractors c ON c.id=pe.contractor_id WHERE pe.report_id=? AND pe.headcount>0", (r['id'],)).fetchall()
            rem = det.execute("SELECT * FROM verbal_remarks WHERE report_id=?", (r['id'],)).fetchall()
            ks2 = det.execute("SELECT * FROM ks2_check WHERE report_id=?", (r['id'],)).fetchall()
            report_details[r['id']] = {'ic': ic, 'oc': oc, 'ac': ac, 'pe': pe, 'rem': rem, 'ks2': ks2}
        except Exception:
            report_details[r['id']] = {}
        finally:
            det.close()

    def safe_path(s):
        return (s or '').replace('/', '-').replace('\\', '-').replace(':', '').strip()

    def eng_folder(full_name):
        # "Глуховской Александр Константинович" → "Глуховской АК"
        parts = (full_name or '').split()
        if len(parts) >= 3:
            return f"{parts[0]} {parts[1][0]}.{parts[2][0]}."
        return parts[0] if parts else full_name

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # — Сводки: сводный CSV (все инженеры)
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf, delimiter=';')
        writer.writerow(['Проект','Объект','Дата','Инженер','Статус','Дата сдачи','ID сводки'])
        for r in reports:
            status = 'Сдана' if r['status'] == 'submitted' else 'Черновик'
            writer.writerow([r['project_name'], r['object_name'], r['report_date'],
                             r['engineer'], status, r['submitted_at'] or '', r['id']])
        zf.writestr('Сводки/сводки.csv', '﻿' + csv_buf.getvalue())  # BOM для Excel

        # — Детальные TXT: Сводки/{Проект}/{Инженер}/{дата}_id{N}.txt
        for r in reports:
            proj  = safe_path(r['project_name'] or 'Без_проекта')
            eng   = safe_path(eng_folder(r['engineer']))
            fname = f"Сводки/{proj}/{eng}/{r['report_date']}_id{r['id']}.txt"

            sep = '=' * 54
            lines = [sep,
                     f"  СВОДКА #{r['id']}  ·  {r['report_date']}",
                     sep,
                     f"Инженер : {r['engineer']}",
                     f"Объект  : {r['object_name']}",
                     f"Проект  : {r['project_name'] or '—'}",
                     f"Статус  : {'Сдана' if r['status'] == 'submitted' else 'Черновик'}"]
            if r['submitted_at']:
                lines.append(f"Сдана   : {r['submitted_at'][:16]}")
            lines.append('')

            det = report_details.get(r['id'], {})

            if det.get('pe'):
                lines.append('── ПЕРСОНАЛ ' + '─' * 42)
                for p in det['pe']:
                    lines.append(f"  {p['contractor_name'] or '—'}  ·  {p['headcount']} чел.  ·  {p['work_description'] or ''}")
                lines.append('')

            if det.get('ic'):
                lines.append('── ВХОДНОЙ КОНТРОЛЬ ' + '─' * 34)
                for ic in det['ic']:
                    dev = ic['deviation_note']
                    status_ic = f"ОТКЛОНЕНИЕ: {dev}" if dev else 'Норма'
                    lines.append(f"  {ic['material_name'] or '—'}")
                    lines.append(f"    Кол-во: {ic['quantity'] or '—'}  ·  Документ: {ic['document_name'] or '—'}")
                    if ic['contractor_name']:
                        lines.append(f"    Подрядчик: {ic['contractor_name']}")
                    lines.append(f"    [{status_ic}]")
                lines.append('')

            if det.get('oc'):
                lines.append('── ОПЕРАЦИОННЫЙ КОНТРОЛЬ ' + '─' * 29)
                for oc in det['oc']:
                    lines.append(f"  {oc['work_stage'] or '—'}  [{oc['section_name'] or ''}]")
                    if oc['contractor_name']:
                        lines.append(f"    Подрядчик: {oc['contractor_name']}")
                    lines.append(f"    Операции: {oc['controlled_operations'] or '—'}")
                    lines.append(f"    Метод: {oc['control_method'] or '—'}")
                    if oc['deviation_note']:
                        lines.append(f"    ОТКЛОНЕНИЕ: {oc['deviation_note']}")
                lines.append('')

            if det.get('ac'):
                lines.append('── ПРИЁМОЧНЫЙ КОНТРОЛЬ ' + '─' * 31)
                for ac in det['ac']:
                    lines.append(f"  {ac['work_stage'] or '—'}  [{ac['section_name'] or ''}]")
                    if ac['contractor_name']:
                        lines.append(f"    Подрядчик: {ac['contractor_name']}")
                    if ac['deviation_note']:
                        lines.append(f"    ОТКЛОНЕНИЕ: {ac['deviation_note']}")
                lines.append('')

            if det.get('rem'):
                open_r  = [x for x in det['rem'] if x['status'] == 'open']
                close_r = [x for x in det['rem'] if x['status'] != 'open']
                lines.append(f"── ЗАМЕЧАНИЯ ({len(open_r)} открытых / {len(close_r)} закрытых) " + '─' * 20)
                for rm in det['rem']:
                    mark = '[ОТКРЫТО]' if rm['status'] == 'open' else '[ЗАКРЫТО]'
                    lines.append(f"  {mark} {rm['description']}")
                    if rm['deadline']:
                        lines.append(f"    Срок: {rm['deadline']}")
                lines.append('')

            if det.get('ks2'):
                lines.append('── ИД / КС-2 ' + '─' * 41)
                for k in det['ks2']:
                    ks6a = 'есть' if k['has_ks6a'] else 'нет'
                    idd  = 'есть' if k['has_id']   else 'нет'
                    lines.append(f"  {k['contractor_name'] or '—'}")
                    lines.append(f"    КС-2: {k['ks2_number'] or '—'}  ·  КС-6а: {ks6a}  ·  ИД: {idd}")
                lines.append('')

            zf.writestr(fname, '\n'.join(lines))

        # — Фото: Фотографии/{Проект}/{Инженер}/{дата}/{N:02}_{подпись}{ext}
        from collections import defaultdict
        photo_counters = defaultdict(int)
        for ph in photos:
            proj  = safe_path(ph['project_name'] or 'Без_проекта')
            eng   = safe_path(eng_folder(ph['engineer']))
            src   = os.path.join(UPLOAD_FOLDER, ph['file_path'])
            if not os.path.exists(src):
                continue
            ext = os.path.splitext(ph['file_path'])[1] or '.jpg'
            day_key = (proj, eng, ph['report_date'])
            photo_counters[day_key] += 1
            n   = photo_counters[day_key]
            cap = safe_path(ph['caption'] or '')[:40]
            cap_part = f"_{cap}" if cap else ''
            zname = f"Фотографии/{proj}/{eng}/{ph['report_date']}/{n:02d}{cap_part}{ext}"
            zf.write(src, zname)

    buf.seek(0)
    date_str = datetime.now().strftime('%Y%m%d_%H%M')
    proj_suffix = f"_project{project_id}" if project_id else ""
    fname = f"sk_export{proj_suffix}_{date_str}.zip"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype='application/zip')

@app.get('/api/admin/export_day')
def export_day():
    import traceback, zipfile, io, csv, os
    user_id   = arg_int('user_id')
    date_str  = request.args.get('date', '')
    if not date_str:
        return err('Параметр date обязателен')
    with db_conn() as db:
        user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user['role'] not in ADMIN_ROLES:
            return err('Доступ запрещён', 403)
        try:
            return _export_day_inner(db, date_str)
        except Exception:
            return err(f'Ошибка экспорта: {traceback.format_exc()}', 500)

def _export_day_inner(db, date_str):
    import zipfile, io, csv, os

    reports = db.execute("""
        SELECT dr.id, dr.report_date, dr.status, dr.submitted_at,
               u.full_name as engineer, o.name as object_name,
               COALESCE(p.name,'') as project_name
        FROM daily_reports dr
        JOIN users u ON u.id=dr.user_id
        JOIN objects o ON o.id=dr.object_id
        LEFT JOIN projects p ON p.id=o.project_id
        WHERE dr.report_date=?
        ORDER BY o.name, u.full_name
    """, (date_str,)).fetchall()

    photos = db.execute("""
        SELECT ph.file_path, ph.caption, o.name as object_name, dr.report_date
        FROM photos ph
        JOIN daily_reports dr ON dr.id=ph.report_id
        JOIN objects o ON o.id=dr.object_id
        WHERE dr.report_date=?
    """, (date_str,)).fetchall()

    UPLOAD_FOLDER = os.environ.get('SK_UPLOAD_DIR') or os.path.join(os.path.dirname(__file__), 'uploads')

    report_details = {}
    for r in reports:
        det = get_db()
        try:
            ic  = det.execute("SELECT ic.*, s.name as section_name, c.name as contractor_name FROM input_control ic LEFT JOIN sections s ON s.id=ic.section_id LEFT JOIN contractors c ON c.id=ic.contractor_id WHERE ic.report_id=?", (r['id'],)).fetchall()
            oc  = det.execute("SELECT oc.*, s.name as section_name, c.name as contractor_name FROM operational_control oc LEFT JOIN sections s ON s.id=oc.section_id LEFT JOIN contractors c ON c.id=oc.contractor_id WHERE oc.report_id=?", (r['id'],)).fetchall()
            ac  = det.execute("SELECT ac.*, s.name as section_name, c.name as contractor_name FROM acceptance_control ac LEFT JOIN sections s ON s.id=ac.section_id LEFT JOIN contractors c ON c.id=ac.contractor_id WHERE ac.report_id=?", (r['id'],)).fetchall()
            pe  = det.execute("SELECT pe.*, c.name as contractor_name FROM personnel_entries pe LEFT JOIN contractors c ON c.id=pe.contractor_id WHERE pe.report_id=? AND pe.headcount>0", (r['id'],)).fetchall()
            rem = det.execute("SELECT * FROM verbal_remarks WHERE report_id=?", (r['id'],)).fetchall()
            ks2 = det.execute("SELECT * FROM ks2_check WHERE report_id=?", (r['id'],)).fetchall()
            report_details[r['id']] = {'ic': ic, 'oc': oc, 'ac': ac, 'pe': pe, 'rem': rem, 'ks2': ks2}
        except Exception:
            report_details[r['id']] = {}
        finally:
            det.close()

    def safe_name(s):
        import re
        s = (s or '').strip()
        s = s.replace('«', '').replace('»', '').replace('"', '').replace("'", '')
        s = re.sub(r'[/\\:*?"<>|]', '-', s)
        s = re.sub(r'\s+', ' ', s).strip(' -')
        return s or 'Без названия'

    def eng_folder(full_name):
        parts = (full_name or '').split()
        if len(parts) >= 3:
            return f"{parts[0]} {parts[1][0]}.{parts[2][0]}."
        return parts[0] if parts else full_name

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if not reports:
            zf.writestr('нет_данных.txt', f'За {date_str} сводок не найдено.')
        else:
            # Сводный CSV в корень архива
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf, delimiter=';')
            writer.writerow(['Проект','Объект','Дата','Инженер','Статус','Дата сдачи','ID сводки'])
            for r in reports:
                status = 'Сдана' if r['status'] == 'submitted' else 'Черновик'
                writer.writerow([r['project_name'], r['object_name'], r['report_date'],
                                 r['engineer'], status, r['submitted_at'] or '', r['id']])
            zf.writestr(f'сводки_{date_str}.csv', '﻿' + csv_buf.getvalue())

            # Детальные TXT и фото по папкам объектов
            for r in reports:
                obj_folder = safe_name(r['object_name'])
                eng        = safe_name(eng_folder(r['engineer']))
                txt_path   = f"{obj_folder}/{eng}_id{r['id']}.txt"

                sep = '=' * 54
                lines = [sep,
                         f"  СВОДКА #{r['id']}  ·  {r['report_date']}",
                         sep,
                         f"Инженер : {r['engineer']}",
                         f"Объект  : {r['object_name']}",
                         f"Проект  : {r['project_name'] or '—'}",
                         f"Статус  : {'Сдана' if r['status'] == 'submitted' else 'Черновик'}"]
                if r['submitted_at']:
                    lines.append(f"Сдана   : {r['submitted_at'][:16]}")
                lines.append('')

                det = report_details.get(r['id'], {})

                if det.get('pe'):
                    lines.append('── ПЕРСОНАЛ ' + '─' * 42)
                    for p in det['pe']:
                        lines.append(f"  {p['contractor_name'] or '—'}  ·  {p['headcount']} чел.  ·  {p['work_description'] or ''}")
                    lines.append('')

                if det.get('ic'):
                    lines.append('── ВХОДНОЙ КОНТРОЛЬ ' + '─' * 34)
                    for ic in det['ic']:
                        dev = ic['deviation_note']
                        status_ic = f"ОТКЛОНЕНИЕ: {dev}" if dev else 'Норма'
                        lines.append(f"  {ic['material_name'] or '—'}")
                        lines.append(f"    Кол-во: {ic['quantity'] or '—'}  ·  Документ: {ic['document_name'] or '—'}")
                        if ic['contractor_name']:
                            lines.append(f"    Подрядчик: {ic['contractor_name']}")
                        lines.append(f"    [{status_ic}]")
                    lines.append('')

                if det.get('oc'):
                    lines.append('── ОПЕРАЦИОННЫЙ КОНТРОЛЬ ' + '─' * 29)
                    for oc in det['oc']:
                        lines.append(f"  {oc['work_stage'] or '—'}  [{oc['section_name'] or ''}]")
                        if oc['contractor_name']:
                            lines.append(f"    Подрядчик: {oc['contractor_name']}")
                        lines.append(f"    Операции: {oc['controlled_operations'] or '—'}")
                        lines.append(f"    Метод: {oc['control_method'] or '—'}")
                        if oc['deviation_note']:
                            lines.append(f"    ОТКЛОНЕНИЕ: {oc['deviation_note']}")
                    lines.append('')

                if det.get('ac'):
                    lines.append('── ПРИЁМОЧНЫЙ КОНТРОЛЬ ' + '─' * 31)
                    for ac in det['ac']:
                        lines.append(f"  {ac['work_stage'] or '—'}  [{ac['section_name'] or ''}]")
                        if ac['contractor_name']:
                            lines.append(f"    Подрядчик: {ac['contractor_name']}")
                        if ac['deviation_note']:
                            lines.append(f"    ОТКЛОНЕНИЕ: {ac['deviation_note']}")
                    lines.append('')

                if det.get('rem'):
                    open_r  = [x for x in det['rem'] if x['status'] == 'open']
                    close_r = [x for x in det['rem'] if x['status'] != 'open']
                    lines.append(f"── ЗАМЕЧАНИЯ ({len(open_r)} открытых / {len(close_r)} закрытых) " + '─' * 20)
                    for rm in det['rem']:
                        mark = '[ОТКРЫТО]' if rm['status'] == 'open' else '[ЗАКРЫТО]'
                        lines.append(f"  {mark} {rm['description']}")
                        if rm['deadline']:
                            lines.append(f"    Срок: {rm['deadline']}")
                    lines.append('')

                if det.get('ks2'):
                    lines.append('── ИД / КС-2 ' + '─' * 41)
                    for k in det['ks2']:
                        ks6a = 'есть' if k['has_ks6a'] else 'нет'
                        idd  = 'есть' if k['has_id']   else 'нет'
                        lines.append(f"  {k['contractor_name'] or '—'}")
                        lines.append(f"    КС-2: {k['ks2_number'] or '—'}  ·  КС-6а: {ks6a}  ·  ИД: {idd}")
                    lines.append('')

                zf.writestr(txt_path, '\n'.join(lines))

            from collections import defaultdict
            photo_counters = defaultdict(int)
            for ph in photos:
                obj_folder = safe_name(ph['object_name'])
                src = os.path.join(UPLOAD_FOLDER, ph['file_path'])
                if not os.path.exists(src):
                    continue
                ext = os.path.splitext(ph['file_path'])[1] or '.jpg'
                photo_counters[obj_folder] += 1
                n   = photo_counters[obj_folder]
                cap = safe_name(ph['caption'] or '')[:40]
                cap_part = f"_{cap}" if cap else ''
                zf.write(src, f"{obj_folder}/фото/{n:02d}{cap_part}{ext}")

    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"export_{date_str}.zip",
                     mimetype='application/zip')

# ─────────────────────────────────────────────────────────
# ФОТО — управление (Админ: все + удаление; Инженер: свои)
# ─────────────────────────────────────────────────────────

@app.get('/api/all_photos')
def all_photos():
    """Все фото — для admin или senior с can_view_all=1"""
    with db_conn() as db:
        requester_id = arg_int('requester_id')
        project_id = arg_int('project_id', NO_MATCH)
        object_id = arg_int('object_id', NO_MATCH)
        # Проверка прав: admin или senior с can_view_all=1
        if requester_id:
            req = db.execute("SELECT role, can_view_all FROM users WHERE id=?", (requester_id,)).fetchone()
            if not req or (req['role'] not in ALL_REPORTS_ROLES and not (req['role']=='senior' and req['can_view_all'])):
                return err('Доступ запрещён', 403)
        else:
            return err('Доступ запрещён', 403)
        query = """
            SELECT ph.*, u.full_name as engineer_name,
                   o.name as object_name, dr.report_date
            FROM photos ph
            JOIN daily_reports dr ON dr.id=ph.report_id
            JOIN users u ON u.id=dr.user_id
            JOIN objects o ON o.id=dr.object_id
            WHERE 1=1
        """
        params = []
        if project_id: query += " AND o.project_id=?"; params.append(project_id)
        if object_id: query += " AND o.id=?"; params.append(object_id)
        query += " ORDER BY ph.uploaded_at DESC"
        rows = db.execute(query, params).fetchall()
        return ok(rows_to_list(rows))

@app.get('/api/my_photos')
def my_photos():
    """Фото инженера"""
    user_id = arg_int('user_id')
    if not user_id: return err('user_id обязателен')
    with db_conn() as db:
        rows = db.execute("""
            SELECT ph.*, o.name as object_name, dr.report_date
            FROM photos ph
            JOIN daily_reports dr ON dr.id=ph.report_id
            JOIN objects o ON o.id=dr.object_id
            WHERE dr.user_id=?
            ORDER BY ph.uploaded_at DESC
        """, (user_id,)).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# ОБНОВЛЕНИЕ SCHEMA ДЛЯ RENDER (миграция при старте)
# ─────────────────────────────────────────────────────────

@app.post('/api/migrate_v2')
def migrate_v2():
    user_id = arg_int('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] not in ADMIN_ROLES:
            return err('Доступ запрещён', 403)
        if IS_POSTGRES:
            return ok('Не требуется: схема PostgreSQL создаётся из db/schema_postgres.sql')
        try:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, description TEXT,
                tj_project_id TEXT, is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS partners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, type TEXT, address TEXT,
                contact_name TEXT, contact_role TEXT, inn TEXT,
                phone TEXT, email TEXT, notes TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS user_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """)
            cols = [c[1] for c in db.execute("PRAGMA table_info(objects)").fetchall()]
            if 'project_id' not in cols:
                db.execute("ALTER TABLE objects ADD COLUMN project_id INTEGER")
            cols_m = [c[1] for c in db.execute("PRAGMA table_info(meetings)").fetchall()]
            if 'protocol_file' not in cols_m:
                db.execute("ALTER TABLE meetings ADD COLUMN protocol_file TEXT")
            # Create default projects from existing objects
            cnt = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            if cnt == 0:
                db.execute("INSERT INTO projects (name) VALUES ('ЖК «Окла»')")
                p1 = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                db.execute("INSERT INTO projects (name) VALUES ('IQ Гатчина')")
                p2 = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                db.execute("UPDATE objects SET project_id=? WHERE name LIKE '%Окла%'", (p1,))
                db.execute("UPDATE objects SET project_id=? WHERE name LIKE '%Гатчина%'", (p2,))
            db.commit()
            return ok('Миграция v2 выполнена')
        except Exception as e:
            return err(str(e))


# ─────────────────────────────────────────────────────────
# РЕЗЕРВНАЯ КОПИЯ БД (только для администратора)
# ─────────────────────────────────────────────────────────

def _backup_postgres():
    """Резервная копия PostgreSQL через pg_dump в формате custom (-Fc).

    Пароль передаётся переменной окружения PGPASSWORD и не попадает
    ни в аргументы команды (их видно в ps), ни в логи, ни в ответ.
    Путь к pg_dump можно задать через SK_PG_DUMP — у системного
    пользователя сервиса бинарник не всегда есть в PATH.
    """
    import subprocess, tempfile
    from db.schema import pg_params

    p = pg_params()
    dump_bin = os.environ.get('SK_PG_DUMP') or 'pg_dump'

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.dump')
    tmp.close()

    env = os.environ.copy()
    if p['password']:
        env['PGPASSWORD'] = p['password']

    cmd = [dump_bin, '-Fc', '--no-password',
           '-h', p['host'], '-p', str(p['port']),
           '-U', p['user'], '-d', p['dbname'],
           '-f', tmp.name]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=600)
    except FileNotFoundError:
        os.unlink(tmp.name)
        return err(f'Не найдена программа pg_dump ({dump_bin}). Установите '
                   'postgresql-client или задайте путь в SK_PG_DUMP', 500)
    except subprocess.TimeoutExpired:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        return err('pg_dump не завершился за 10 минут, копия не создана', 504)

    if proc.returncode != 0:
        # stderr безопасен: пароль ушёл через окружение, а не аргументом
        detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()[:400]
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        return err(f'pg_dump завершился с ошибкой: {detail or "код " + str(proc.returncode)}', 500)

    # Пустой файл — это не копия. Лучше честная ошибка, чем ложный успех.
    if not os.path.exists(tmp.name) or os.path.getsize(tmp.name) == 0:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        return err('pg_dump отработал, но файл копии пуст — копия не создана', 500)

    date_str = datetime.now().strftime('%Y-%m-%d')
    return send_file(tmp.name, as_attachment=True,
                     download_name=f'sk_pilot_{date_str}.dump',
                     mimetype='application/octet-stream')


@app.get('/api/admin/backup_db')
def backup_db():
    import shutil, tempfile
    from db.schema import DB_PATH
    user_id = arg_int('user_id')
    # Проверяем что запрашивает администратор
    with db_conn() as db:
        user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user['role'] not in ADMIN_ROLES:
            return err('Доступ запрещён', 403)
    if IS_POSTGRES:
        return _backup_postgres()
    # Копируем БД во временный файл чтобы не блокировать основную
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    shutil.copy2(DB_PATH, tmp.name)
    tmp.close()
    date_str = datetime.now().strftime('%Y%m%d_%H%M')
    return send_file(tmp.name, as_attachment=True,
                     download_name=f'sk_pilot_backup_{date_str}.db',
                     mimetype='application/octet-stream')


if __name__ == '__main__':
    init_db()
    print("🚀 Сервер запущен: http://localhost:5000")
    print("📋 API документация: http://localhost:5000/api/objects")
    app.run(debug=False, host='0.0.0.0', port=5001)

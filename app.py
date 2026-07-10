# SK-pilot (СК-пилот) — система ежедневных сводок строительного контроля.
# Автор: Vladislav Nikonenko (идея и разработка). © 2026. Версия 1.3.

APP_VERSION = '1.3'

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from contextlib import contextmanager
import os, sys, hashlib, uuid
from datetime import datetime, date

try:
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

sys.path.insert(0, os.path.dirname(__file__))
from db.schema import get_db, init_db

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

# Auto-migrate: создать таблицы и добавить новые колонки если их нет
def auto_migrate():
    db = get_db()
    try:
        # Создаём таблицы, которые могут отсутствовать в старых БД
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
    finally:
        db.close()

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
    Идемпотентна: INSERT OR IGNORE по UNIQUE(partner_id, project_id)."""
    rows = db.execute("SELECT id, name, project_id FROM partners").fetchall()
    with_proj = [r for r in rows if r['project_id'] is not None]
    without_proj = [r for r in rows if r['project_id'] is None]
    migrated = 0
    for r in with_proj:
        cur = db.execute(
            "INSERT OR IGNORE INTO partner_projects (partner_id, project_id) VALUES (?,?)",
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

@app.get('/')
def index():
    return send_from_directory('static', 'login.html')

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
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
    user_id = request.args.get('user_id')
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
        rows = db.execute("SELECT * FROM objects WHERE is_active=1 ORDER BY name").fetchall()
        return ok(rows_to_list(rows))

@app.post('/api/objects')
def create_object():
    d = request.json or {}
    if not d.get('name'):
        return err('name обязателен')
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO objects (name, address, client_name, contract_number, tj_object_id, project_id) VALUES (?,?,?,?,?,?)",
            (d['name'], d.get('address'), d.get('client_name'), d.get('contract_number'), d.get('tj_object_id'), d.get('project_id'))
        )
        db.commit()
        row = db.execute("SELECT * FROM objects WHERE id=?", (cur.lastrowid,)).fetchone()
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
        cur = db.execute("INSERT INTO sections (object_id, name) VALUES (?,?)", (obj_id, d['name']))
        db.commit()
        row = db.execute("SELECT * FROM sections WHERE id=?", (cur.lastrowid,)).fetchone()
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
            "INSERT INTO contractors (object_id, name, work_type) VALUES (?,?,?)",
            (obj_id, d['name'], d.get('work_type'))
        )
        db.commit()
        row = db.execute("SELECT * FROM contractors WHERE id=?", (cur.lastrowid,)).fetchone()
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
        allowed = ['full_name', 'email', 'role', 'is_active', 'can_view_all']
        updates = {k: v for k, v in d.items() if k in allowed}
        if updates:
            sql = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE users SET {sql} WHERE id=?", list(updates.values()) + [user_id])
            db.commit()
        return ok()

@app.post('/api/users')
def create_user():
    d = request.json or {}
    if not d.get('email') or not d.get('full_name'):
        return err('email и full_name обязательны')
    pwd = d.get('password', 'changeme')
    pwd_hash = hashlib.sha256(pwd.encode()).hexdigest()
    with db_conn() as db:
        try:
            cur = db.execute(
                "INSERT INTO users (full_name, email, role, password_hash, tj_user_id) VALUES (?,?,?,?,?)",
                (d['full_name'], d['email'], d.get('role', 'engineer'), pwd_hash, d.get('tj_user_id'))
            )
            db.commit()
            row = db.execute("SELECT id, full_name, email, role FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            return ok(dict(row)), 201
        except Exception as e:
            return err(f'Email уже существует: {e}')

@app.post('/api/objects/<int:obj_id>/assign_user')
def assign_user(obj_id):
    d = request.json or {}
    if not d.get('user_id'):
        return err('user_id обязателен')
    with db_conn() as db:
        try:
            db.execute(
                "INSERT OR REPLACE INTO object_users (object_id, user_id, date_from) VALUES (?,?,?)",
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
                "INSERT INTO daily_reports (object_id, user_id, report_date) VALUES (?,?,?)",
                (d['object_id'], d['user_id'], d['report_date'])
            )
            db.commit()
            row = db.execute("SELECT * FROM daily_reports WHERE id=?", (cur.lastrowid,)).fetchone()
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
            "INSERT INTO input_control (report_id, material_name, quantity, document_name, section_id, status, deviation_note, engineer_id, contractor_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, d.get('material_name'), d.get('quantity'), d.get('document_name'), d.get('section_id'), d.get('status',''), d.get('deviation_note',''), d.get('engineer_id'), d.get('contractor_id'))
        )
        db.commit()
        row = db.execute("SELECT ic.*, s.name as section_name, c.name as contractor_name FROM input_control ic LEFT JOIN sections s ON s.id=ic.section_id LEFT JOIN contractors c ON c.id=ic.contractor_id WHERE ic.id=?", (cur.lastrowid,)).fetchone()
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
            "INSERT INTO verbal_remarks (report_id, section_id, description, deadline, issued_by) VALUES (?,?,?,?,?)",
            (report_id, d.get('section_id'), d['description'], d.get('deadline'), d.get('issued_by'))
        )
        db.commit()
        row = db.execute("SELECT * FROM verbal_remarks WHERE id=?", (cur.lastrowid,)).fetchone()
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
            "INSERT INTO prescriptions_log (report_id, tj_prescription_id, number, issue_date, section_id, deadline, status) VALUES (?,?,?,?,?,?,?)",
            (report_id, d.get('tj_prescription_id'), d.get('number'), d.get('issue_date'), d.get('section_id'), d.get('deadline'), d.get('status'))
        )
        db.commit()
        row = db.execute("SELECT * FROM prescriptions_log WHERE id=?", (cur.lastrowid,)).fetchone()
        return ok(dict(row)), 201

# ─────────────────────────────────────────────────────────
# ОПЕРАЦИОННЫЙ КОНТРОЛЬ
# ─────────────────────────────────────────────────────────

@app.post('/api/reports/<int:report_id>/operational_control')
def add_operational_control(report_id):
    d = request.json or {}
    with db_conn() as db:
        cur = db.execute(
            "INSERT INTO operational_control (report_id, section_id, work_stage, controlled_operations, control_method, status, deviation_note, engineer_id, contractor_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, d.get('section_id'), d.get('work_stage'), d.get('controlled_operations'), d.get('control_method'), d.get('status',''), d.get('deviation_note',''), d.get('engineer_id'), d.get('contractor_id'))
        )
        db.commit()
        row = db.execute("SELECT oc.*, s.name as section_name, c.name as contractor_name FROM operational_control oc LEFT JOIN sections s ON s.id=oc.section_id LEFT JOIN contractors c ON c.id=oc.contractor_id WHERE oc.id=?", (cur.lastrowid,)).fetchone()
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
            "INSERT INTO meetings (report_id, location, time, participants, agenda, engineer_id) VALUES (?,?,?,?,?,?)",
            (report_id, d.get('location'), d.get('time'), d.get('participants'), d.get('agenda'), d.get('engineer_id'))
        )
        db.commit()
        row = db.execute("SELECT * FROM meetings WHERE id=?", (cur.lastrowid,)).fetchone()
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
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM photos WHERE report_id=?",
            (report_id,)
        ).fetchone()[0]
        cur = db.execute(
            "INSERT INTO photos (report_id, file_path, caption, sort_order, remark_id) VALUES (?,?,?,?,?)",
            (report_id, filename, caption, sort_order, remark_id)
        )
        db.commit()
        row = db.execute("SELECT * FROM photos WHERE id=?", (cur.lastrowid,)).fetchone()
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
    user_id = request.args.get('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] != 'admin':
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
            "INSERT INTO acceptance_control (report_id, section_id, work_stage, controlled_operations, control_method, status, deviation_note, engineer_id, contractor_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, d.get('section_id'), d.get('work_stage'), d.get('controlled_operations'), d.get('control_method'), d.get('status',''), d.get('deviation_note',''), d.get('engineer_id'), d.get('contractor_id'))
        )
        db.commit()
        row = db.execute("SELECT ac.*, s.name as section_name, c.name as contractor_name FROM acceptance_control ac LEFT JOIN sections s ON s.id=ac.section_id LEFT JOIN contractors c ON c.id=ac.contractor_id WHERE ac.id=?", (cur.lastrowid,)).fetchone()
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
            "INSERT INTO ks2_check (report_id, contractor_name, object_work, ks2_number, ks3_number, has_ks3, has_ks6a, has_id, engineer_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, d.get('contractor_name'), d.get('object_work'), d.get('ks2_number'),
             d.get('ks3_number'), 1 if d.get('has_ks3') else 0,
             1 if d.get('has_ks6a') else 0, 1 if d.get('has_id') else 0, d.get('engineer_id'))
        )
        db.commit()
        row = db.execute("SELECT * FROM ks2_check WHERE id=?", (cur.lastrowid,)).fetchone()
        return ok(dict(row)), 201

@app.delete('/api/ks2_check/<int:ks2_id>')
def delete_ks2_check(ks2_id):
    with db_conn() as db:
        db.execute("DELETE FROM ks2_check WHERE id=?", (ks2_id,))
        db.commit()
        return ok()


@app.post('/api/migrate')
def run_migration():
    user_id = request.args.get('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] != 'admin':
            return err('Доступ запрещён', 403)
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
            db.execute("INSERT OR IGNORE INTO users (full_name,email,role,tj_user_id,password_hash) VALUES ('Ухов Илья Викторович','uhov@stroymanager.ru','engineer','uhov_tj_001','"+pwd+"')")
            db.execute("INSERT OR IGNORE INTO objects (name,address,client_name,tj_object_id) VALUES ('IQ Гатчина (участок 6)','Ленинградская обл., г. Гатчина','ЛСТ Генподряд','tj_gatchina_006')")
            gid = db.execute("SELECT id FROM objects WHERE tj_object_id='tj_gatchina_006'").fetchone()[0]
            uid = db.execute("SELECT id FROM users WHERE email='uhov@stroymanager.ru'").fetchone()[0]
            aid_row = db.execute("SELECT id FROM users WHERE role='admin'").fetchone()
            aid = aid_row[0] if aid_row else None
            for name in ['Пятно застройки','Блок 3','ПОС']:
                db.execute("INSERT OR IGNORE INTO sections (object_id,name) VALUES (?,?)",(gid,name))
            for name,wt in [('ООО Гелиос','ПОС, замещение грунта'),('ООО Фортес','Лидерное бурение, сваи')]:
                db.execute("INSERT OR IGNORE INTO contractors (object_id,name,work_type) VALUES (?,?,?)",(gid,name,wt))
            db.execute("INSERT OR IGNORE INTO object_users (object_id,user_id) VALUES (?,?)",(gid,uid))
            if aid: db.execute("INSERT OR IGNORE INTO object_users (object_id,user_id) VALUES (?,?)",(gid,aid))
            db.commit()
            return ok('Миграция выполнена успешно')
        except Exception as e:
            return err(str(e))


@app.post('/api/fix_duplicates')
def fix_duplicates():
    user_id = request.args.get('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] != 'admin':
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
        cur = db.execute("INSERT INTO projects (name, description, tj_project_id) VALUES (?,?,?)",
            (d['name'], d.get('description'), d.get('tj_project_id')))
        db.commit()
        row = db.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
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
        db.execute("INSERT OR IGNORE INTO partner_projects (partner_id, project_id) VALUES (?,?)",
                   (partner_id, pid))
    compat_project_id = project_ids[0] if project_ids else None
    db.execute("UPDATE partners SET project_id=? WHERE id=?", (compat_project_id, partner_id))

@app.get('/api/partners')
def list_partners():
    with db_conn() as db:
        project_id = request.args.get('project_id')
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
            "INSERT INTO partners (name, type, address, contact_name, contact_role, inn, phone, email, notes, work_type, project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (d['name'], d.get('type'), d.get('address'), d.get('contact_name'),
             d.get('contact_role'), d.get('inn'), d.get('phone'), d.get('email'),
             d.get('notes'), d.get('work_type'), compat_pid))
        pid = cur.lastrowid
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
    user_id = request.args.get('user_id')
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
        cur = db.execute("INSERT INTO user_sections (object_id, user_id, name) VALUES (?,?,?)",
            (obj_id, d['user_id'], d['name']))
        db.commit()
        row = db.execute("SELECT * FROM user_sections WHERE id=?", (cur.lastrowid,)).fetchone()
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
    user_id = request.args.get('user_id')
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
        requester_id = request.args.get('requester_id')
        project_id = request.args.get('project_id')
        object_id = request.args.get('object_id')
        user_id = request.args.get('user_id')
        # Проверка прав: admin или senior с can_view_all=1
        if requester_id:
            req = db.execute("SELECT role, can_view_all FROM users WHERE id=?", (requester_id,)).fetchone()
            if not req or (req['role'] not in ('admin',) and not (req['role']=='senior' and req['can_view_all'])):
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
        if user_id: query += " AND dr.user_id=?"; params.append(user_id)
        query += " ORDER BY dr.report_date DESC LIMIT 200"
        rows = db.execute(query, params).fetchall()
        return ok(rows_to_list(rows))

# ─────────────────────────────────────────────────────────
# ЭКСПОРТ — ZIP архив со сводками и фото
# ─────────────────────────────────────────────────────────

@app.get('/api/admin/export_zip')
def export_zip():
    import traceback
    user_id = request.args.get('user_id')
    project_id = request.args.get('project_id')
    with db_conn() as db:
        # Проверка прав
        user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user['role'] != 'admin':
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

    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')

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

# ─────────────────────────────────────────────────────────
# ФОТО — управление (Админ: все + удаление; Инженер: свои)
# ─────────────────────────────────────────────────────────

@app.get('/api/all_photos')
def all_photos():
    """Все фото — для admin или senior с can_view_all=1"""
    with db_conn() as db:
        requester_id = request.args.get('requester_id')
        project_id = request.args.get('project_id')
        object_id = request.args.get('object_id')
        # Проверка прав: admin или senior с can_view_all=1
        if requester_id:
            req = db.execute("SELECT role, can_view_all FROM users WHERE id=?", (requester_id,)).fetchone()
            if not req or (req['role'] not in ('admin',) and not (req['role']=='senior' and req['can_view_all'])):
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
    user_id = request.args.get('user_id')
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
    user_id = request.args.get('user_id')
    with db_conn() as db:
        u = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u or u['role'] != 'admin':
            return err('Доступ запрещён', 403)
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

@app.get('/api/admin/backup_db')
def backup_db():
    import shutil, tempfile
    from db.schema import DB_PATH
    user_id = request.args.get('user_id')
    # Проверяем что запрашивает администратор
    with db_conn() as db:
        user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user['role'] != 'admin':
            return err('Доступ запрещён', 403)
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

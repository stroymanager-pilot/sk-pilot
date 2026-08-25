"""Общие фикстуры автотестов. Работают на SQLite и на PostgreSQL.

Выбор базы — переменной окружения SK_DB_TYPE, как и в приложении:

    pytest                          # SQLite (по умолчанию)
    SK_DB_TYPE=postgres pytest      # PostgreSQL

ВАЖНО про изоляцию: переменные окружения выставляются ДО импорта app,
потому что app.py на уровне модуля вызывает init_db() и auto_migrate().
Для SQLite путь берётся из SK_DB_PATH, каталог загрузок — из SK_UPLOAD_DIR,
поэтому боевая база тестам недоступна в принципе. Ниже стоят проверки,
которые роняют прогон, если база оказалась не тестовой.
"""
import hashlib
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Изоляция: временная база и каталог загрузок ──────────────────────────
_TMP = pathlib.Path(tempfile.mkdtemp(prefix='sk-tests-'))
_DB = _TMP / 'test.db'
_BASELINE = _TMP / 'baseline.db'

os.environ.setdefault('SK_DB_PATH', str(_DB))
os.environ['SK_UPLOAD_DIR'] = str(_TMP / 'uploads')
os.environ['SK_SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['SK_COOKIE_INSECURE'] = '1'

# Для PostgreSQL — отдельная тестовая база, никогда не боевая
if (os.environ.get('SK_DB_TYPE') or '').lower().startswith('p'):
    os.environ.setdefault('SK_PG_HOST', 'localhost')
    os.environ.setdefault('SK_PG_PORT', '55432')
    os.environ.setdefault('SK_PG_DB', 'sk_test')
    os.environ.setdefault('SK_PG_USER', 'sk')
    os.environ.setdefault('SK_PG_PASSWORD', 'test')

import app as sk_app  # noqa: E402  — импорт строго после настройки окружения
from db.schema import DB_PATH, IS_POSTGRES, get_db  # noqa: E402

if IS_POSTGRES:
    # Страховка: тестовая база должна называться явно тестовой
    _pg_db = os.environ.get('SK_PG_DB', '')
    assert 'test' in _pg_db.lower(), (
        f'Имя базы PostgreSQL не похоже на тестовое: {_pg_db!r}. Прогон остановлен.'
    )
else:
    # Страховка: если приложение всё же смотрит на боевую базу — не запускаемся
    assert pathlib.Path(DB_PATH).resolve() == _DB.resolve(), (
        f'Тесты смотрят не на тестовую базу: {DB_PATH}. Прогон остановлен.'
    )
    assert 'sk-tests-' in str(DB_PATH), 'Путь к базе вне временной папки'

# ── Учётные записи, известные тестам ─────────────────────────────────────
PWD = {
    'root': 'RootParol123',
    'admin': 'AdminParol123',
    'senior': 'SeniorParol123',
    'senior_noview': 'Senior2Parol123',
    'engineer': 'EngParol123',
}
UID = {
    'root': 1, 'admin': 2, 'senior': 3, 'senior_noview': 4,
    'engineer': 5, 'legacy': 6, 'archived': 7,
}
EMAIL = {
    'root': 'root@test.ru', 'admin': 'admin@test.ru', 'senior': 'senior@test.ru',
    'senior_noview': 'senior2@test.ru', 'engineer': 'eng@test.ru',
    'legacy': 'legacy@test.ru', 'archived': 'arch@test.ru',
}
# Тестовый пароль из старой схемы: sha256 без '$' — паролем НЕ считается
LEGACY_HASH = hashlib.sha256(b'password123').hexdigest()

OBJ_IN_ACTIVE_PROJECT = 1
OBJ_SECOND = 2
OBJ_IN_INACTIVE_PROJECT = 3
PROJECT_ACTIVE = 1
PROJECT_INACTIVE = 2
PARTNER = 1

# Все таблицы схемы — для очистки перед посевом в PostgreSQL
TABLES = [
    'photos', 'meetings', 'prescriptions_log', 'verbal_remarks', 'ks2_check',
    'acceptance_control', 'operational_control', 'input_control',
    'personnel_entries', 'daily_reports', 'user_sections', 'object_users',
    'contractors', 'sections', 'objects', 'partner_projects', 'partners',
    'projects', 'users', 'organizations',
]


def _seed():
    """Наполняет пустую базу предсказуемыми данными.

    Работает через слой совместимости приложения, поэтому один и тот же
    код с плейсхолдерами '?' исполняется и в SQLite, и в PostgreSQL.
    Идентификаторы задаются явно — тесты на них ссылаются.
    """
    from werkzeug.security import generate_password_hash
    # PostgreSQL требует явного разрешения на запись в колонку identity
    ovr = 'OVERRIDING SYSTEM VALUE ' if IS_POSTGRES else ''
    db = get_db()
    try:
        if IS_POSTGRES:
            db.execute('TRUNCATE TABLE ' + ', '.join(TABLES) + ' RESTART IDENTITY CASCADE')
        else:
            for t in ('users', 'projects', 'objects', 'object_users',
                      'sections', 'partners', 'partner_projects', 'organizations'):
                db.execute(f'DELETE FROM {t}')

        db.execute(f"INSERT INTO organizations (id, name, is_active) {ovr}VALUES (1,'Стройменеджер',1)")

        def user(uid, key, role, active=1, can_view=0, real_pwd=True):
            h = generate_password_hash(PWD[key]) if real_pwd else LEGACY_HASH
            db.execute(
                "INSERT INTO users (id, full_name, email, role, password_hash, "
                f"is_active, can_view_all, organization_id) {ovr}VALUES (?,?,?,?,?,?,?,1)",
                (uid, f'Тестовый {key}', EMAIL[key], role, h, active, can_view))

        user(UID['root'], 'root', 'root')
        user(UID['admin'], 'admin', 'admin')
        user(UID['senior'], 'senior', 'senior', can_view=1)
        user(UID['senior_noview'], 'senior_noview', 'senior', can_view=0)
        user(UID['engineer'], 'engineer', 'engineer')
        # Пользователь со старым sha256-хешем: входит только выбором из списка
        db.execute("INSERT INTO users (id, full_name, email, role, password_hash, "
                   f"is_active, can_view_all, organization_id) {ovr}VALUES (?,?,?,?,?,1,0,1)",
                   (UID['legacy'], 'Тестовый legacy', EMAIL['legacy'], 'engineer', LEGACY_HASH))
        # Архивный инженер
        db.execute("INSERT INTO users (id, full_name, email, role, password_hash, "
                   f"is_active, can_view_all, organization_id) {ovr}VALUES (?,?,?,?,?,0,0,1)",
                   (UID['archived'], 'Тестовый архивный', EMAIL['archived'], 'engineer', LEGACY_HASH))

        db.execute(f"INSERT INTO projects (id, name, is_active, organization_id) {ovr}VALUES (1,'Проект А',1,1)")
        db.execute(f"INSERT INTO projects (id, name, is_active, organization_id) {ovr}VALUES (2,'Проект Б (архив)',0,1)")

        db.execute("INSERT INTO objects (id, name, address, client_name, project_id, is_active) "
                   f"{ovr}VALUES (1,'Объект 1','Адрес 1','Заказчик 1',1,1)")
        db.execute(f"INSERT INTO objects (id, name, project_id, is_active) {ovr}VALUES (2,'Объект 2',1,1)")
        db.execute(f"INSERT INTO objects (id, name, project_id, is_active) {ovr}VALUES (3,'Объект 3',2,1)")

        db.execute("INSERT INTO object_users (object_id, user_id) VALUES (1,?)", (UID['engineer'],))
        db.execute("INSERT INTO object_users (object_id, user_id) VALUES (1,?)", (UID['senior'],))

        db.execute(f"INSERT INTO sections (id, object_id, name, is_active) {ovr}VALUES (1,1,'Корпус 1',1)")

        db.execute("INSERT INTO partners (id, name, type, work_type, is_active, organization_id) "
                   f"{ovr}VALUES (?,'ООО Партнёр','Субподрядчик','Монолитные работы',1,1)", (PARTNER,))
        db.execute("INSERT INTO partner_projects (partner_id, project_id) VALUES (?,?)",
                   (PARTNER, PROJECT_ACTIVE))
        db.commit()

        if IS_POSTGRES:
            _sync_sequences(db)
            db.commit()
    finally:
        db.close()


def _sync_sequences(db):
    """Выравнивает счётчики identity после вставки строк с явными id."""
    for t in TABLES:
        db.execute(
            "SELECT setval(pg_get_serial_sequence(?, 'id'), "
            "COALESCE((SELECT MAX(id) FROM " + t + "), 1))", (t,))


@pytest.fixture(scope='session', autouse=True)
def _baseline():
    """Готовит базу один раз и убирает временные файлы в конце."""
    _seed()
    if not IS_POSTGRES:
        shutil.copy2(_DB, _BASELINE)
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture
def client():
    """Свежий клиент на восстановленной базе — тесты не влияют друг на друга."""
    if IS_POSTGRES:
        _seed()                       # TRUNCATE + повторный посев
    else:
        shutil.copy2(_BASELINE, _DB)  # мгновенный откат файла
    sk_app.app.config['TESTING'] = True
    with sk_app.app.test_client() as c:
        yield c


@pytest.fixture
def db():
    """Прямое соединение с тестовой базой — для проверки состояния."""
    if IS_POSTGRES:
        conn = get_db()
        yield conn
        conn.close()
    else:
        conn = sqlite3.connect(_DB)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()


# ── Помощники входа ─────────────────────────────────────────────────────
def login_password(client, key):
    return client.post('/api/auth/login',
                       json={'email': EMAIL[key], 'password': PWD[key]})


def login_legacy(client, uid):
    return client.post('/api/auth/login_legacy', json={'user_id': uid})


def as_role(client, key):
    """Вход по паролю с проверкой, что он удался."""
    r = login_password(client, key)
    assert r.status_code == 200, f'не удалось войти как {key}: {r.get_json()}'
    return client

"""Общие фикстуры автотестов.

ВАЖНО про изоляцию: переменные окружения выставляются ДО импорта app,
потому что app.py на уровне модуля вызывает init_db() и auto_migrate().
Путь к базе берётся из SK_DB_PATH, каталог загрузок — из SK_UPLOAD_DIR,
поэтому боевая база тестам недоступна в принципе. Ниже стоит проверка,
которая роняет прогон, если база вдруг оказалась вне временной папки.
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

os.environ['SK_DB_PATH'] = str(_DB)
os.environ['SK_UPLOAD_DIR'] = str(_TMP / 'uploads')
os.environ['SK_SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['SK_COOKIE_INSECURE'] = '1'

import app as sk_app  # noqa: E402  — импорт строго после настройки окружения
from db.schema import DB_PATH  # noqa: E402

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


def _seed(path):
    """Наполняет пустую (уже промигрированную) базу предсказуемыми данными."""
    from werkzeug.security import generate_password_hash
    db = sqlite3.connect(path)
    db.executescript("DELETE FROM users; DELETE FROM projects; DELETE FROM objects;")

    def user(uid, key, role, active=1, can_view=0, real_pwd=True):
        h = generate_password_hash(PWD[key]) if real_pwd else LEGACY_HASH
        db.execute(
            "INSERT INTO users (id, full_name, email, role, password_hash, "
            "is_active, can_view_all, organization_id) VALUES (?,?,?,?,?,?,?,1)",
            (uid, f'Тестовый {key}', EMAIL[key], role, h, active, can_view))

    user(UID['root'], 'root', 'root')
    user(UID['admin'], 'admin', 'admin')
    user(UID['senior'], 'senior', 'senior', can_view=1)
    user(UID['senior_noview'], 'senior_noview', 'senior', can_view=0)
    user(UID['engineer'], 'engineer', 'engineer')
    # Пользователь со старым sha256-хешем: входит только выбором из списка
    db.execute("INSERT INTO users (id, full_name, email, role, password_hash, "
               "is_active, can_view_all, organization_id) VALUES (?,?,?,?,?,1,0,1)",
               (UID['legacy'], 'Тестовый legacy', EMAIL['legacy'], 'engineer', LEGACY_HASH))
    # Архивный инженер
    db.execute("INSERT INTO users (id, full_name, email, role, password_hash, "
               "is_active, can_view_all, organization_id) VALUES (?,?,?,?,?,0,0,1)",
               (UID['archived'], 'Тестовый архивный', EMAIL['archived'], 'engineer', LEGACY_HASH))

    db.execute("INSERT INTO projects (id, name, is_active, organization_id) VALUES (1,'Проект А',1,1)")
    db.execute("INSERT INTO projects (id, name, is_active, organization_id) VALUES (2,'Проект Б (архив)',0,1)")

    db.execute("INSERT INTO objects (id, name, address, client_name, project_id, is_active) "
               "VALUES (1,'Объект 1','Адрес 1','Заказчик 1',1,1)")
    db.execute("INSERT INTO objects (id, name, project_id, is_active) VALUES (2,'Объект 2',1,1)")
    db.execute("INSERT INTO objects (id, name, project_id, is_active) VALUES (3,'Объект 3',2,1)")

    db.execute("INSERT INTO object_users (object_id, user_id) VALUES (1,?)", (UID['engineer'],))
    db.execute("INSERT INTO object_users (object_id, user_id) VALUES (1,?)", (UID['senior'],))

    db.execute("INSERT INTO sections (id, object_id, name, is_active) VALUES (1,1,'Корпус 1',1)")

    db.execute("INSERT INTO partners (id, name, type, work_type, is_active, organization_id) "
               "VALUES (?,'ООО Партнёр','Субподрядчик','Монолитные работы',1,1)", (PARTNER,))
    db.execute("INSERT INTO partner_projects (partner_id, project_id) VALUES (?,?)",
               (PARTNER, PROJECT_ACTIVE))
    db.commit()
    db.close()


@pytest.fixture(scope='session', autouse=True)
def _baseline():
    """Один раз готовит эталонную базу и удаляет временную папку в конце."""
    _seed(_DB)
    shutil.copy2(_DB, _BASELINE)
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture
def client():
    """Свежий клиент на восстановленной из эталона базе — тесты не влияют друг на друга."""
    shutil.copy2(_BASELINE, _DB)
    sk_app.app.config['TESTING'] = True
    with sk_app.app.test_client() as c:
        yield c


@pytest.fixture
def db():
    """Прямое соединение с тестовой базой — для проверки состояния."""
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

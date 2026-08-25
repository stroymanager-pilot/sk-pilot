"""Управление учётными записями: создание, пароли, деактивация, защита root."""
from conftest import EMAIL, OBJ_IN_ACTIVE_PROJECT, UID, as_role


def _создать(client, actor, **поля):
    поля.setdefault('role', 'engineer')
    return client.post(f'/api/users?user_id={UID[actor]}', json=поля)


# ── Создание ─────────────────────────────────────────────────────────────

def test_создание_инженера_сразу_выдаёт_пароль(client):
    as_role(client, 'admin')
    r = _создать(client, 'admin', full_name='Иванов Инженер', email='ivanov@test.ru')
    assert r.status_code == 201
    d = r.get_json()['data']
    assert len(d['password']) >= 12
    assert d['password'].isalnum(), 'пароль должен быть из латиницы и цифр'


def test_созданная_учётка_имеет_настоящий_хеш(client, db):
    as_role(client, 'admin')
    _создать(client, 'admin', full_name='Иванов Инженер', email='ivanov@test.ru')
    h = db.execute("SELECT password_hash FROM users WHERE email='ivanov@test.ru'").fetchone()[0]
    assert '$' in h, 'должен быть werkzeug-хеш, а не sha256'


def test_созданная_учётка_не_попадает_в_открытый_вход(client):
    """Главное следствие: под новой учёткой нельзя войти без пароля."""
    as_role(client, 'admin')
    _создать(client, 'admin', full_name='Иванов Инженер', email='ivanov@test.ru')
    открытые = {u['email'] for u in client.get('/api/auth/login_users').get_json()['data']}
    assert 'ivanov@test.ru' not in открытые


def test_созданным_паролем_можно_войти(client):
    as_role(client, 'admin')
    pwd = _создать(client, 'admin', full_name='Иванов Инженер',
                   email='ivanov@test.ru').get_json()['data']['password']
    client.post('/api/auth/logout')
    r = client.post('/api/auth/login', json={'email': 'ivanov@test.ru', 'password': pwd})
    assert r.status_code == 200


def test_повторный_email_отклоняется(client):
    as_role(client, 'root')
    assert _создать(client, 'root', full_name='Дубль', email=EMAIL['engineer']).status_code == 400


def test_созданный_администратор_не_привязан_к_объектам(client, db):
    as_role(client, 'root')
    r = _создать(client, 'root', full_name='Новый Админ',
                 email='newadm@test.ru', role='admin')
    uid = r.get_json()['data']['id']
    links = db.execute("SELECT COUNT(*) c FROM object_users WHERE user_id=?", (uid,)).fetchone()['c']
    assert links == 0


# ── Сброс пароля ─────────────────────────────────────────────────────────

def test_админ_сбрасывает_пароль_инженеру(client):
    as_role(client, 'admin')
    r = client.post(f'/api/users/{UID["engineer"]}/reset_password?user_id={UID["admin"]}')
    assert r.status_code == 200
    assert len(r.get_json()['data']['password']) >= 12


def test_админ_не_сбрасывает_пароль_другому_админу(client):
    as_role(client, 'admin')
    assert client.post(
        f'/api/users/{UID["admin"]}/reset_password?user_id={UID["admin"]}').status_code == 403


def test_root_сбрасывает_пароль_администратору(client):
    as_role(client, 'root')
    assert client.post(
        f'/api/users/{UID["admin"]}/reset_password?user_id={UID["root"]}').status_code == 200


def test_пароль_root_сбрасывает_только_он_сам(client):
    as_role(client, 'admin')
    assert client.post(
        f'/api/users/{UID["root"]}/reset_password?user_id={UID["admin"]}').status_code == 403


def test_root_сбрасывает_пароль_себе(client):
    as_role(client, 'root')
    assert client.post(
        f'/api/users/{UID["root"]}/reset_password?user_id={UID["root"]}').status_code == 200


def test_инженер_не_сбрасывает_пароли(client):
    as_role(client, 'engineer')
    assert client.post(
        f'/api/users/{UID["engineer"]}/reset_password?user_id={UID["engineer"]}').status_code == 403


def test_старый_пароль_после_сброса_не_работает(client):
    as_role(client, 'admin')
    новый = client.post(
        f'/api/users/{UID["engineer"]}/reset_password?user_id={UID["admin"]}'
    ).get_json()['data']['password']
    client.post('/api/auth/logout')
    from conftest import PWD
    assert client.post('/api/auth/login', json={
        'email': EMAIL['engineer'], 'password': PWD['engineer']}).status_code == 401
    assert client.post('/api/auth/login', json={
        'email': EMAIL['engineer'], 'password': новый}).status_code == 200


# ── Изменение и деактивация ──────────────────────────────────────────────

def test_учётку_root_нельзя_изменить_никому(client):
    for actor in ('root', 'admin'):
        as_role(client, actor)
        r = client.patch(f'/api/users/{UID["root"]}?user_id={UID[actor]}',
                         json={'full_name': 'Переименован'})
        assert r.status_code == 403, f'{actor} смог изменить root'
        client.post('/api/auth/logout')


def test_учётку_root_нельзя_деактивировать(client):
    as_role(client, 'root')
    r = client.patch(f'/api/users/{UID["root"]}?user_id={UID["root"]}', json={'is_active': 0})
    assert r.status_code == 403


def test_админ_не_деактивирует_другого_админа(client):
    as_role(client, 'admin')
    r = client.patch(f'/api/users/{UID["admin"]}?user_id={UID["admin"]}', json={'is_active': 0})
    assert r.status_code == 403


def test_root_деактивирует_админа(client):
    as_role(client, 'root')
    r = client.patch(f'/api/users/{UID["admin"]}?user_id={UID["root"]}', json={'is_active': 0})
    assert r.status_code == 200


def test_админ_деактивирует_инженера(client, db):
    as_role(client, 'admin')
    r = client.patch(f'/api/users/{UID["engineer"]}?user_id={UID["admin"]}', json={'is_active': 0})
    assert r.status_code == 200
    assert db.execute("SELECT is_active FROM users WHERE id=?",
                      (UID['engineer'],)).fetchone()[0] == 0


def test_админ_не_повышает_до_администратора(client):
    as_role(client, 'admin')
    r = client.patch(f'/api/users/{UID["engineer"]}?user_id={UID["admin"]}', json={'role': 'admin'})
    assert r.status_code == 403


def test_root_повышает_до_администратора(client):
    as_role(client, 'root')
    r = client.patch(f'/api/users/{UID["engineer"]}?user_id={UID["root"]}', json={'role': 'admin'})
    assert r.status_code == 200


def test_админ_меняет_роль_между_инженером_и_главным(client):
    as_role(client, 'admin')
    for роль in ('senior', 'engineer'):
        r = client.patch(f'/api/users/{UID["engineer"]}?user_id={UID["admin"]}',
                         json={'role': роль})
        assert r.status_code == 200


# ── Назначение на объекты ────────────────────────────────────────────────

def test_архивного_нельзя_назначить_на_объект(client):
    as_role(client, 'admin')
    r = client.post(f'/api/objects/{OBJ_IN_ACTIVE_PROJECT}/assign_user',
                    json={'user_id': UID['archived']})
    assert r.status_code == 400


def test_администратора_нельзя_назначить_на_объект(client):
    as_role(client, 'root')
    r = client.post(f'/api/objects/{OBJ_IN_ACTIVE_PROJECT}/assign_user',
                    json={'user_id': UID['admin']})
    assert r.status_code == 400


def test_активного_инженера_назначить_можно(client):
    as_role(client, 'admin')
    r = client.post('/api/objects/2/assign_user', json={'user_id': UID['engineer']})
    assert r.status_code == 200


def test_деактивация_не_удаляет_существующие_назначения(client, db):
    """Назначения архивного пользователя — часть истории, их не трогают."""
    было = db.execute("SELECT COUNT(*) c FROM object_users WHERE user_id=?",
                      (UID['engineer'],)).fetchone()['c']
    as_role(client, 'admin')
    client.patch(f'/api/users/{UID["engineer"]}?user_id={UID["admin"]}', json={'is_active': 0})
    стало = db.execute("SELECT COUNT(*) c FROM object_users WHERE user_id=?",
                       (UID['engineer'],)).fetchone()['c']
    assert стало == было

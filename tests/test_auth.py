"""Аутентификация: вход по паролю, старый вход из списка, выход, смена пароля."""
from conftest import EMAIL, PWD, UID, as_role, login_legacy, login_password


# ── Вход по email и паролю ───────────────────────────────────────────────

def test_вход_по_верному_паролю(client):
    r = login_password(client, 'root')
    assert r.status_code == 200
    assert r.get_json()['data']['role'] == 'root'


def test_вход_по_неверному_паролю(client):
    r = client.post('/api/auth/login',
                    json={'email': EMAIL['root'], 'password': 'ЗаведомоНеверный1'})
    assert r.status_code == 401
    assert r.get_json()['ok'] is False


def test_вход_под_несуществующим_email(client):
    r = client.post('/api/auth/login',
                    json={'email': 'нет@такого.ru', 'password': 'ЧтоУгодно1'})
    assert r.status_code == 401


def test_сообщение_об_ошибке_не_выдаёт_существование_учётки(client):
    """Неверный пароль и несуществующий email отвечают одинаково."""
    r1 = client.post('/api/auth/login',
                     json={'email': EMAIL['root'], 'password': 'Неверный1'})
    r2 = client.post('/api/auth/login',
                     json={'email': 'нет@такого.ru', 'password': 'Неверный1'})
    assert r1.get_json()['error'] == r2.get_json()['error']


def test_вход_без_email_или_пароля(client):
    assert client.post('/api/auth/login', json={'email': '', 'password': ''}).status_code == 400


# ── Старые sha256-хеши паролем не считаются ──────────────────────────────

def test_legacy_хеш_не_пускает_по_паролю(client):
    """Учётка со старым sha256 не входит по паролю ни при каких условиях."""
    import hashlib
    for pwd in ('password123', hashlib.sha256(b'password123').hexdigest(), 'qwerty123456'):
        r = client.post('/api/auth/login', json={'email': EMAIL['legacy'], 'password': pwd})
        assert r.status_code == 401, f'пароль {pwd!r} неожиданно принят'


# ── Старый вход выбором из списка ────────────────────────────────────────

def test_старый_вход_для_учётки_без_пароля(client):
    r = login_legacy(client, UID['legacy'])
    assert r.status_code == 200
    assert client.get('/api/auth/me').get_json()['data']['auth'] == 'legacy'


def test_старый_вход_закрыт_для_учётки_с_паролем(client):
    r = login_legacy(client, UID['root'])
    assert r.status_code == 403


def test_старый_вход_закрыт_для_архивного(client):
    assert login_legacy(client, UID['archived']).status_code == 403


def test_список_открытого_входа_только_без_пароля(client):
    names = {u['email'] for u in client.get('/api/auth/login_users').get_json()['data']}
    assert EMAIL['legacy'] in names           # пароля нет — в списке есть
    assert EMAIL['root'] not in names         # пароль задан — из списка исчез
    assert EMAIL['archived'] not in names     # архивный не показывается


# ── Сессия и выход ───────────────────────────────────────────────────────

def test_me_без_входа_пустой(client):
    assert client.get('/api/auth/me').get_json().get('data') is None


def test_выход_завершает_сессию_на_сервере(client):
    as_role(client, 'engineer')
    assert client.get('/api/auth/me').get_json()['data']['id'] == UID['engineer']
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/auth/me').get_json().get('data') is None


# ── Смена своего пароля ──────────────────────────────────────────────────

def test_смена_пароля_короткий_отклоняется(client):
    as_role(client, 'engineer')
    r = client.post('/api/auth/change_password', json={
        'current_password': PWD['engineer'], 'new_password': 'Korot7', 'confirm_password': 'Korot7'})
    assert r.status_code == 400


def test_смена_пароля_подтверждение_не_совпало(client):
    as_role(client, 'engineer')
    r = client.post('/api/auth/change_password', json={
        'current_password': PWD['engineer'],
        'new_password': 'NovyParol2026', 'confirm_password': 'DrugoyParol2026'})
    assert r.status_code == 400


def test_смена_пароля_текущий_неверен(client):
    as_role(client, 'engineer')
    r = client.post('/api/auth/change_password', json={
        'current_password': 'СовсемНеТот1',
        'new_password': 'NovyParol2026', 'confirm_password': 'NovyParol2026'})
    assert r.status_code == 403


def test_смена_пароля_успешна_и_старый_перестаёт_работать(client):
    as_role(client, 'engineer')
    r = client.post('/api/auth/change_password', json={
        'current_password': PWD['engineer'],
        'new_password': 'NovyParol2026', 'confirm_password': 'NovyParol2026'})
    assert r.status_code == 200

    client.post('/api/auth/logout')
    старый = client.post('/api/auth/login',
                         json={'email': EMAIL['engineer'], 'password': PWD['engineer']})
    assert старый.status_code == 401
    новый = client.post('/api/auth/login',
                        json={'email': EMAIL['engineer'], 'password': 'NovyParol2026'})
    assert новый.status_code == 200


def test_смена_пароля_требует_входа(client):
    r = client.post('/api/auth/change_password', json={
        'current_password': 'x', 'new_password': 'NovyParol2026',
        'confirm_password': 'NovyParol2026'})
    assert r.status_code == 401


def test_пользователь_без_пароля_не_задаёт_его_сам(client):
    """Первый пароль выдаёт администратор — иначе учётку можно перехватить."""
    login_legacy(client, UID['legacy'])
    r = client.post('/api/auth/change_password', json={
        'current_password': '', 'new_password': 'NovyParol2026',
        'confirm_password': 'NovyParol2026'})
    assert r.status_code == 403

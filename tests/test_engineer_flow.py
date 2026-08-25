"""Основной рабочий цикл инженера: объекты → сводка → разделы → сдача."""
from conftest import OBJ_IN_ACTIVE_PROJECT, UID, as_role

DATE = '2026-03-17'


def _создать_сводку(client, object_id=OBJ_IN_ACTIVE_PROJECT, date=DATE):
    r = client.post('/api/reports', json={
        'object_id': object_id, 'user_id': UID['engineer'], 'report_date': date})
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()['data']['id']


def _подрядчик(client, object_id=OBJ_IN_ACTIVE_PROJECT):
    """Первый подрядчик объекта (появляется автосинхронизацией из партнёров)."""
    data = client.get(f'/api/objects/{object_id}').get_json()['data']
    assert data['contractors'], 'на объекте нет подрядчиков'
    return data['contractors'][0]['id']


def test_инженер_видит_только_свои_объекты(client):
    as_role(client, 'engineer')
    r = client.get(f'/api/users/{UID["engineer"]}/objects')
    assert r.status_code == 200
    ids = [o['id'] for o in r.get_json()['data']]
    assert ids == [OBJ_IN_ACTIVE_PROJECT]


def test_деактивированный_пользователь_не_получает_объекты(client):
    r = client.get(f'/api/users/{UID["archived"]}/objects')
    assert r.status_code == 403


def test_создание_сводки(client):
    as_role(client, 'engineer')
    rid = _создать_сводку(client)
    d = client.get(f'/api/reports/{rid}').get_json()['data']
    assert d['status'] == 'draft'
    assert d['report_date'] == DATE


def test_повторное_создание_в_тот_же_день_не_плодит_дубль(client):
    """UNIQUE(object_id, user_id, report_date) — вторая попытка не создаёт новую."""
    as_role(client, 'engineer')
    first = _создать_сводку(client)
    r = client.post('/api/reports', json={
        'object_id': OBJ_IN_ACTIVE_PROJECT, 'user_id': UID['engineer'], 'report_date': DATE})
    if r.status_code in (200, 201):
        assert r.get_json()['data']['id'] == first
    else:
        assert r.status_code == 400


def test_инженер_не_создаёт_сводку_по_чужому_объекту(client):
    as_role(client, 'engineer')
    r = client.post('/api/reports', json={
        'object_id': 2, 'user_id': UID['engineer'], 'report_date': DATE})
    assert r.status_code == 403


def test_заполнение_разделов_и_повторное_открытие(client):
    as_role(client, 'engineer')
    rid = _создать_сводку(client)
    cid = _подрядчик(client)

    assert client.post(f'/api/reports/{rid}/personnel', json=[{
        'contractor_id': cid, 'section_id': 1,
        'headcount': 12, 'work_description': 'Армирование плиты'}]).status_code == 200

    assert client.post(f'/api/reports/{rid}/input_control', json={
        'material_name': 'Бетон B25', 'quantity': '30 м3',
        'document_name': 'ТТН 42', 'contractor_id': cid}).status_code in (200, 201)

    assert client.post(f'/api/reports/{rid}/operational_control', json={
        'work_stage': 'Бетонирование', 'controlled_operations': 'Укладка',
        'control_method': 'Визуальный', 'section_id': 1}).status_code in (200, 201)

    assert client.post(f'/api/reports/{rid}/acceptance_control', json={
        'work_stage': 'Приёмка армирования', 'section_id': 1}).status_code in (200, 201)

    assert client.post(f'/api/reports/{rid}/remarks', json={
        'description': 'Замечание по опалубке', 'section_id': 1}).status_code in (200, 201)

    # Повторное открытие черновика возвращает всё сохранённое
    d = client.get(f'/api/reports/{rid}').get_json()['data']
    assert d['personnel'][0]['headcount'] == 12
    assert d['personnel'][0]['work_description'] == 'Армирование плиты'
    assert d['input_control'][0]['material_name'] == 'Бетон B25'
    assert d['operational_control'][0]['work_stage'] == 'Бетонирование'
    assert d['acceptance_control'][0]['work_stage'] == 'Приёмка армирования'
    assert d['verbal_remarks'][0]['description'] == 'Замечание по опалубке'


def test_персонал_перезаписывается_целиком(client):
    """Повторное сохранение заменяет строки этой сводки, а не добавляет."""
    as_role(client, 'engineer')
    rid = _создать_сводку(client)
    cid = _подрядчик(client)
    client.post(f'/api/reports/{rid}/personnel', json=[{'contractor_id': cid, 'headcount': 5}])
    client.post(f'/api/reports/{rid}/personnel', json=[{'contractor_id': cid, 'headcount': 9}])
    persons = client.get(f'/api/reports/{rid}').get_json()['data']['personnel']
    assert len(persons) == 1
    assert persons[0]['headcount'] == 9


def test_сдача_сводки(client):
    as_role(client, 'engineer')
    rid = _создать_сводку(client)
    assert client.post(f'/api/reports/{rid}/submit').status_code == 200
    d = client.get(f'/api/reports/{rid}').get_json()['data']
    assert d['status'] == 'submitted'
    assert d['submitted_at']


def test_мои_сводки_показывают_созданное(client):
    as_role(client, 'engineer')
    rid = _создать_сводку(client)
    r = client.get(f'/api/my_reports?user_id={UID["engineer"]}')
    assert r.status_code == 200
    assert rid in [x['id'] for x in r.get_json()['data']]


def test_личные_участки_инженера(client):
    as_role(client, 'engineer')
    obj = OBJ_IN_ACTIVE_PROJECT
    r = client.post(f'/api/objects/{obj}/my_sections',
                    json={'user_id': UID['engineer'], 'name': 'Мой участок'})
    assert r.status_code in (200, 201)
    got = client.get(f'/api/objects/{obj}/my_sections?user_id={UID["engineer"]}').get_json()['data']
    assert 'Мой участок' in [s['name'] for s in got]

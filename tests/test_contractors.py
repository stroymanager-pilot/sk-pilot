"""Автосинхронизация подрядчиков объекта с партнёрами проекта.

Синхронизация выполняется при GET /api/objects/<id> — поэтому каждая
проверка состоит из «изменить справочник → открыть объект → посмотреть».
"""
from conftest import OBJ_IN_ACTIVE_PROJECT, PARTNER, PROJECT_ACTIVE, UID, as_role


def _подрядчики(client, obj=OBJ_IN_ACTIVE_PROJECT):
    return client.get(f'/api/objects/{obj}').get_json()['data']['contractors']


def _по_партнёру(client, partner_id=PARTNER, obj=OBJ_IN_ACTIVE_PROJECT):
    return [c for c in _подрядчики(client, obj) if c.get('partner_id') == partner_id]


def test_партнёр_проекта_появляется_подрядчиком(client):
    as_role(client, 'admin')
    свои = _по_партнёру(client)
    assert len(свои) == 1
    assert свои[0]['name'] == 'ООО Партнёр'
    assert свои[0]['work_type'] == 'Монолитные работы'


def test_повторное_открытие_не_плодит_дубли(client):
    as_role(client, 'admin')
    for _ in range(3):
        _подрядчики(client)
    assert len(_по_партнёру(client)) == 1


def test_переименование_партнёра_обновляет_подрядчика(client):
    as_role(client, 'admin')
    _подрядчики(client)  # первичная синхронизация
    r = client.patch(f'/api/partners/{PARTNER}', json={'name': 'ООО Партнёр Новый'})
    assert r.status_code == 200

    свои = _по_партнёру(client)
    assert len(свои) == 1, 'переименование создало дубль'
    assert свои[0]['name'] == 'ООО Партнёр Новый'


def test_смена_вида_работ_доезжает_до_подрядчика(client):
    as_role(client, 'admin')
    _подрядчики(client)
    client.patch(f'/api/partners/{PARTNER}', json={'work_type': 'Кладочные работы'})
    assert _по_партнёру(client)[0]['work_type'] == 'Кладочные работы'


def test_скрытый_вручную_подрядчик_не_воскресает(client):
    as_role(client, 'admin')
    cid = _по_партнёру(client)[0]['id']

    r = client.patch(f'/api/contractors/{cid}',
                     json={'is_active': 0, 'hidden_manually': 1})
    assert r.status_code == 200

    # Несколько повторных открытий объекта не должны его вернуть
    for _ in range(3):
        assert _по_партнёру(client) == []

    скрытые = client.get(f'/api/objects/{OBJ_IN_ACTIVE_PROJECT}/contractors/hidden').get_json()['data']
    assert cid in [c['id'] for c in скрытые]


def test_возврат_скрытого_подрядчика(client):
    as_role(client, 'admin')
    cid = _по_партнёру(client)[0]['id']
    client.patch(f'/api/contractors/{cid}', json={'is_active': 0, 'hidden_manually': 1})
    assert _по_партнёру(client) == []

    client.patch(f'/api/contractors/{cid}', json={'is_active': 1, 'hidden_manually': 0})
    assert len(_по_партнёру(client)) == 1


def test_партнёр_убран_из_проекта_подрядчик_деактивируется(client):
    as_role(client, 'admin')
    _подрядчики(client)
    client.patch(f'/api/partners/{PARTNER}', json={'project_ids': []})
    assert _по_партнёру(client) == []


def test_партнёр_возвращён_в_проект_подрядчик_реактивируется(client):
    as_role(client, 'admin')
    _подрядчики(client)
    client.patch(f'/api/partners/{PARTNER}', json={'project_ids': []})
    assert _по_партнёру(client) == []

    client.patch(f'/api/partners/{PARTNER}', json={'project_ids': [PROJECT_ACTIVE]})
    свои = _по_партнёру(client)
    assert len(свои) == 1, 'партнёр не вернулся или создан дубль'


def test_скрытый_вручную_не_реактивируется_возвратом_партнёра(client):
    """hidden_manually=1 сильнее синхронизации: администратор скрыл сознательно."""
    as_role(client, 'admin')
    cid = _по_партнёру(client)[0]['id']
    client.patch(f'/api/contractors/{cid}', json={'is_active': 0, 'hidden_manually': 1})

    client.patch(f'/api/partners/{PARTNER}', json={'project_ids': []})
    client.patch(f'/api/partners/{PARTNER}', json={'project_ids': [PROJECT_ACTIVE]})

    assert _по_партнёру(client) == []


def test_ручной_подрядчик_живёт_независимо_от_партнёров(client):
    as_role(client, 'admin')
    r = client.post(f'/api/objects/{OBJ_IN_ACTIVE_PROJECT}/contractors',
                    json={'name': 'ООО Ручной', 'work_type': 'Демонтаж'})
    assert r.status_code == 201
    имена = [c['name'] for c in _подрядчики(client)]
    assert 'ООО Ручной' in имена

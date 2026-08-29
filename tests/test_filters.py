"""Фильтры реестра сводок и фотографий.

Регрессия после переезда на PostgreSQL: параметры приходят из HTTP
строками, а колонки числовые. Плюс отдельная ловушка — фильтр по
инженеру раньше назывался user_id и перезаписывался из сессии защитой
от подмены действующего пользователя, из-за чего обнулялся.

Тесты идут через вход по паролю: именно так работает реальная админка,
и именно в этом режиме проявлялась ошибка.
"""
from conftest import (OBJ_IN_ACTIVE_PROJECT, OBJ_IN_INACTIVE_PROJECT, OBJ_SECOND,
                      PROJECT_ACTIVE, PROJECT_INACTIVE, UID, as_role)

# Кто, на каком объекте и с каким статусом — набор для проверки фильтров
НАБОР = [
    (UID['engineer'], OBJ_IN_ACTIVE_PROJECT, '2026-05-01', 'submitted'),
    (UID['engineer'], OBJ_IN_ACTIVE_PROJECT, '2026-05-02', 'draft'),
    (UID['senior'], OBJ_SECOND, '2026-05-03', 'submitted'),
    (UID['senior_noview'], OBJ_IN_INACTIVE_PROJECT, '2026-05-04', 'draft'),
]


def _наполнить(db):
    """Кладёт сводки напрямую: удобнее, чем гонять их через эндпоинты."""
    for uid, obj, дата, статус in НАБОР:
        db.execute(
            "INSERT INTO daily_reports (object_id, user_id, report_date, status) "
            "VALUES (?,?,?,?)", (obj, uid, дата, статус))
    db.commit()


def _реестр(client, **фильтры):
    хвост = ''.join(f'&{k}={v}' for k, v in фильтры.items())
    r = client.get(f'/api/all_reports?requester_id={UID["root"]}{хвост}')
    assert r.status_code == 200, r.get_json()
    return r.get_json()['data']


def _инженеры(строки):
    return sorted({int(x['user_id']) for x in строки})


# ── Фильтр по инженеру ───────────────────────────────────────────────────

def test_фильтр_по_инженеру_отдаёт_только_его_сводки(client, db):
    _наполнить(db)
    as_role(client, 'root')
    строки = _реестр(client, engineer_id=UID['engineer'])
    assert строки, 'фильтр не должен возвращать пустой список'
    assert _инженеры(строки) == [UID['engineer']]


def test_фильтр_по_другому_инженеру(client, db):
    _наполнить(db)
    as_role(client, 'root')
    строки = _реестр(client, engineer_id=UID['senior'])
    assert _инженеры(строки) == [UID['senior']]


def test_без_фильтра_видны_все_инженеры(client, db):
    _наполнить(db)
    as_role(client, 'root')
    assert len(_инженеры(_реестр(client))) >= 3


def test_фильтр_по_инженеру_работает_после_входа_по_паролю(client, db):
    """Регрессия: user_id перезаписывался из сессии и обнулял фильтр.

    Проверяем именно под администратором, вошедшим по паролю, — в этом
    режиме фильтр возвращал пустой список.
    """
    _наполнить(db)
    as_role(client, 'admin')
    r = client.get(f'/api/all_reports?requester_id={UID["admin"]}'
                   f'&engineer_id={UID["engineer"]}')
    строки = r.get_json()['data']
    assert строки, 'фильтр обнулился — сессия перезаписала параметр'
    assert _инженеры(строки) == [UID['engineer']]


def test_нечисловой_фильтр_по_инженеру_даёт_пусто(client, db):
    """Мусор в фильтре показывает пустой список, а не весь реестр."""
    _наполнить(db)
    as_role(client, 'root')
    assert _реестр(client, engineer_id='abc') == []


# ── Фильтр по проекту ────────────────────────────────────────────────────

def test_фильтр_по_проекту(client, db):
    _наполнить(db)
    as_role(client, 'root')
    строки = _реестр(client, project_id=PROJECT_ACTIVE)
    assert строки
    assert {x['project_name'] for x in строки} == {'Проект А'}


def test_фильтр_по_другому_проекту(client, db):
    _наполнить(db)
    as_role(client, 'root')
    строки = _реестр(client, project_id=PROJECT_INACTIVE)
    assert строки
    assert {x['object_id'] for x in строки} == {OBJ_IN_INACTIVE_PROJECT}


def test_нечисловой_фильтр_по_проекту_даёт_пусто(client, db):
    _наполнить(db)
    as_role(client, 'root')
    assert _реестр(client, project_id='abc') == []


# ── Фильтр по объекту ────────────────────────────────────────────────────

def test_фильтр_по_объекту(client, db):
    _наполнить(db)
    as_role(client, 'root')
    строки = _реестр(client, object_id=OBJ_SECOND)
    assert строки
    assert {x['object_id'] for x in строки} == {OBJ_SECOND}


# ── Совместная работа фильтров ───────────────────────────────────────────

def test_фильтры_по_инженеру_и_проекту_вместе(client, db):
    _наполнить(db)
    as_role(client, 'root')
    строки = _реестр(client, engineer_id=UID['engineer'], project_id=PROJECT_ACTIVE)
    assert строки
    assert _инженеры(строки) == [UID['engineer']]
    assert {x['project_name'] for x in строки} == {'Проект А'}


def test_несовместимые_фильтры_дают_пусто(client, db):
    """Инженер есть, проект есть, но сводок на их пересечении нет."""
    _наполнить(db)
    as_role(client, 'root')
    assert _реестр(client, engineer_id=UID['engineer'],
                   project_id=PROJECT_INACTIVE) == []


# ── Статус ───────────────────────────────────────────────────────────────
# Отбор по статусу выполняется в браузере (admin.html фильтрует уже
# полученный список), отдельного параметра у эндпоинта нет. Проверяем,
# что сервер отдаёт статус корректно — это то, на чём работает фильтр.

def test_реестр_отдаёт_статусы_сводок(client, db):
    _наполнить(db)
    as_role(client, 'root')
    строки = _реестр(client)
    статусы = {x['status'] for x in строки}
    assert 'submitted' in статусы
    assert 'draft' in статусы
    свои = [x for x in строки if x['user_id'] == UID['engineer']]
    assert sorted(x['status'] for x in свои) == ['draft', 'submitted']


# ── Фотографии ───────────────────────────────────────────────────────────

def test_фильтр_фотографий_по_проекту(client, db):
    """У /api/all_photos фильтры те же по устройству — проверяем приведение типов."""
    _наполнить(db)
    as_role(client, 'root')
    r = client.get(f'/api/all_photos?requester_id={UID["root"]}'
                   f'&project_id={PROJECT_ACTIVE}')
    assert r.status_code == 200
    assert isinstance(r.get_json()['data'], list)


def test_нечисловой_фильтр_фотографий_даёт_пусто(client, db):
    _наполнить(db)
    as_role(client, 'root')
    r = client.get(f'/api/all_photos?requester_id={UID["root"]}&project_id=abc')
    assert r.status_code == 200
    assert r.get_json()['data'] == []


def test_нечисловой_идентификатор_запросившего_не_ломает_сервер(client):
    """Мусор в requester_id — это 403, а не 500.

    Проверяем без входа: у вошедшего по паролю сессия сама подставляет
    правильный идентификатор, и до эндпоинта мусор не доходит.
    """
    assert client.get('/api/all_reports?requester_id=abc').status_code == 403
    assert client.get('/api/all_photos?requester_id=abc').status_code == 403


def test_сессия_перекрывает_мусор_в_идентификаторе(client):
    """У вошедшего по паролю неверный requester_id заменяется из сессии."""
    as_role(client, 'root')
    assert client.get('/api/all_reports?requester_id=abc').status_code == 200

"""Сборка автоматических блоков ежемесячного отчёта (шаг 2-2).

Проверяем правило отбора сводок, периоды членства объектов в группе,
границу отчётного периода, сохранность ручных правок при пересборке и
содержимое блоков организаций и персонала.

Данные сеются прямо в базу через ту же прослойку совместимости, что
использует приложение, поэтому один и тот же код идёт и в SQLite,
и в PostgreSQL.
"""
import json

import pytest

from conftest import UID, as_role

АВГУСТ = {'year': 2026, 'month': 8}


# ── Помощники посева ────────────────────────────────────────────────────

def _id(db, sql, params=()):
    return db.execute(sql, params).fetchone()['id']


def группа(db, name='Суздальское', title='Суздальское', объекты=((1, None, None),)):
    db.execute("INSERT INTO report_groups (name, report_title) VALUES (?,?)",
               (name, title))
    gid = _id(db, "SELECT id FROM report_groups WHERE name=?", (name,))
    for oid, с, по in объекты:
        db.execute("INSERT INTO report_group_objects (report_group_id, object_id, "
                   "date_from, date_to) VALUES (?,?,?,?)", (gid, oid, с, по))
    db.commit()
    return gid


def сводка(db, object_id, user_id, дата, status='draft'):
    db.execute("INSERT INTO daily_reports (object_id, user_id, report_date, status) "
               "VALUES (?,?,?,?)", (object_id, user_id, дата, status))
    db.commit()
    return _id(db, "SELECT id FROM daily_reports WHERE object_id=? AND user_id=? "
                   "AND report_date=?", (object_id, user_id, дата))


def подрядчик(db, object_id, name, work_type='Монолитные работы', is_active=1):
    db.execute("INSERT INTO contractors (object_id, name, work_type, is_active) "
               "VALUES (?,?,?,?)", (object_id, name, work_type, is_active))
    db.commit()
    return _id(db, "SELECT MAX(id) AS id FROM contractors WHERE object_id=? AND name=?",
               (object_id, name))


def ок(client, gid, year=2026, month=8, uid=None):
    return client.post('/api/monthly_reports/generate',
                       json={'report_group_id': gid, 'year': year, 'month': month},
                       query_string={'user_id': uid or UID['admin']})


# ── Отбор сводок ────────────────────────────────────────────────────────

def test_пустая_сданная_сводка_с_одним_фото_не_попадает(client, db):
    """Фотография сама по себе содержательности не даёт."""
    gid = группа(db)
    rid = сводка(db, 1, UID['engineer'], '2026-08-05', status='submitted')
    db.execute("INSERT INTO photos (report_id, file_path) VALUES (?,?)", (rid, 'a.jpg'))
    db.commit()

    as_role(client, 'admin')
    d = ок(client, gid).get_json()['data']
    assert d['сводок_отобрано'] == 0
    assert d['блоки']['photos']['строки'] == []


def test_черновик_с_содержанием_попадает(client, db):
    """Статус значения не имеет: черновик действующего инженера — работа."""
    gid = группа(db)
    rid = сводка(db, 1, UID['engineer'], '2026-08-05', status='draft')
    db.execute("INSERT INTO operational_control (report_id, section_id, work_stage) "
               "VALUES (?,?,?)", (rid, 1, 'Армирование'))
    db.commit()

    as_role(client, 'admin')
    d = ок(client, gid).get_json()['data']
    assert d['сводок_отобрано'] == 1
    участки = [р['участок'] for р in d['блоки']['control_summary']['разделы']]
    assert участки == ['Корпус 1']


def test_сводка_архивного_инженера_попадает(client, db):
    """Инженер уволен, но его работа за период остаётся в отчёте."""
    gid = группа(db)
    rid = сводка(db, 1, UID['archived'], '2026-08-06')
    db.execute("INSERT INTO personnel_entries (report_id, contractor_id, headcount) "
               "VALUES (?,?,?)", (rid, подрядчик(db, 1, 'ООО «Тест»'), 5))
    db.commit()

    as_role(client, 'admin')
    отв = ок(client, gid).get_json()['data']['блоки']['title']['ответственные']
    assert [(о['full_name'], о['is_active']) for о in отв] == [('Тестовый архивный', 0)]


def test_сводка_другого_месяца_не_попадает(client, db):
    gid = группа(db)
    rid = сводка(db, 1, UID['engineer'], '2026-07-31')
    db.execute("INSERT INTO operational_control (report_id, work_stage) VALUES (?,?)",
               (rid, 'Июльские работы'))
    db.commit()

    as_role(client, 'admin')
    assert ок(client, gid).get_json()['data']['сводок_отобрано'] == 0


# ── Состав группы и периоды членства ────────────────────────────────────

def test_два_объекта_группы_без_потерь_и_задвоения(client, db):
    """Случай «Суздальского»: два дома в одном отчёте."""
    gid = группа(db, объекты=((1, None, None), (2, None, None)))
    for oid, дата, этап in ((1, '2026-08-05', 'Дом 1'), (2, '2026-08-06', 'Дом 2')):
        rid = сводка(db, oid, UID['engineer'], дата)
        db.execute("INSERT INTO operational_control (report_id, work_stage) VALUES (?,?)",
                   (rid, этап))
    db.commit()

    as_role(client, 'admin')
    d = ок(client, gid).get_json()['data']
    assert d['сводок_отобрано'] == 2
    assert [о['object_id'] for о in d['объекты']] == [1, 2]
    строки = [с for р in d['блоки']['control_summary']['разделы'] for с in р['строки']]
    assert sorted(с['этап'] for с in строки) == ['Дом 1', 'Дом 2']


def test_объект_вне_периода_членства_не_учитывается(client, db):
    """Дом ушёл из группы в июле — его августовские сводки не наши."""
    gid = группа(db, объекты=((1, None, '2026-07-31'), (2, None, None)))
    for oid in (1, 2):
        rid = сводка(db, oid, UID['engineer'], '2026-08-05')
        db.execute("INSERT INTO operational_control (report_id, work_stage) VALUES (?,?)",
                   (rid, f'Объект {oid}'))
    db.commit()

    as_role(client, 'admin')
    d = ок(client, gid).get_json()['data']
    assert [о['object_id'] for о in d['объекты']] == [2]
    assert d['сводок_отобрано'] == 1


def test_членство_обрезает_период_внутри_месяца(client, db):
    """Дом вернулся в группу с 10 августа: до этой даты сводки не берём."""
    gid = группа(db, объекты=((1, '2026-08-10', None),))
    for дата in ('2026-08-05', '2026-08-15'):
        rid = сводка(db, 1, UID['engineer'], дата)
        db.execute("INSERT INTO operational_control (report_id, work_stage) VALUES (?,?)",
                   (rid, дата))
    db.commit()

    as_role(client, 'admin')
    d = ок(client, gid).get_json()['data']
    assert d['объекты'][0]['период_с'] == '2026-08-10'
    assert d['сводок_отобрано'] == 1


# ── Граница отчётного периода и доступ ──────────────────────────────────

@pytest.mark.parametrize('year,month', [(2026, 7), (2025, 12)])
def test_период_раньше_августа_2026_отклоняется(client, db, year, month):
    gid = группа(db)
    as_role(client, 'admin')
    r = ок(client, gid, year=year, month=month)
    assert r.status_code == 400
    assert 'август' in r.get_json()['error']


def test_инженеру_сборка_запрещена(client, db):
    gid = группа(db)
    as_role(client, 'engineer')
    assert ок(client, gid, uid=UID['engineer']).status_code == 403


def test_несуществующая_группа(client, db):
    as_role(client, 'admin')
    assert ок(client, 999).status_code == 404


# ── Пересборка ──────────────────────────────────────────────────────────

def test_пересборка_не_затирает_ручные_правки(client, db):
    gid = группа(db)
    rid = сводка(db, 1, UID['engineer'], '2026-08-05')
    db.execute("INSERT INTO operational_control (report_id, work_stage) VALUES (?,?)",
               (rid, 'Первый заход'))
    db.commit()

    as_role(client, 'admin')
    отчёт = ок(client, gid).get_json()['data']['report_id']

    db.execute("UPDATE monthly_report_blocks SET content_json=?, is_edited=1 "
               "WHERE report_id=? AND block_key=?",
               (json.dumps({'строки': ['правка руками']}, ensure_ascii=False),
                отчёт, 'general'))
    db.commit()

    d = ок(client, gid).get_json()['data']
    assert d['report_id'] == отчёт, 'вторая сборка не должна заводить новый отчёт'
    assert 'general' in d['сохранены_правки']

    сохранено = db.execute(
        "SELECT content_json FROM monthly_report_blocks WHERE report_id=? AND block_key=?",
        (отчёт, 'general')).fetchone()['content_json']
    assert json.loads(сохранено) == {'строки': ['правка руками']}


def test_сохранённый_отчёт_читается(client, db):
    gid = группа(db)
    rid = сводка(db, 1, UID['engineer'], '2026-08-05')
    db.execute("INSERT INTO operational_control (report_id, work_stage) VALUES (?,?)",
               (rid, 'Работы'))
    db.commit()

    as_role(client, 'admin')
    отчёт = ок(client, gid).get_json()['data']['report_id']
    d = client.get(f'/api/monthly_reports/{отчёт}',
                   query_string={'user_id': UID['admin']}).get_json()['data']
    assert d['status'] == 'черновик'
    assert d['блоки']['title']['период'] == 'август 2026'
    assert d['блоки']['general']['_is_edited'] is False


# ── Организации и персонал ──────────────────────────────────────────────

def test_перечень_организаций_включает_заказчика(client, db):
    """По признаку «подрядчик» не фильтруем: заказчик должен быть виден."""
    gid = группа(db)
    подряд = подрядчик(db, 1, 'ООО «Пинстрой»', 'Монолитные работы')
    подрядчик(db, 1, 'ООО «СЗ «ЛСТ Эксперт»', 'Заказчик')
    rid = сводка(db, 1, UID['engineer'], '2026-08-05')
    db.execute("INSERT INTO operational_control (report_id, contractor_id, work_stage) "
               "VALUES (?,?,?)", (rid, подряд, 'Бетонирование'))
    db.commit()

    as_role(client, 'admin')
    строки = ок(client, gid).get_json()['data']['блоки']['organizations']['строки']
    по_имени = {с['наименование']: с for с in строки}
    assert 'ООО «СЗ «ЛСТ Эксперт»' in по_имени
    assert по_имени['ООО «СЗ «ЛСТ Эксперт»']['статус_организации'] == 'Заказчик'
    assert по_имени['ООО «СЗ «ЛСТ Эксперт»']['работала_в_периоде'] is False
    assert по_имени['ООО «Пинстрой»']['работала_в_периоде'] is True
    assert 'работы в периоде' in по_имени['ООО «Пинстрой»']['источники']


def test_персонал_считает_среднюю_максимум_минимум_и_дни(client, db):
    gid = группа(db)
    cid = подрядчик(db, 1, 'ООО «Пинстрой»')
    for дата, чел, работа in (('2026-08-05', 9, 'Армирование'),
                              ('2026-08-06', 12, 'Монтаж плит')):
        rid = сводка(db, 1, UID['engineer'], дата)
        db.execute("INSERT INTO personnel_entries (report_id, contractor_id, headcount, "
                   "work_description) VALUES (?,?,?,?)", (rid, cid, чел, работа))
    # нулевая численность в расчёт не идёт
    rid = сводка(db, 1, UID['engineer'], '2026-08-07')
    db.execute("INSERT INTO personnel_entries (report_id, contractor_id, headcount) "
               "VALUES (?,?,?)", (rid, cid, 0))
    db.commit()

    as_role(client, 'admin')
    строки = ок(client, gid).get_json()['data']['блоки']['personnel']['строки']
    assert len(строки) == 1
    с = строки[0]
    assert (с['средняя_численность'], с['максимум'], с['минимум'],
            с['дней_присутствия']) == (10.5, 12, 9, 2)
    assert sorted(с['виды_работ']) == ['Армирование', 'Монтаж плит']


def test_фото_без_подписи_получает_автоподпись(client, db):
    gid = группа(db)
    rid = сводка(db, 1, UID['engineer'], '2026-08-18')
    db.execute("INSERT INTO operational_control (report_id, section_id, work_stage) "
               "VALUES (?,?,?)", (rid, 1, 'Монтаж плиты перекрытия'))
    db.execute("INSERT INTO photos (report_id, file_path, caption) VALUES (?,?,?)",
               (rid, 'a.jpg', ''))
    db.execute("INSERT INTO photos (report_id, file_path, caption) VALUES (?,?,?)",
               (rid, 'b.jpg', 'Своя подпись'))
    db.commit()

    as_role(client, 'admin')
    строки = ок(client, gid).get_json()['data']['блоки']['photos']['строки']
    по_файлу = {с['файл']: с for с in строки}
    assert по_файлу['a.jpg']['подпись_своя'] is False
    assert по_файлу['a.jpg']['подпись'] == \
        'Монтаж плиты перекрытия, Корпус 1, 2026-08-18'
    assert по_файлу['b.jpg']['подпись_своя'] is True
    assert по_файлу['b.jpg']['подпись'] == 'Своя подпись'


def test_несобираемые_блоки_помечены(client, db):
    """Остальные блоки шаблона видны, но честно помечены как незаполненные."""
    gid = группа(db)
    as_role(client, 'admin')
    блоки = ок(client, gid).get_json()['data']['блоки']
    import report_builder
    for ключ in report_builder.ПОКА_НЕ_СОБИРАЮТСЯ:
        assert блоки[ключ] == {'не_собирается': True}
    for ключ in report_builder.АВТО_БЛОКИ:
        assert 'не_собирается' not in блоки[ключ]


# ── Проверка целостности справочника ────────────────────────────────────

def test_проверка_целостности_видит_объект_в_архивном_проекте(client):
    """Объект 3 активен внутри деактивированного проекта — случай объектов 5 и 9."""
    as_role(client, 'admin')
    d = client.get('/api/admin/integrity_check',
                   query_string={'user_id': UID['admin']}).get_json()['data']
    по_ключу = {п['ключ']: п for п in d['проверки']}
    assert [с['id'] for с in по_ключу['project_inactive']['объекты']] == [3]
    # Участки заведены только у объекта 1
    assert {с['id'] for с in по_ключу['no_sections']['объекты']} == {2, 3}
    # Инженеры назначены только на объект 1
    assert {с['id'] for с in по_ключу['no_engineers']['объекты']} == {2, 3}


def test_проверка_целостности_закрыта_для_инженера(client):
    as_role(client, 'engineer')
    assert client.get('/api/admin/integrity_check',
                      query_string={'user_id': UID['engineer']}).status_code == 403

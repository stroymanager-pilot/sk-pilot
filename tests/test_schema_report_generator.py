"""Схема генератора ежемесячного отчёта (шаг 2-1).

Проверяем структуру, а не наполнение: группы отчётов и шаблон заводятся
на боевой базе скриптом db/schema_2_1_report_generator.sql, тесты работают
на чистой базе.

Проверки написаны дословно одинаково для обеих СУБД: обращение к
несуществующей таблице или колонке поднимает исключение и в SQLite,
и в PostgreSQL, поэтому отдельные ветки не нужны.
"""
import pytest

НОВЫЕ_ТАБЛИЦЫ = [
    'report_groups', 'report_group_objects',
    'report_templates', 'report_template_blocks',
    'monthly_reports', 'monthly_report_blocks',
    'author_supervision', 'volume_changes', 'schedule_notes', 'design_approvals',
    'work_types', 'object_work_types', 'monthly_progress',
]

НОВЫЕ_КОЛОНКИ = [
    ('objects', 'report_name'),
    ('objects', 'object_type'),
    ('objects', 'contract_date'),
    ('objects', 'contract_amendment'),
    ('objects', 'client_partner_id'),
    ('objects', 'client_signatory'),
    ('objects', 'client_signatory_role'),
    ('objects', 'contractor_signatory'),
    ('objects', 'contractor_signatory_role'),
    ('sections', 'section_type'),
    ('prescriptions_log', 'issued_by_name'),
]


@pytest.mark.parametrize('таблица', НОВЫЕ_ТАБЛИЦЫ)
def test_таблица_создана(db, таблица):
    assert db.execute(f'SELECT COUNT(*) AS c FROM {таблица}').fetchone()['c'] == 0


@pytest.mark.parametrize('таблица,колонка', НОВЫЕ_КОЛОНКИ)
def test_колонка_добавлена(db, таблица, колонка):
    db.execute(f'SELECT {колонка} FROM {таблица} WHERE 1=0').fetchall()


def test_группа_отчёта_и_её_объекты(db):
    """Группа собирается из нескольких объектов — случай «Суздальское»."""
    db.execute("INSERT INTO report_groups (name, report_title) VALUES (?,?)",
               ('Тестовая группа', 'Суздальское'))
    gid = db.execute("SELECT id FROM report_groups WHERE name=?",
                     ('Тестовая группа',)).fetchone()['id']
    for obj in (1, 2):
        db.execute("INSERT INTO report_group_objects (report_group_id, object_id) "
                   "VALUES (?,?)", (gid, obj))
    db.commit()
    n = db.execute("SELECT COUNT(*) AS c FROM report_group_objects WHERE report_group_id=?",
                   (gid,)).fetchone()['c']
    assert n == 2


def test_объект_входит_в_группу_повторно_с_другим_периодом(db):
    """Состав группы меняется во времени: дом ушёл и вернулся по доп. соглашению.
    Уникальности по (группа, объект) быть не должно."""
    db.execute("INSERT INTO report_groups (name) VALUES (?)", ('Группа с периодами',))
    gid = db.execute("SELECT id FROM report_groups WHERE name=?",
                     ('Группа с периодами',)).fetchone()['id']
    db.execute("INSERT INTO report_group_objects (report_group_id, object_id, date_from, date_to) "
               "VALUES (?,?,?,?)", (gid, 1, '2026-01-01', '2026-06-30'))
    db.execute("INSERT INTO report_group_objects (report_group_id, object_id, date_from) "
               "VALUES (?,?,?)", (gid, 1, '2026-09-01'))
    db.commit()
    assert db.execute("SELECT COUNT(*) AS c FROM report_group_objects WHERE report_group_id=?",
                      (gid,)).fetchone()['c'] == 2


def test_имя_группы_уникально(db):
    db.execute("INSERT INTO report_groups (name) VALUES (?)", ('Дубль',))
    db.commit()
    with pytest.raises(Exception):
        db.execute("INSERT INTO report_groups (name) VALUES (?)", ('Дубль',))
        db.commit()


def test_блок_отчёта_один_на_ключ(db):
    """UNIQUE(report_id, block_key): повторная сборка обновляет строку,
    а не добавляет вторую — иначе ручные правки задваивались бы."""
    db.execute("INSERT INTO report_groups (name) VALUES (?)", ('Группа для отчёта',))
    gid = db.execute("SELECT id FROM report_groups WHERE name=?",
                     ('Группа для отчёта',)).fetchone()['id']
    db.execute("INSERT INTO monthly_reports (report_group_id, year, month) VALUES (?,?,?)",
               (gid, 2026, 8))
    rid = db.execute("SELECT id FROM monthly_reports WHERE report_group_id=?",
                     (gid,)).fetchone()['id']
    db.execute("INSERT INTO monthly_report_blocks (report_id, block_key, content_json) "
               "VALUES (?,?,?)", (rid, 'personnel', '{}'))
    db.commit()
    with pytest.raises(Exception):
        db.execute("INSERT INTO monthly_report_blocks (report_id, block_key, content_json) "
                   "VALUES (?,?,?)", (rid, 'personnel', '{}'))
        db.commit()


def test_статус_отчёта_по_умолчанию_черновик(db):
    db.execute("INSERT INTO report_groups (name) VALUES (?)", ('Группа статуса',))
    gid = db.execute("SELECT id FROM report_groups WHERE name=?",
                     ('Группа статуса',)).fetchone()['id']
    db.execute("INSERT INTO monthly_reports (report_group_id, year, month) VALUES (?,?,?)",
               (gid, 2026, 8))
    db.commit()
    r = db.execute("SELECT status, version FROM monthly_reports WHERE report_group_id=?",
                   (gid,)).fetchone()
    assert r['status'] == 'черновик'
    assert r['version'] == 1


def test_существующие_данные_на_месте(db):
    """Схема добавлена, ничего не потеряно."""
    assert db.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c'] == 7
    assert db.execute('SELECT COUNT(*) AS c FROM objects').fetchone()['c'] == 3
    assert db.execute('SELECT COUNT(*) AS c FROM partners').fetchone()['c'] == 1

# SK-pilot — сборка автоматических блоков ежемесячного отчёта (шаг 2-2).
#
# Вынесено из app.py отдельным модулем: логика сборки самодостаточна и
# объёмна, в app.py остаются только эндпоинты.
#
# Всё общение с базой идёт через тот же слой совместимости, что и остальное
# приложение: db.execute(sql, params) с плейсхолдерами '?'. Поэтому модуль
# одинаково работает на SQLite и на PostgreSQL.

import calendar
import json

# Отчётный период начинается с августа 2026: более ранние данные остаются
# в системе, но в отчёты не идут.
ПЕРИОД_С = (2026, 8)

МЕСЯЦЫ = ['', 'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
          'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']

# Блоки, которые собираются автоматически в этой подзадаче
АВТО_БЛОКИ = ['title', 'control_summary', 'deviations', 'prescriptions',
              'organizations', 'personnel', 'as_built_docs', 'general', 'photos']

# Блоки шаблона, которые пока не собираются — показываются как «нет данных»
ПОКА_НЕ_СОБИРАЮТСЯ = ['author_supervision', 'progress', 'volume_changes',
                      'schedule', 'design_docs', 'safety', 'prescriptions_appendix']


def границы_месяца(year, month):
    """Первое и последнее число месяца строками YYYY-MM-DD."""
    последний = calendar.monthrange(year, month)[1]
    return f'{year:04d}-{month:02d}-01', f'{year:04d}-{month:02d}-{последний:02d}'


def подпись_периода(year, month):
    return f'{МЕСЯЦЫ[month]} {year}'


def _пусто(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _текст(v):
    return (v or '').strip()


# ─────────────────────────────────────────────────────────────
# СОСТАВ ГРУППЫ И ОТБОР СВОДОК
# ─────────────────────────────────────────────────────────────

def объекты_группы(db, group_id, дата_с, дата_по):
    """Объекты группы, действующие хотя бы часть отчётного месяца.

    Учитывает date_from / date_to в report_group_objects: объект попадает
    в отчёт только за периоды своего членства. Один объект может входить
    в группу несколько раз с разными периодами — тогда для него получится
    несколько интервалов.
    """
    строки = db.execute("""
        SELECT rgo.object_id, rgo.date_from, rgo.date_to,
               o.name, o.report_name, o.address, o.client_name,
               o.contract_number, o.contract_date, o.contract_amendment,
               o.object_type, o.client_signatory, o.client_signatory_role,
               o.contractor_signatory, o.contractor_signatory_role,
               COALESCE(o.is_active,1) AS is_active
          FROM report_group_objects rgo
          JOIN objects o ON o.id = rgo.object_id
         WHERE rgo.report_group_id = ?
         ORDER BY rgo.object_id
    """, (group_id,)).fetchall()

    результат = []
    for r in строки:
        с = _текст(r['date_from']) or дата_с
        по = _текст(r['date_to']) or дата_по
        # пересечение периода членства с отчётным месяцем
        начало, конец = max(с, дата_с), min(по, дата_по)
        if начало > конец:
            continue                      # членство не пересекается с месяцем
        д = dict(r)
        д['период_с'], д['период_по'] = начало, конец
        результат.append(д)
    return результат


def сводки_периода(db, объекты):
    """Сводки, попадающие в отчёт.

    Правило отбора: сводка содержательна, если в ней есть хотя бы одна
    запись входного, операционного или приёмочного контроля либо запись
    персонала. Статус сводки значения не имеет — черновики действующих
    инженеров это настоящая работа. Фотография содержательности не даёт:
    пустая сводка со статусом «сдана» и одним фото в отчёт не идёт.
    """
    if not объекты:
        return []

    кандидаты = []
    for o in объекты:
        кандидаты.extend(db.execute("""
            SELECT dr.id, dr.object_id, dr.user_id, dr.report_date, dr.status,
                   u.full_name AS engineer_name,
                   COALESCE(u.is_active,1) AS engineer_active
              FROM daily_reports dr
              JOIN users u ON u.id = dr.user_id
             WHERE dr.object_id = ?
               AND dr.report_date >= ? AND dr.report_date <= ?
             ORDER BY dr.report_date
        """, (o['object_id'], o['период_с'], o['период_по'])).fetchall())

    отобранные = []
    for r in кандидаты:
        rid = r['id']
        содержательных = 0
        for таблица in ('input_control', 'operational_control',
                        'acceptance_control', 'personnel_entries'):
            содержательных += db.execute(
                f'SELECT COUNT(*) AS c FROM {таблица} WHERE report_id=?',
                (rid,)).fetchone()['c']
            if содержательных:
                break
        if содержательных:
            отобранные.append(dict(r))
    return отобранные


def _ids(сводки):
    return [s['id'] for s in сводки]


def _заглушки(db, ids):
    """Список плейсхолдеров и параметров для IN (...)."""
    return ', '.join('?' for _ in ids), list(ids)


# ─────────────────────────────────────────────────────────────
# СПРАВОЧНЫЕ СООТВЕТСТВИЯ
# ─────────────────────────────────────────────────────────────

def _участки(db, объекты):
    """id участка → название. Учитываются и общие, и личные участки."""
    карта = {}
    for o in объекты:
        for r in db.execute('SELECT id, name FROM sections WHERE object_id=?',
                            (o['object_id'],)).fetchall():
            карта[r['id']] = r['name']
    return карта


def _подрядчики(db, объекты):
    """id подрядчика → строка с названием и статусом организации."""
    карта = {}
    for o in объекты:
        for r in db.execute("""
            SELECT c.id, c.name, c.work_type, p.type AS partner_type,
                   COALESCE(c.is_active,1) AS is_active
              FROM contractors c
              LEFT JOIN partners p ON p.id = c.partner_id
             WHERE c.object_id = ?
        """, (o['object_id'],)).fetchall():
            карта[r['id']] = dict(r)
    return карта


# ─────────────────────────────────────────────────────────────
# БЛОКИ
# ─────────────────────────────────────────────────────────────

def блок_title(группа, объекты, year, month, сводки):
    """Реквизиты, период, договор, заказчик.

    Ответственные за период берутся из авторов сводок, а не из текущей
    привязки в админке: инженеры подменяют друг друга на время отпуска.
    """
    ответственные = {}
    for s in сводки:
        ответственные.setdefault(s['user_id'], {
            'user_id': s['user_id'],
            'full_name': s['engineer_name'],
            'is_active': s['engineer_active'],
            'сводок': 0,
        })
        ответственные[s['user_id']]['сводок'] += 1

    return {
        'report_title': группа['report_title'] or группа['name'],
        'client_name': группа['client_name'] or next(
            (o['client_name'] for o in объекты if o['client_name']), None),
        'период': подпись_периода(year, month),
        'year': year,
        'month': month,
        'объекты': [{
            'object_id': o['object_id'],
            'наименование': o['report_name'] or o['name'],
            'адрес': o['address'],
            'заказчик': o['client_name'],
            'тип': o['object_type'],
            'договор': o['contract_number'],
            'дата_договора': o['contract_date'],
            'доп_соглашение': o['contract_amendment'],
            'подписант_заказчика': o['client_signatory'],
            'должность_заказчика': o['client_signatory_role'],
            'подписант_подрядчика': o['contractor_signatory'],
            'должность_подрядчика': o['contractor_signatory_role'],
        } for o in объекты],
        'ответственные': sorted(ответственные.values(),
                                key=lambda x: -x['сводок']),
        'сводок_в_периоде': len(сводки),
        # Фото обложки выбирает инженер — в этой подзадаче не заполняется
        'фото_обложки': None,
    }


def блок_control_summary(db, сводки, участки):
    """Свод проконтролированных работ, сгруппированный по участкам."""
    if not сводки:
        return {'разделы': []}
    ids = _ids(сводки)
    зн, пар = _заглушки(db, ids)
    дата_по_сводке = {s['id']: s['report_date'] for s in сводки}

    источники = [
        ('Входной контроль', 'input_control',
         "material_name AS work_stage, quantity AS controlled_operations, "
         "document_name AS control_method"),
        ('Операционный контроль', 'operational_control',
         'work_stage, controlled_operations, control_method'),
        ('Приёмочный контроль', 'acceptance_control',
         'work_stage, controlled_operations, control_method'),
    ]
    по_участкам = {}
    for подпись, таблица, поля in источники:
        for r in db.execute(
                f'SELECT report_id, section_id, {поля} FROM {таблица} '
                f'WHERE report_id IN ({зн})', пар).fetchall():
            уч = участки.get(r['section_id']) or 'Без участка'
            по_участкам.setdefault(уч, []).append({
                'вид_контроля': подпись,
                'дата': дата_по_сводке.get(r['report_id']),
                'этап': _текст(r['work_stage']),
                'операции': _текст(r['controlled_operations']),
                'метод': _текст(r['control_method']),
            })
    return {'разделы': [
        {'участок': уч, 'записей': len(строки),
         'строки': sorted(строки, key=lambda x: (x['дата'] or '', x['вид_контроля']))}
        for уч, строки in sorted(по_участкам.items())
    ]}


def блок_deviations(db, сводки, участки, подрядчики):
    """Отклонения с датой выявления, участком и подрядчиком."""
    if not сводки:
        return {'строки': []}
    ids = _ids(сводки)
    зн, пар = _заглушки(db, ids)
    дата_по_сводке = {s['id']: s['report_date'] for s in сводки}

    строки = []
    for подпись, таблица in (('Входной контроль', 'input_control'),
                             ('Операционный контроль', 'operational_control'),
                             ('Приёмочный контроль', 'acceptance_control')):
        поле_подрядчика = 'contractor_id'
        for r in db.execute(
                f'SELECT report_id, section_id, {поле_подрядчика}, '
                f'deviation_note, status FROM {таблица} '
                f'WHERE report_id IN ({зн})', пар).fetchall():
            if _пусто(r['deviation_note']):
                continue
            орг = подрядчики.get(r['contractor_id'])
            строки.append({
                'дата_выявления': дата_по_сводке.get(r['report_id']),
                'вид_контроля': подпись,
                'участок': участки.get(r['section_id']) or 'Без участка',
                'подрядчик': орг['name'] if орг else None,
                'описание': _текст(r['deviation_note']),
                'статус': _текст(r['status']) or 'не указан',
            })
    return {'строки': sorted(строки, key=lambda x: (x['дата_выявления'] or '',))}


def блок_prescriptions(db, сводки, участки):
    """Реестр предписаний: номер, дата, срок, кем выдано, статус."""
    if not сводки:
        return {'строки': []}
    зн, пар = _заглушки(db, _ids(сводки))
    строки = [{
        'номер': _текст(r['number']),
        'дата_выдачи': _текст(r['issue_date']),
        'срок': _текст(r['deadline']),
        'участок': участки.get(r['section_id']) or 'Без участка',
        'кем_выдано': _текст(r['issued_by_name']),
        'статус': _текст(r['status']) or 'не указан',
        'tj_id': _текст(r['tj_prescription_id']),
    } for r in db.execute(
        f'SELECT number, issue_date, deadline, section_id, issued_by_name, '
        f'status, tj_prescription_id FROM prescriptions_log '
        f'WHERE report_id IN ({зн})', пар).fetchall()]
    return {'строки': sorted(строки, key=lambda x: (x['дата_выдачи'], x['номер']))}


def блок_organizations(db, объекты, сводки, подрядчики):
    """Перечень организаций за период из двух источников.

    Первый — активные привязки contractors к объектам группы.
    Второй — организации, фактически упомянутые в записях контроля и
    персонала за месяц; он точнее, потому что показывает, кто реально
    работал, а не кто числится.

    Заказчик и генподряд попадают в перечень наравне с субподрядчиками:
    таблица contractors используется шире своего названия, там же лежит
    заказчик с ролью «Заказчик» в work_type. По признаку «подрядчик»
    не фильтруем.
    """
    сводные = {}

    def добавить(cid, источник):
        орг = подрядчики.get(cid)
        if not орг:
            return
        зап = сводные.setdefault(cid, {
            'contractor_id': cid,
            'наименование': орг['name'],
            'статус_организации': _текст(орг['work_type']) or _текст(орг['partner_type']) or '—',
            'источники': set(),
            'активна_на_объекте': bool(орг['is_active']),
        })
        зап['источники'].add(источник)

    # 1. Активные привязки к объектам группы
    for cid, орг in подрядчики.items():
        if орг['is_active']:
            добавить(cid, 'справочник')

    # 2. Фактические упоминания в периоде
    if сводки:
        зн, пар = _заглушки(db, _ids(сводки))
        for таблица in ('input_control', 'operational_control',
                        'acceptance_control', 'personnel_entries'):
            for r in db.execute(
                    f'SELECT DISTINCT contractor_id FROM {таблица} '
                    f'WHERE report_id IN ({зн}) AND contractor_id IS NOT NULL',
                    пар).fetchall():
                добавить(r['contractor_id'], 'работы в периоде')

    строки = []
    for з in сводные.values():
        з['источники'] = sorted(з['источники'])
        з['работала_в_периоде'] = 'работы в периоде' in з['источники']
        строки.append(з)
    return {'строки': sorted(строки, key=lambda x: (not x['работала_в_периоде'],
                                                    x['наименование']))}


def блок_personnel(db, сводки, подрядчики):
    """Персонал подрядчиков за месяц.

    По каждой организации: средняя численность, максимум, минимум, число
    дней присутствия и виды работ. Полностью автоматический — данные
    инженер вносит ежедневно, отдельных действий не требуется.
    """
    if not сводки:
        return {'строки': []}
    зн, пар = _заглушки(db, _ids(сводки))
    дата_по_сводке = {s['id']: s['report_date'] for s in сводки}

    по_орг = {}
    for r in db.execute(
            f'SELECT report_id, contractor_id, headcount, work_description '
            f'FROM personnel_entries WHERE report_id IN ({зн})', пар).fetchall():
        ч = int(r['headcount'] or 0)
        if ч <= 0:
            continue
        з = по_орг.setdefault(r['contractor_id'], {'численности': [], 'дни': set(), 'работы': []})
        з['численности'].append(ч)
        з['дни'].add(дата_по_сводке.get(r['report_id']))
        оп = _текст(r['work_description'])
        if оп and оп not in з['работы']:
            з['работы'].append(оп)

    строки = []
    for cid, з in по_орг.items():
        орг = подрядчики.get(cid)
        ч = з['численности']
        строки.append({
            'contractor_id': cid,
            'наименование': орг['name'] if орг else f'Организация #{cid}',
            'средняя_численность': round(sum(ч) / len(ч), 1),
            'максимум': max(ч),
            'минимум': min(ч),
            'дней_присутствия': len(з['дни']),
            'виды_работ': з['работы'],
        })
    return {'строки': sorted(строки, key=lambda x: -x['средняя_численность'])}


def блок_as_built_docs(db, сводки, подрядчики):
    """Подрядчики, чья исполнительная документация проверялась."""
    if not сводки:
        return {'строки': []}
    зн, пар = _заглушки(db, _ids(сводки))
    дата_по_сводке = {s['id']: s['report_date'] for s in сводки}

    строки = []
    for r in db.execute(
            f'SELECT report_id, contractor_id, contractor_name, object_work, '
            f'ks2_number, ks3_number, has_id, has_ks6a, has_ks3 '
            f'FROM ks2_check WHERE report_id IN ({зн})', пар).fetchall():
        if not r['has_id']:
            continue                       # блок про проверку ИД
        орг = подрядчики.get(r['contractor_id'])
        строки.append({
            'дата': дата_по_сводке.get(r['report_id']),
            'наименование': (орг['name'] if орг else None) or _текст(r['contractor_name']) or '—',
            'работы': _текст(r['object_work']),
            'кс2': _текст(r['ks2_number']),
            'кс3': _текст(r['ks3_number']),
            'ид_проверена': True,
            'кс6а': bool(r['has_ks6a']),
        })
    return {'строки': sorted(строки, key=lambda x: (x['дата'] or '', x['наименование']))}


def блок_general(db, сводки):
    """Планёрки и совещания за период."""
    if not сводки:
        return {'строки': []}
    зн, пар = _заглушки(db, _ids(сводки))
    дата_по_сводке = {s['id']: s['report_date'] for s in сводки}
    строки = [{
        'дата': дата_по_сводке.get(r['report_id']),
        'время': _текст(r['time']),
        'место': _текст(r['location']),
        'участники': _текст(r['participants']),
        'вопросы': _текст(r['agenda']),
        'протокол': _текст(r['protocol_name']) or None,
    } for r in db.execute(
        f'SELECT report_id, time, location, participants, agenda, protocol_name '
        f'FROM meetings WHERE report_id IN ({зн})', пар).fetchall()]
    строки = [с for с in строки if с['вопросы'] or с['участники'] or с['протокол']]
    return {'строки': sorted(строки, key=lambda x: (x['дата'] or '', x['время']))}


def блок_photos(db, сводки, участки, подрядчики):
    """Приложение фотофиксации.

    Если у фото заполнена подпись — используем её. Иначе собираем из
    данных сводки: работы, участок, дата.
    """
    if not сводки:
        return {'строки': []}
    зн, пар = _заглушки(db, _ids(сводки))
    сводка_по_id = {s['id']: s for s in сводки}

    # Для автоподписи: что делали в этот день на этом участке
    работы = {}
    for таблица, поле in (('operational_control', 'work_stage'),
                          ('acceptance_control', 'work_stage')):
        for r in db.execute(
                f'SELECT report_id, section_id, {поле} AS этап FROM {таблица} '
                f'WHERE report_id IN ({зн})', пар).fetchall():
            если_есть = _текст(r['этап'])
            if если_есть:
                работы.setdefault(r['report_id'], []).append(
                    (r['section_id'], если_есть))

    строки = []
    for r in db.execute(
            f'SELECT id, report_id, file_path, caption, sort_order '
            f'FROM photos WHERE report_id IN ({зн}) '
            f'ORDER BY report_id, sort_order, id', пар).fetchall():
        подпись = _текст(r['caption'])
        if not подпись:
            куски = []
            варианты = работы.get(r['report_id']) or []
            if варианты:
                sid, этап = варианты[0]
                куски.append(этап)
                уч = участки.get(sid)
                if уч:
                    куски.append(уч)
            св = сводка_по_id.get(r['report_id'])
            if св:
                куски.append(св['report_date'])
            подпись = ', '.join(куски) if куски else 'Фотофиксация'
        строки.append({
            'photo_id': r['id'],
            'файл': r['file_path'],
            'подпись': подпись,
            'подпись_своя': not _пусто(r['caption']),
            'дата': (сводка_по_id.get(r['report_id']) or {}).get('report_date'),
        })
    return {'строки': строки}


# ─────────────────────────────────────────────────────────────
# СБОРКА ЦЕЛИКОМ
# ─────────────────────────────────────────────────────────────

def собрать(db, group_id, year, month):
    """Собирает все автоматические блоки отчёта за месяц по группе.

    Возвращает словарь, готовый к сохранению в monthly_report_blocks
    и к показу на экране. Ничего в базу не пишет — запись делает
    вызывающий код в app.py.
    """
    группа = db.execute(
        'SELECT id, name, client_name, report_title FROM report_groups WHERE id=?',
        (group_id,)).fetchone()
    if not группа:
        return None

    дата_с, дата_по = границы_месяца(year, month)
    объекты = объекты_группы(db, group_id, дата_с, дата_по)
    сводки = сводки_периода(db, объекты)
    участки = _участки(db, объекты)
    подрядчики = _подрядчики(db, объекты)

    блоки = {
        'title': блок_title(dict(группа), объекты, year, month, сводки),
        'control_summary': блок_control_summary(db, сводки, участки),
        'deviations': блок_deviations(db, сводки, участки, подрядчики),
        'prescriptions': блок_prescriptions(db, сводки, участки),
        'organizations': блок_organizations(db, объекты, сводки, подрядчики),
        'personnel': блок_personnel(db, сводки, подрядчики),
        'as_built_docs': блок_as_built_docs(db, сводки, подрядчики),
        'general': блок_general(db, сводки),
        'photos': блок_photos(db, сводки, участки, подрядчики),
    }
    for ключ in ПОКА_НЕ_СОБИРАЮТСЯ:
        блоки[ключ] = {'не_собирается': True}

    return {
        'группа': {'id': группа['id'], 'name': группа['name'],
                   'report_title': группа['report_title']},
        'период': {'year': year, 'month': month,
                   'подпись': подпись_периода(year, month),
                   'с': дата_с, 'по': дата_по},
        'объекты': [{'object_id': o['object_id'],
                     'наименование': o['report_name'] or o['name'],
                     'период_с': o['период_с'], 'период_по': o['период_по']}
                    for o in объекты],
        'сводок_отобрано': len(сводки),
        'блоки': блоки,
    }

#!/usr/bin/env python3
"""Перенос данных из SQLite в PostgreSQL с сохранением всех идентификаторов.

Идентификаторы сохраняются намеренно: часть связей в схеме держится на
числовых id без внешних ключей (contractors.partner_id, section_id в
таблицах инженера, partner_projects). Смена id тихо порвала бы их.

SQLite не проверяет типы, поэтому в числовой колонке может лежать пустая
строка — так в operational_control.section_id оказались пустые значения
вместо NULL. PostgreSQL строг и падает на такой строке. Скрипт приводит
пустые значения к NULL, опираясь на тип колонки в ЦЕЛЕВОЙ схеме, а не
угадывая по данным, и печатает, сколько значений и где было приведено.

Порядок действий:
    1. создать схему из db/schema_postgres.sql (если ещё не создана)
    2. проверить ВСЕ таблицы до загрузки: собрать приведения и блокеры
    3. очистить целевые таблицы (делает запуск повторяемым)
    4. загрузить таблицы в порядке зависимостей, с явными id
    5. выровнять счётчики identity через setval
    6. сверить количество строк по каждой таблице и напечатать отчёт

Запуск:

    python3 scripts/migrate_to_postgres.py \\
        --sqlite /var/sk-pilot/db/pilot.db \\
        --pg "host=localhost dbname=sk_pilot user=sk password=..." \\
        --truncate

Скрипт читает SQLite только на чтение и ничего в нём не меняет.
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# Порядок важен: сначала родительские таблицы, потом зависимые.
# Внешние ключи в схеме есть только на daily_reports(id).
LOAD_ORDER = [
    'organizations',
    'projects',
    'partners',
    'partner_projects',
    'objects',
    'sections',
    'contractors',
    'users',
    'object_users',
    'user_sections',
    'daily_reports',
    'personnel_entries',
    'input_control',
    'operational_control',
    'acceptance_control',
    'ks2_check',
    'verbal_remarks',
    'prescriptions_log',
    'meetings',
    'photos',
]

# Типы PostgreSQL, в которые нельзя положить пустую строку
NUMERIC_TYPES = {
    'smallint', 'integer', 'bigint', 'decimal', 'numeric',
    'real', 'double precision',
}


def sqlite_columns(sq, table):
    return [r[1] for r in sq.execute(f'PRAGMA table_info({table})').fetchall()]


def pg_schema(pg, table):
    """Тип и обязательность каждой колонки целевой таблицы."""
    cur = pg.cursor()
    cur.execute(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s", (table,))
    schema = {name: {'type': typ, 'nullable': nullable == 'YES'}
              for name, typ, nullable in cur.fetchall()}
    cur.close()
    return schema


def _пустое(v):
    """Пустая строка (в том числе из одних пробелов) — отсутствие значения."""
    return isinstance(v, str) and v.strip() == ''


def _число(v):
    """Можно ли значение положить в числовую колонку как есть."""
    if v is None or isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v.strip())
            return True
        except ValueError:
            return False
    return False


def проверить(sq, pg, table, cols):
    """Пред-проверка одной таблицы.

    Возвращает (приведения, блокеры):
      приведения — {колонка: количество пустых значений → NULL}
      блокеры    — список описаний проблем, требующих решения человеком
    """
    схема = pg_schema(pg, table)
    числовые = [c for c in cols
                if схема.get(c, {}).get('type') in NUMERIC_TYPES]
    if not числовые:
        return {}, []

    приведения = defaultdict(int)
    блокеры = []
    пустые_notnull = defaultdict(list)
    мусор = defaultdict(list)

    есть_id = 'id' in cols
    выборка = ', '.join(cols)
    for r in sq.execute(f'SELECT {выборка} FROM {table}').fetchall():
        rid = r['id'] if есть_id else '?'
        for c in числовые:
            v = r[c]
            if _пустое(v):
                if схема[c]['nullable']:
                    приведения[c] += 1
                else:
                    пустые_notnull[c].append(rid)
            elif not _число(v):
                мусор[c].append((rid, repr(v)[:40]))

    for c, ids in пустые_notnull.items():
        блокеры.append(
            f'{table}.{c}: пустое значение в колонке NOT NULL '
            f'({len(ids)} записей, id: {", ".join(map(str, ids[:20]))}'
            f'{"…" if len(ids) > 20 else ""}). '
            'Подставлять ноль нельзя — нужно решение человека.')
    for c, items in мусор.items():
        примеры = '; '.join(f'id {i} = {v}' for i, v in items[:10])
        блокеры.append(
            f'{table}.{c}: нечисловое значение в числовой колонке '
            f'({len(items)} записей). Примеры: {примеры}')
    return dict(приведения), блокеры


def подготовить(r, cols, числовые):
    """Кортеж для вставки: пустые значения числовых колонок → NULL."""
    out = []
    for c in cols:
        v = r[c]
        out.append(None if (c in числовые and _пустое(v)) else v)
    return tuple(out)


def main():
    ap = argparse.ArgumentParser(description='Перенос SK-pilot из SQLite в PostgreSQL')
    ap.add_argument('--sqlite', required=True, help='путь к файлу pilot.db')
    ap.add_argument('--pg', required=True, help='строка подключения psycopg2')
    ap.add_argument('--truncate', action='store_true',
                    help='очистить целевые таблицы перед загрузкой '
                         '(обязательно, если в них уже есть данные)')
    ap.add_argument('--dry-run', action='store_true',
                    help='только проверить данные, ничего не записывать')
    args = ap.parse_args()

    import psycopg2
    from psycopg2.extras import execute_values

    if not os.path.exists(args.sqlite):
        sys.exit(f'Не найден файл SQLite: {args.sqlite}')

    sq = sqlite3.connect(f'file:{args.sqlite}?mode=ro', uri=True)  # только чтение
    sq.row_factory = sqlite3.Row
    pg = psycopg2.connect(args.pg)

    print(f'Источник : {args.sqlite}')
    print(f'Приёмник : PostgreSQL\n')

    # ── 1. Схема ──────────────────────────────────────────────────────────
    ddl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'db', 'schema_postgres.sql')
    with open(ddl_path, encoding='utf-8') as f:
        cur = pg.cursor()
        cur.execute(f.read())
        cur.close()
    pg.commit()
    print('✅ Схема PostgreSQL готова\n')

    # Какие колонки реально переносим
    план = {}
    for table in LOAD_ORDER:
        try:
            src = sqlite_columns(sq, table)
        except sqlite3.OperationalError:
            план[table] = None          # таблицы нет в источнике
            continue
        схема = pg_schema(pg, table)
        cols = [c for c in src if c in схема]
        лишние = [c for c in src if c not in схема]
        if лишние:
            print(f'⚠️  {table}: нет в целевой схеме, не переносим: {", ".join(лишние)}')
        план[table] = cols

    # ── 2. Пред-проверка данных: находим ВСЕ проблемы до записи ───────────
    print('Проверка данных перед загрузкой...')
    все_приведения = {}
    все_блокеры = []
    for table, cols in план.items():
        if not cols:
            continue
        приведения, блокеры = проверить(sq, pg, table, cols)
        if приведения:
            все_приведения[table] = приведения
        все_блокеры.extend(блокеры)

    if все_приведения:
        всего = sum(sum(v.values()) for v in все_приведения.values())
        print(f'\n⚠️  ВНИМАНИЕ: пустых значений в числовых колонках — {всего}. '
              'Все они будут перенесены как NULL:')
        for table, кол in все_приведения.items():
            for c, n in кол.items():
                print(f'      {table}.{c}: {n}')
    else:
        print('   Пустых значений в числовых колонках не найдено')

    if все_блокеры:
        print('\n❌ ПЕРЕНОС ОСТАНОВЛЕН. Требуется решение человека:\n')
        for b in все_блокеры:
            print(f'   • {b}')
        print('\nНичего не записано. Исправьте данные в источнике и повторите.')
        sq.close(); pg.close()
        sys.exit(1)

    if args.dry_run:
        print('\n--dry-run: проверка пройдена, запись не выполнялась.')
        sq.close(); pg.close()
        return

    # ── 3. Очистка: делает повторный запуск безопасным ────────────────────
    cur = pg.cursor()
    занято = []
    for table in LOAD_ORDER:
        cur.execute(f'SELECT COUNT(*) FROM {table}')
        n = cur.fetchone()[0]
        if n:
            занято.append((table, n))
    cur.close()

    if занято and not args.truncate:
        print('\n❌ В целевой базе уже есть данные:')
        for t, n in занято:
            print(f'      {t}: {n}')
        print('\nПовторный перенос без очистки дал бы дубли и конфликты по id.')
        print('Запустите с флагом --truncate, чтобы очистить целевые таблицы.')
        sq.close(); pg.close()
        sys.exit(1)

    if занято:
        cur = pg.cursor()
        cur.execute('TRUNCATE TABLE ' + ', '.join(LOAD_ORDER) + ' RESTART IDENTITY CASCADE')
        cur.close()
        pg.commit()
        print(f'\n🧹 Очищено таблиц с данными: {len(занято)}')

    # ── 4. Загрузка ───────────────────────────────────────────────────────
    print()
    for table in LOAD_ORDER:
        cols = план[table]
        if cols is None:
            print(f'⏭  {table}: в SQLite отсутствует, пропускаем')
            continue
        if not cols:
            continue
        схема = pg_schema(pg, table)
        числовые = {c for c in cols if схема.get(c, {}).get('type') in NUMERIC_TYPES}

        rows = sq.execute(f'SELECT {", ".join(cols)} FROM {table}').fetchall()
        if rows:
            cur = pg.cursor()
            # OVERRIDING SYSTEM VALUE — потому что id объявлен GENERATED ALWAYS
            sql = (f'INSERT INTO {table} ({", ".join(cols)}) '
                   f'OVERRIDING SYSTEM VALUE VALUES %s')
            execute_values(cur, sql, [подготовить(r, cols, числовые) for r in rows])
            cur.close()
        pg.commit()
        пометка = ''
        if table in все_приведения:
            пометка = f'  (пустых → NULL: {sum(все_приведения[table].values())})'
        print(f'   {table:<22} перенесено строк: {len(rows)}{пометка}')

    # ── 5. Выравнивание счётчиков ─────────────────────────────────────────
    print()
    cur = pg.cursor()
    for table in LOAD_ORDER:
        cur.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1))", (table,))
    cur.close()
    pg.commit()
    print('✅ Счётчики identity выровнены по максимальному id\n')

    # ── 6. Сверка ─────────────────────────────────────────────────────────
    print('─' * 58)
    print(f'{"ТАБЛИЦА":<24}{"SQLite":>10}{"PostgreSQL":>14}{"":>8}')
    print('─' * 58)
    расхождения = 0
    cur = pg.cursor()
    for table in LOAD_ORDER:
        try:
            было = sq.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        except sqlite3.OperationalError:
            было = 0
        cur.execute(f'SELECT COUNT(*) FROM {table}')
        стало = cur.fetchone()[0]
        ok = было == стало
        if not ok:
            расхождения += 1
        print(f'{table:<24}{было:>10}{стало:>14}{"  ✅" if ok else "  ❌ РАСХОЖДЕНИЕ":>8}')
    cur.close()
    print('─' * 58)

    if все_приведения:
        всего = sum(sum(v.values()) for v in все_приведения.values())
        print(f'\n⚠️  Пустых значений приведено к NULL: {всего}')
        for table, кол in все_приведения.items():
            for c, n in кол.items():
                print(f'      {table}.{c}: {n}')

    sq.close()
    pg.close()

    if расхождения:
        sys.exit(f'\n❌ Расхождений: {расхождения}. Перенос НЕ подтверждён.')
    print('\n✅ Перенос завершён, количество строк совпадает по всем таблицам.')
    print('   Фотографии в uploads/ переносятся отдельно (rsync).')


if __name__ == '__main__':
    main()

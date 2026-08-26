#!/usr/bin/env python3
"""Перенос данных из SQLite в PostgreSQL с сохранением всех идентификаторов.

Идентификаторы сохраняются намеренно: часть связей в схеме держится на
числовых id без внешних ключей (contractors.partner_id, section_id в
таблицах инженера, partner_projects). Смена id тихо порвала бы их.

Запуск:

    python3 scripts/migrate_to_postgres.py \\
        --sqlite /var/sk-pilot/db/pilot.db \\
        --pg "host=localhost port=5432 dbname=sk_pilot user=sk password=..."

Порядок действий:
    1. создать схему из db/schema_postgres.sql (если ещё не создана)
    2. загрузить таблицы в порядке зависимостей, с явными id
    3. выровнять счётчики identity через setval
    4. сверить количество строк по каждой таблице и напечатать отчёт

Скрипт читает SQLite только на чтение и ничего в нём не меняет.
"""
import argparse
import os
import sqlite3
import sys

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


def sqlite_columns(sq, table):
    return [r[1] for r in sq.execute(f'PRAGMA table_info({table})').fetchall()]


def pg_columns(pg, table):
    cur = pg.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s", (table,))
    cols = [r[0] for r in cur.fetchall()]
    cur.close()
    return cols


def main():
    ap = argparse.ArgumentParser(description='Перенос SK-pilot из SQLite в PostgreSQL')
    ap.add_argument('--sqlite', required=True, help='путь к файлу pilot.db')
    ap.add_argument('--pg', required=True, help='строка подключения psycopg2')
    ap.add_argument('--truncate', action='store_true',
                    help='очистить целевые таблицы перед загрузкой')
    args = ap.parse_args()

    import psycopg2
    from psycopg2.extras import execute_values

    if not os.path.exists(args.sqlite):
        sys.exit(f'Не найден файл SQLite: {args.sqlite}')

    sq = sqlite3.connect(f'file:{args.sqlite}?mode=ro', uri=True)  # только чтение
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

    if args.truncate:
        cur = pg.cursor()
        cur.execute('TRUNCATE TABLE ' + ', '.join(LOAD_ORDER) + ' RESTART IDENTITY CASCADE')
        cur.close()
        pg.commit()
        print('🧹 Целевые таблицы очищены\n')

    # ── 2. Загрузка ───────────────────────────────────────────────────────
    sq.row_factory = sqlite3.Row
    итоги = []
    for table in LOAD_ORDER:
        try:
            src_cols = sqlite_columns(sq, table)
        except sqlite3.OperationalError:
            print(f'⏭  {table}: в SQLite отсутствует, пропускаем')
            итоги.append((table, 0, 0))
            continue
        if not src_cols:
            итоги.append((table, 0, 0))
            continue

        dst_cols = pg_columns(pg, table)
        cols = [c for c in src_cols if c in dst_cols]
        пропущено = [c for c in src_cols if c not in dst_cols]
        if пропущено:
            print(f'⚠️  {table}: колонок нет в целевой схеме, не переносим: {", ".join(пропущено)}')

        rows = sq.execute(f'SELECT {", ".join(cols)} FROM {table}').fetchall()
        if rows:
            cur = pg.cursor()
            # OVERRIDING SYSTEM VALUE — потому что id объявлен GENERATED ALWAYS
            sql = (f'INSERT INTO {table} ({", ".join(cols)}) '
                   f'OVERRIDING SYSTEM VALUE VALUES %s')
            execute_values(cur, sql, [tuple(r[c] for c in cols) for r in rows])
            cur.close()
        pg.commit()
        итоги.append((table, len(rows), None))
        print(f'   {table:<22} перенесено строк: {len(rows)}')

    # ── 3. Выравнивание счётчиков ─────────────────────────────────────────
    print()
    cur = pg.cursor()
    for table in LOAD_ORDER:
        cur.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1))", (table,))
    cur.close()
    pg.commit()
    print('✅ Счётчики identity выровнены по максимальному id\n')

    # ── 4. Сверка ─────────────────────────────────────────────────────────
    print('─' * 58)
    print(f'{"ТАБЛИЦА":<24}{"SQLite":>10}{"PostgreSQL":>14}{"":>8}')
    print('─' * 58)
    расхождения = 0
    cur = pg.cursor()
    for table, _, _ in итоги:
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

    sq.close()
    pg.close()

    if расхождения:
        sys.exit(f'\n❌ Расхождений: {расхождения}. Перенос НЕ подтверждён.')
    print('\n✅ Перенос завершён, количество строк совпадает по всем таблицам.')
    print('   Фотографии в uploads/ переносятся отдельно (rsync).')


if __name__ == '__main__':
    main()

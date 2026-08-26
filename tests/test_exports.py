"""Экспорты: за день, полный ZIP, резервная копия базы.

Проверяем код ответа, тип содержимого и то, что архив действительно
распаковывается и содержит ожидаемые файлы.
"""
import io
import os
import zipfile

import pytest

from conftest import IS_POSTGRES, OBJ_IN_ACTIVE_PROJECT, UID, as_role

DATE = '2026-03-17'
ПУСТАЯ_ДАТА = '2019-01-01'


def _сводка_с_данными(client):
    """Создаёт сданную сводку с персоналом — чтобы экспорту было что выгружать."""
    as_role(client, 'engineer')
    rid = client.post('/api/reports', json={
        'object_id': OBJ_IN_ACTIVE_PROJECT, 'user_id': UID['engineer'],
        'report_date': DATE}).get_json()['data']['id']
    cid = client.get(f'/api/objects/{OBJ_IN_ACTIVE_PROJECT}').get_json()['data']['contractors'][0]['id']
    client.post(f'/api/reports/{rid}/personnel',
                json=[{'contractor_id': cid, 'headcount': 7, 'work_description': 'Работы'}])
    client.post(f'/api/reports/{rid}/submit')
    client.post('/api/auth/logout')
    return rid


def _архив(resp):
    assert resp.status_code == 200
    assert resp.mimetype == 'application/zip', resp.mimetype
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    assert zf.testzip() is None, 'архив повреждён'
    return zf


# ── Экспорт за день ──────────────────────────────────────────────────────

def test_экспорт_за_день_с_данными(client):
    _сводка_с_данными(client)
    as_role(client, 'admin')
    zf = _архив(client.get(f'/api/admin/export_day?user_id={UID["admin"]}&date={DATE}'))
    names = zf.namelist()
    assert any(n.endswith('.csv') for n in names), names
    assert any(n.endswith('.txt') for n in names), names


def test_экспорт_за_день_имя_файла_содержит_дату(client):
    _сводка_с_данными(client)
    as_role(client, 'admin')
    r = client.get(f'/api/admin/export_day?user_id={UID["admin"]}&date={DATE}')
    assert DATE in r.headers.get('Content-Disposition', '')


def test_экспорт_за_пустую_дату_отдаёт_валидный_архив(client):
    """Ветка без данных должна вернуть корректный ZIP, а не оборвать ответ."""
    as_role(client, 'admin')
    zf = _архив(client.get(f'/api/admin/export_day?user_id={UID["admin"]}&date={ПУСТАЯ_ДАТА}'))
    assert zf.namelist() == ['нет_данных.txt']
    текст = zf.read('нет_данных.txt').decode('utf-8')
    assert ПУСТАЯ_ДАТА in текст


def test_экспорт_за_день_длина_ответа_совпадает(client):
    """Регрессия на обрыв соединения: Content-Length должен совпасть с телом."""
    as_role(client, 'admin')
    r = client.get(f'/api/admin/export_day?user_id={UID["admin"]}&date={ПУСТАЯ_ДАТА}')
    заявлено = r.headers.get('Content-Length')
    if заявлено is not None:
        assert int(заявлено) == len(r.data)


def test_экспорт_за_день_без_даты(client):
    as_role(client, 'admin')
    assert client.get(f'/api/admin/export_day?user_id={UID["admin"]}').status_code == 400


# ── Полный экспорт ───────────────────────────────────────────────────────

def test_полный_экспорт_zip(client):
    _сводка_с_данными(client)
    as_role(client, 'admin')
    zf = _архив(client.get(f'/api/admin/export_zip?user_id={UID["admin"]}'))
    names = zf.namelist()
    assert any('Сводки' in n for n in names), names


def test_полный_экспорт_на_пустой_базе(client):
    as_role(client, 'admin')
    _архив(client.get(f'/api/admin/export_zip?user_id={UID["admin"]}'))


# ── Резервная копия ──────────────────────────────────────────────────────

@pytest.mark.skipif(IS_POSTGRES, reason='у PostgreSQL нет файла базы — копия снимается pg_dump')
def test_резервная_копия_базы(client):
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    assert r.status_code == 200
    assert r.data[:16].startswith(b'SQLite format 3'), 'ожидался файл базы SQLite'


@pytest.mark.skipif(IS_POSTGRES, reason='у PostgreSQL нет файла базы — копия снимается pg_dump')
def test_резервная_копия_открывается_как_база(client, tmp_path):
    import sqlite3
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    f = tmp_path / 'backup.db'
    f.write_bytes(r.data)
    conn = sqlite3.connect(f)
    users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    assert users == 7


# ── Резервная копия на PostgreSQL: pg_dump ──────────────────────────────

def _pg_dump_доступен():
    """Есть ли рабочий pg_dump (путь берётся из SK_PG_DUMP или PATH)."""
    import subprocess
    binary = os.environ.get('SK_PG_DUMP') or 'pg_dump'
    try:
        return subprocess.run([binary, '--version'],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not IS_POSTGRES, reason='ветка PostgreSQL')
@pytest.mark.skipif(not _pg_dump_доступен(), reason='pg_dump недоступен в этом окружении')
def test_резервная_копия_postgres_отдаёт_дамп(client):
    """Дамп в формате custom: сигнатура PGDMP и дата в имени файла."""
    from datetime import date
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    assert r.status_code == 200, r.get_json()
    assert r.data[:5] == b'PGDMP', 'ожидался дамп формата custom (-Fc)'
    assert len(r.data) > 100
    имя = r.headers.get('Content-Disposition', '')
    assert '.dump' in имя
    assert date.today().isoformat() in имя


@pytest.mark.skipif(not IS_POSTGRES, reason='ветка PostgreSQL')
@pytest.mark.skipif(not _pg_dump_доступен(), reason='pg_dump недоступен в этом окружении')
def test_резервная_копия_postgres_восстанавливается(client, tmp_path):
    """Дамп должен читаться pg_restore — иначе это не резервная копия."""
    import subprocess
    restore = os.environ.get('SK_PG_RESTORE')
    if not restore:
        pytest.skip('pg_restore недоступен')
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    f = tmp_path / 'backup.dump'
    f.write_bytes(r.data)
    proc = subprocess.run([restore, '--list', str(f)], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')[:300]
    assert b'users' in proc.stdout


@pytest.mark.skipif(not IS_POSTGRES, reason='ветка PostgreSQL')
def test_резервная_копия_postgres_нет_pg_dump(client, monkeypatch):
    """Понятное сообщение вместо молчаливого пустого файла."""
    monkeypatch.setenv('SK_PG_DUMP', '/nonexistent/pg_dump')
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    assert r.status_code == 500
    текст = r.get_json()['error']
    assert 'pg_dump' in текст
    assert 'SK_PG_DUMP' in текст


@pytest.mark.skipif(not IS_POSTGRES, reason='ветка PostgreSQL')
def test_резервная_копия_postgres_ошибка_pg_dump(client, monkeypatch, tmp_path):
    """pg_dump завершился с ошибкой — отдаём её текст, а не пустой файл."""
    заглушка = tmp_path / 'fake_pg_dump'
    заглушка.write_text('#!/bin/sh\necho "could not connect to server" >&2\nexit 1\n')
    заглушка.chmod(0o755)
    monkeypatch.setenv('SK_PG_DUMP', str(заглушка))
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    assert r.status_code == 500
    assert 'could not connect' in r.get_json()['error']


@pytest.mark.skipif(not IS_POSTGRES, reason='ветка PostgreSQL')
def test_резервная_копия_postgres_пустой_файл(client, monkeypatch, tmp_path):
    """Успешный код возврата, но пустой файл — это не копия."""
    заглушка = tmp_path / 'empty_pg_dump'
    заглушка.write_text('#!/bin/sh\nexit 0\n')
    заглушка.chmod(0o755)
    monkeypatch.setenv('SK_PG_DUMP', str(заглушка))
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    assert r.status_code == 500
    assert 'пуст' in r.get_json()['error']


@pytest.mark.skipif(not IS_POSTGRES, reason='ветка PostgreSQL')
def test_пароль_не_попадает_в_аргументы_и_ответ(client, monkeypatch, tmp_path):
    """Пароль уходит через PGPASSWORD и не должен светиться в argv.

    Настоящий пароль не подменяем — иначе оборвётся подключение к базе.
    Подменяем только сам pg_dump на шпиона, который записывает аргументы.
    """
    from db.schema import pg_params
    пароль = pg_params()['password']
    if not пароль:
        pytest.skip('пароль не задан — проверять нечего')

    шпион = tmp_path / 'spy_pg_dump'
    лог = tmp_path / 'argv.txt'
    шпион.write_text(
        '#!/bin/sh\n'
        f'echo "$@" > {лог}\n'
        f'if [ -n "$PGPASSWORD" ]; then echo PGPASSWORD_ЕСТЬ >> {лог}; fi\n'
        'exit 1\n')
    шпион.chmod(0o755)
    monkeypatch.setenv('SK_PG_DUMP', str(шпион))

    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')

    строки = лог.read_text().splitlines()
    аргументы = строки[0].split() if строки else []
    # Сверяем аргументы поштучно: подстрочный поиск дал бы ложное срабатывание,
    # если пароль случайно совпадёт с куском имени базы или пользователя
    assert пароль not in аргументы, f'пароль передан отдельным аргументом: {аргументы}'
    assert not any(a.startswith('--password') for a in аргументы), аргументы
    assert 'PGPASSWORD_ЕСТЬ' in строки, 'PGPASSWORD не передан в окружение pg_dump'
    assert пароль not in str(r.get_json()), 'пароль попал в ответ пользователю'

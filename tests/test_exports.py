"""Экспорты: за день, полный ZIP, резервная копия базы.

Проверяем код ответа, тип содержимого и то, что архив действительно
распаковывается и содержит ожидаемые файлы.
"""
import io
import zipfile

from conftest import OBJ_IN_ACTIVE_PROJECT, UID, as_role

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

def test_резервная_копия_базы(client):
    as_role(client, 'root')
    r = client.get(f'/api/admin/backup_db?user_id={UID["root"]}')
    assert r.status_code == 200
    assert r.data[:16].startswith(b'SQLite format 3'), 'ожидался файл базы SQLite'


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

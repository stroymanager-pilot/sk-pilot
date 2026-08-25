"""Права доступа по ролям: root / admin / senior / engineer.

Матрица зафиксирована как есть на SQLite — после переезда на PostgreSQL
поведение должно совпасть строка в строку.
"""
import pytest

from conftest import EMAIL, UID, as_role

# Эндпоинты, доступные только администраторам организации (root и admin).
# {uid} подставляется: эти эндпоинты определяют пользователя по user_id
# из запроса, сессия лишь перекрывает переданное значение.
ADMIN_ONLY = [
    ('get', '/api/admin/export_zip?user_id={uid}'),
    ('get', '/api/admin/export_day?user_id={uid}&date=2026-01-01'),
    ('get', '/api/admin/backup_db?user_id={uid}'),
    ('get', '/api/admin/photo_check?user_id={uid}'),
    ('post', '/api/migrate?user_id={uid}'),
    ('post', '/api/migrate_v2?user_id={uid}'),
    ('post', '/api/fix_duplicates?user_id={uid}'),
]


def _call(client, method, url, role):
    return getattr(client, method)(url.format(uid=UID[role]))


@pytest.mark.parametrize('method,url', ADMIN_ONLY)
@pytest.mark.parametrize('role', ['root', 'admin'])
def test_админские_эндпоинты_доступны(client, role, method, url):
    as_role(client, role)
    assert _call(client, method, url, role).status_code == 200


@pytest.mark.parametrize('method,url', ADMIN_ONLY)
@pytest.mark.parametrize('role', ['senior', 'engineer'])
def test_админские_эндпоинты_закрыты(client, role, method, url):
    as_role(client, role)
    assert _call(client, method, url, role).status_code == 403


def test_админский_эндпоинт_без_user_id_отказывает(client):
    """Текущее поведение: сессия перекрывает user_id, но не подставляет его.
    Клиент обязан передавать параметр — фиксируем это как есть."""
    as_role(client, 'root')
    assert client.get('/api/admin/backup_db').status_code == 403


@pytest.mark.parametrize('role,ожидание', [
    ('root', 200),
    ('admin', 200),
    ('senior', 200),          # can_view_all=1
    ('senior_noview', 403),   # can_view_all=0
    ('engineer', 403),
])
def test_реестр_всех_сводок(client, role, ожидание):
    as_role(client, role)
    r = client.get(f'/api/all_reports?requester_id={UID[role]}')
    assert r.status_code == ожидание


@pytest.mark.parametrize('role,ожидание', [
    ('root', 200), ('admin', 200), ('senior', 200),
    ('senior_noview', 403), ('engineer', 403),
])
def test_все_фотографии(client, role, ожидание):
    as_role(client, role)
    assert client.get(f'/api/all_photos?requester_id={UID[role]}').status_code == ожидание


# ── Создание учётных записей ─────────────────────────────────────────────

@pytest.mark.parametrize('role,ожидание', [
    ('root', 201),      # только root заводит администраторов
    ('admin', 403),
    ('senior', 403),
    ('engineer', 403),
])
def test_создание_администратора(client, role, ожидание):
    as_role(client, role)
    r = client.post(f'/api/users?user_id={UID[role]}', json={
        'full_name': 'Новый Админ', 'email': 'new_admin@test.ru', 'role': 'admin'})
    assert r.status_code == ожидание


@pytest.mark.parametrize('role,ожидание', [
    ('root', 201), ('admin', 201), ('senior', 403), ('engineer', 403),
])
def test_создание_инженера(client, role, ожидание):
    as_role(client, role)
    r = client.post(f'/api/users?user_id={UID[role]}', json={
        'full_name': 'Новый Инженер', 'email': 'new_eng@test.ru', 'role': 'engineer'})
    assert r.status_code == ожидание


def test_создание_учётки_без_входа_запрещено(client):
    r = client.post('/api/users', json={
        'full_name': 'Аноним', 'email': 'anon@test.ru', 'role': 'admin'})
    assert r.status_code == 403


def test_недопустимая_роль_отклоняется(client):
    as_role(client, 'root')
    r = client.post(f'/api/users?user_id={UID["root"]}', json={
        'full_name': 'Кто-то', 'email': 'x@test.ru', 'role': 'platform'})
    assert r.status_code == 400


# ── Подмена user_id при активной сессии ──────────────────────────────────

def test_инженер_не_выдаёт_себя_за_root(client):
    """Вошедший по паролю опознаётся по сессии, а не по user_id из запроса."""
    as_role(client, 'engineer')
    r = client.get(f'/api/admin/export_day?user_id={UID["root"]}&date=2026-01-01')
    assert r.status_code == 403


def test_root_сохраняет_доступ_при_подменённом_user_id(client):
    as_role(client, 'root')
    r = client.get(f'/api/admin/export_day?user_id={UID["engineer"]}&date=2026-01-01')
    assert r.status_code == 200


def test_инженер_не_читает_чужой_реестр_подменой(client):
    as_role(client, 'engineer')
    assert client.get(f'/api/all_reports?requester_id={UID["root"]}').status_code == 403

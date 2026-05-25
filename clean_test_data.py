#!/usr/bin/env python3
"""
Очистка тестовых данных из базы.
Запуск: python3 /var/sk-pilot/clean_test_data.py
"""
import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'pilot.db')
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=== Очистка тестовых данных ===\n")

# 1. Показываем текущих подрядчиков объекта IQ Гатчина (id=2)
print("Текущие подрядчики IQ Гатчина (object_id=2):")
rows = conn.execute("SELECT id, name, work_type, is_active FROM contractors WHERE object_id=2").fetchall()
for r in rows:
    status = "активен" if r['is_active'] else "скрыт"
    print(f"  id={r['id']} | {r['name']} | {r['work_type']} | [{status}]")

# 2. Деактивируем тестовых подрядчиков (Гелиос, Фортес) — они не из этого проекта
test_names = ['ООО Гелиос', 'ООО Фортес', 'ООО Партнер', 'ООО Партнер 12']
for name in test_names:
    c.execute("UPDATE contractors SET is_active=0 WHERE name=? AND is_active=1", (name,))
    if c.rowcount:
        print(f"\nОК: деактивирован '{name}' ({c.rowcount} записей)")

# 3. Деактивируем тестовых партнёров
test_partners = ['ООО Партнер', 'ООО Партнер 12']
for name in test_partners:
    c.execute("UPDATE partners SET is_active=0 WHERE name=?", (name,))
    if c.rowcount:
        print(f"ОК: партнёр '{name}' деактивирован")

conn.commit()

print("\n=== Подрядчики после очистки ===")
rows = conn.execute("""
    SELECT id, name, work_type FROM contractors
    WHERE object_id=2 AND is_active=1
""").fetchall()
for r in rows:
    print(f"  ✓ {r['name']} | {r['work_type'] or '—'}")

print("\n=== Активные партнёры ===")
rows = conn.execute("SELECT name, type, project_id FROM partners WHERE is_active=1").fetchall()
for r in rows:
    proj = f"проект id={r['project_id']}" if r['project_id'] else "все проекты"
    print(f"  ✓ {r['name']} | {r['type']} | {proj}")

conn.close()
print("\nГотово! Обновите страницу в браузере.")

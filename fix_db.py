#!/usr/bin/env python3
"""
Скрипт очистки базы данных от тестовых данных.
Запуск: python3 /var/sk-pilot/fix_db.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'pilot.db')

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=== Исправление базы данных ===\n")

# 1. Перенести объект IQ Гатчина (id=2) в правильный проект (id=2)
c.execute("UPDATE objects SET project_id=2, name='IQ Гатчина' WHERE id=2")
print(f"OK: объект id=2 перенесён в проект IQ Гатчина, название исправлено ({c.rowcount} строк)")

# 2. Деактивировать дубликат (id=3)
c.execute("UPDATE objects SET is_active=0 WHERE id=3")
print(f"OK: объект id=3 (дубликат) деактивирован ({c.rowcount} строк)")

# 3. Деактивировать старый сводный объект id=1 если у него нет сводок
c.execute("SELECT COUNT(*) FROM daily_reports WHERE object_id=1")
count = c.fetchone()[0]
if count == 0:
    c.execute("UPDATE objects SET is_active=0 WHERE id=1")
    print(f"OK: объект id=1 (старый тестовый) деактивирован")
else:
    print(f"INFO: объект id=1 имеет {count} сводок — оставлен активным")

conn.commit()
conn.close()

print("\n=== Проверка результата ===")
conn2 = sqlite3.connect(DB_PATH)
conn2.row_factory = sqlite3.Row
rows = conn2.execute("""
    SELECT o.id, o.name, p.name as project, o.is_active
    FROM objects o
    LEFT JOIN projects p ON p.id=o.project_id
    ORDER BY o.id
""").fetchall()
for r in rows:
    status = "aktiv" if r['is_active'] else "OTKL"
    print(f"  [{status}] id={r['id']} | {r['project']} | {r['name']}")
conn2.close()

print("\nГотово! Обновите страницу в браузере.")

import sqlite3, hashlib, sys

db_path = sys.argv[1] if len(sys.argv)>1 else 'db/pilot.db'
db = sqlite3.connect(db_path)

db.executescript("""
CREATE TABLE IF NOT EXISTS acceptance_control (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    section_id INTEGER,
    work_stage TEXT,
    controlled_operations TEXT,
    control_method TEXT,
    engineer_id INTEGER
);
CREATE TABLE IF NOT EXISTS ks2_check (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    contractor_name TEXT,
    object_work TEXT,
    ks2_number TEXT,
    has_ks6a INTEGER DEFAULT 0,
    has_id INTEGER DEFAULT 0,
    engineer_id INTEGER
);
""")

pwd = hashlib.sha256(b'password123').hexdigest()
db.execute("INSERT OR IGNORE INTO users (full_name,email,role,tj_user_id,password_hash) VALUES ('Ухов Илья Викторович','uhov@stroymanager.ru','engineer','uhov_tj_001','"+pwd+"')")
db.execute("INSERT OR IGNORE INTO objects (name,address,client_name,tj_object_id) VALUES ('IQ Гатчина (участок 6)','Ленинградская обл., г. Гатчина','ЛСТ Генподряд','tj_gatchina_006')")

gid = db.execute("SELECT id FROM objects WHERE tj_object_id='tj_gatchina_006'").fetchone()[0]
uid = db.execute("SELECT id FROM users WHERE email='uhov@stroymanager.ru'").fetchone()[0]
aid = db.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]

for name in ['Пятно застройки','Блок 3','ПОС']:
    db.execute("INSERT OR IGNORE INTO sections (object_id,name) VALUES (?,?)",(gid,name))
for name,wt in [('ООО Гелиос','ПОС, замещение грунта'),('ООО Фортес','Лидерное бурение, сваи')]:
    db.execute("INSERT OR IGNORE INTO contractors (object_id,name,work_type) VALUES (?,?,?)",(gid,name,wt))
db.execute("INSERT OR IGNORE INTO object_users (object_id,user_id) VALUES (?,?)",(gid,uid))
db.execute("INSERT OR IGNORE INTO object_users (object_id,user_id) VALUES (?,?)",(gid,aid))

db.commit()
db.close()
print("OK — миграция выполнена успешно!")

#!/bin/bash
set -e
echo "1) Останавливаю приложение, чтобы освободить базу..."
systemctl stop sk-pilot
sleep 2
echo "2) Делаю свежий бэкап..."
sqlite3 /var/sk-pilot/db/pilot.db ".backup /var/sk-pilot/db/pilot-backup-fix.db"
echo "3) Применяю правку к обеим таблицам контроля..."
sqlite3 /var/sk-pilot/db/pilot.db "PRAGMA foreign_keys=OFF; BEGIN; CREATE TABLE operational_control_new (id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE, section_id INTEGER, work_stage TEXT, controlled_operations TEXT, control_method TEXT, engineer_id INTEGER, status TEXT DEFAULT '', deviation_note TEXT DEFAULT '', contractor_id INTEGER); INSERT INTO operational_control_new SELECT id, report_id, section_id, work_stage, controlled_operations, control_method, engineer_id, status, deviation_note, contractor_id FROM operational_control; DROP TABLE operational_control; ALTER TABLE operational_control_new RENAME TO operational_control; CREATE TABLE acceptance_control_new (id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE, section_id INTEGER, work_stage TEXT, controlled_operations TEXT, control_method TEXT, engineer_id INTEGER, status TEXT DEFAULT '', deviation_note TEXT DEFAULT '', contractor_id INTEGER); INSERT INTO acceptance_control_new SELECT id, report_id, section_id, work_stage, controlled_operations, control_method, engineer_id, status, deviation_note, contractor_id FROM acceptance_control; DROP TABLE acceptance_control; ALTER TABLE acceptance_control_new RENAME TO acceptance_control; COMMIT;"
echo "4) Запускаю приложение обратно..."
systemctl start sk-pilot
sleep 2
echo ""
echo "=== РЕЗУЛЬТАТ operational_control ==="
sqlite3 /var/sk-pilot/db/pilot.db ".schema operational_control"
echo ""
echo "=== РЕЗУЛЬТАТ acceptance_control ==="
sqlite3 /var/sk-pilot/db/pilot.db ".schema acceptance_control"
echo ""
echo "=== Статус приложения ==="
systemctl is-active sk-pilot

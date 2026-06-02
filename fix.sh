#!/bin/bash
sqlite3 /var/sk-pilot/db/pilot.db ".backup /var/sk-pilot/db/pilot-backup-3.db"
sqlite3 /var/sk-pilot/db/pilot.db "PRAGMA foreign_keys=OFF; BEGIN; CREATE TABLE operational_control_new (id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE, section_id INTEGER, work_stage TEXT, controlled_operations TEXT, control_method TEXT, engineer_id INTEGER, status TEXT DEFAULT '', deviation_note TEXT DEFAULT '', contractor_id INTEGER); INSERT INTO operational_control_new SELECT id, report_id, section_id, work_stage, controlled_operations, control_method, engineer_id, status, deviation_note, contractor_id FROM operational_control; DROP TABLE operational_control; ALTER TABLE operational_control_new RENAME TO operational_control; COMMIT;"
echo "=== ГОТОВО. Новая структура operational_control: ==="
sqlite3 /var/sk-pilot/db/pilot.db ".schema operational_control"

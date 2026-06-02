#!/bin/bash
set -e
echo "1) Останавливаю приложение..."
systemctl stop sk-pilot
sleep 2
echo "2) Свежий бэкап..."
sqlite3 /var/sk-pilot/db/pilot.db ".backup /var/sk-pilot/db/pilot-backup-cleanup.db"
echo "3) Удаляю тестовые сводки id 3, 4, 5 (Глуховской и Чувашов по Гатчине)..."
sqlite3 /var/sk-pilot/db/pilot.db "PRAGMA foreign_keys=ON; DELETE FROM daily_reports WHERE id IN (3,4,5);"
echo "4) Запускаю приложение обратно..."
systemctl start sk-pilot
sleep 2
echo ""
echo "=== Что осталось по Гатчине (должен быть только Ухов): ==="
sqlite3 /var/sk-pilot/db/pilot.db "SELECT dr.id, dr.report_date, u.full_name, o.name FROM daily_reports dr JOIN users u ON u.id=dr.user_id JOIN objects o ON o.id=dr.object_id WHERE o.name LIKE '%Гатчина%' ORDER BY u.full_name;"
echo "=== Статус приложения ==="
systemctl is-active sk-pilot

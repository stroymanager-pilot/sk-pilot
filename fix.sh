#!/bin/bash
echo "=== Сводки по объектам, где инженер-автор НЕ совпадает с тем, что ожидается ==="
echo "--- Все сводки по 'IQ Гатчина' с авторами: ---"
sqlite3 /var/sk-pilot/db/pilot.db "SELECT dr.id, dr.report_date, u.full_name, o.name FROM daily_reports dr JOIN users u ON u.id=dr.user_id JOIN objects o ON o.id=dr.object_id WHERE o.name LIKE '%Гатчина%' ORDER BY u.full_name;"

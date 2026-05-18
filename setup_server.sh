#!/bin/bash
set -e

echo "=== SK Pilot: настройка сервера ==="

# 1. Исправляем/создаём сервис
cat > /etc/systemd/system/sk-pilot.service << 'SVCEOF'
[Unit]
Description=SK Pilot
After=network.target

[Service]
WorkingDirectory=/var/sk-pilot
ExecStart=/usr/local/bin/gunicorn -w 2 -b 127.0.0.1:5001 app:app
Restart=always
User=root

[Install]
WantedBy=multi-user.target
SVCEOF

echo "✅ Файл сервиса создан"

# 2. Создаём конфиг nginx
cat > /etc/nginx/conf.d/sk-pilot.conf << 'NGXEOF'
server {
    listen 80;
    server_name sk.stroymanager.ru;

    client_max_body_size 50M;

    location /uploads/ {
        alias /var/sk-pilot/uploads/;
    }

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
NGXEOF

echo "✅ Конфиг nginx создан"

# 3. Создаём папку для загрузок
mkdir -p /var/sk-pilot/uploads

# 4. Инициализируем БД (на случай если не было)
cd /var/sk-pilot && python3 db/schema.py

# 5. Запускаем сервис
systemctl daemon-reload
systemctl enable sk-pilot
systemctl restart sk-pilot
sleep 2
systemctl status sk-pilot --no-pager

# 6. Проверяем nginx
nginx -t && systemctl reload nginx

echo ""
echo "=== Готово! ==="
echo "Проверьте: http://sk.stroymanager.ru"
echo ""
echo "Для SSL выполните:"
echo "  yum install -y certbot python2-certbot-nginx"
echo "  certbot --nginx -d sk.stroymanager.ru"

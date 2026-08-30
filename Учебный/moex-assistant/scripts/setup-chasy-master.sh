#!/bin/bash
# Setup script for chasy-master.ru on the same server as MOEX Assistant
# Run this on the server as root

set -e

echo "=== Step 1: Install nginx ==="
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

echo "=== Step 2: Create website directory ==="
mkdir -p /var/www/chasy-master.ru
cd /var/www/chasy-master.ru

echo "=== Step 3: Download website files ==="
curl -sO https://chasy-master.ru/index.html
curl -sO https://chasy-master.ru/styles.css
curl -sO https://chasy-master.ru/script.js
curl -sO https://chasy-master.ru/master.jpg
curl -sO https://chasy-master.ru/workshop1.png
curl -sO https://chasy-master.ru/workshop2.png
curl -sO https://chasy-master.ru/video.mp4
curl -sO https://chasy-master.ru/advice-accuracy.html
curl -sO https://chasy-master.ru/advice-mechanical.html
curl -sO https://chasy-master.ru/advice-storage.html
curl -sO https://chasy-master.ru/advice-when-repair.html
curl -sO https://chasy-master.ru/favicon-16x16.png
curl -sO https://chasy-master.ru/favicon-32x32.png
curl -sO https://chasy-master.ru/apple-touch-icon.png

echo "=== Step 4: Fix permissions ==="
chown -R www-data:www-data /var/www/chasy-master.ru

echo "=== Step 5: Download nginx config ==="
curl -sL -o /etc/nginx/sites-available/chasy-master.ru https://raw.githubusercontent.com/mixa6901pann-lang/-moex-assistant/master/scripts/nginx-chasy-master.conf

echo "=== Step 6: Enable site ==="
ln -sf /etc/nginx/sites-available/chasy-master.ru /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "=== Step 7: Open firewall ports ==="
ufw allow 'Nginx Full' || true
ufw allow 80/tcp || true
ufw allow 443/tcp || true

echo "=== Step 8: Obtain SSL certificate ==="
certbot --nginx -d chasy-master.ru -d www.chasy-master.ru --non-interactive --agree-tos -m admin@chasy-master.ru || true

echo "=== Done ==="
echo "Website files are in /var/www/chasy-master.ru"
echo "Next: change your domain A-record to 201.51.1.244 in Timeweb panel"

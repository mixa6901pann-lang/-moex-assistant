#!/bin/bash
# setup-security.sh — Server hardening for MOEX Assistant
# Run ON THE SERVER as root: bash setup-security.sh
set -e

PROJECT_DIR="/root/moex-assistant"
REAL_DIR="$PROJECT_DIR/Учебный/mоex-assistant"
APP_LINK="/root/moex-app"
USER_NAME="moex"
LOG_DIR="$APP_LINK/logs"
DATA_DIR="$APP_LINK/data"
BACKUP_DIR="/backup/moex-assistant"

# ── 0. Fix Cyrillic path via symlink ─────────────────────────
echo "[0/8] Creating symlink $APP_LINK -> $REAL_DIR..."
if [ -d "$REAL_DIR" ]; then
    ln -sf "$REAL_DIR" "$APP_LINK"
    echo "Symlink created."
else
    echo "WARNING: Real directory $REAL_DIR not found. Skipping symlink."
fi

# ── 1. Create dedicated user ────────────────────────────────
echo "[1/8] Creating user '$USER_NAME'..."
if ! id "$USER_NAME" &>/dev/null; then
    useradd -m -s /bin/bash "$USER_NAME"
    echo "User $USER_NAME created."
else
    echo "User $USER_NAME already exists."
fi

# Allow moex to read project files (in /root this requires ACL or move)
# For simplicity: add moex to root group temporarily, or we chown selectively
gpasswd -a "$USER_NAME" root 2>/dev/null || true

# ── 2. Fix file permissions ─────────────────────────────────
echo "[2/8] Setting file permissions..."
# Database: only owner readable
if [ -f "$DATA_DIR/moex.db" ]; then
    chmod 600 "$DATA_DIR/moex.db"
    chown "$USER_NAME":"$USER_NAME" "$DATA_DIR/moex.db"
fi
# .env with keys
if [ -f "$APP_LINK/.env" ]; then
    chmod 600 "$APP_LINK/.env"
    chown root:root "$APP_LINK/.env"
fi
# Logs
mkdir -p "$LOG_DIR"
chmod 750 "$LOG_DIR"
chown "$USER_NAME":"$USER_NAME" "$LOG_DIR"
# Ensure moex can write to logs
touch "$LOG_DIR/moex.out.log" "$LOG_DIR/moex.err.log" 2>/dev/null || true
chown "$USER_NAME":"$USER_NAME" "$LOG_DIR"/*.log 2>/dev/null || true

# ── 3. UFW firewall ───────────────────────────────────────
echo "[3/8] Configuring UFW firewall..."
apt-get update -qq
apt-get install -y -qq ufw

ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
# Do NOT expose the app port (8080) to the internet. The app binds 127.0.0.1:8080;
# access it via Cloudflare Tunnel or nginx reverse-proxy with HTTPS + auth.
# If nginx is installed, open 80/443.
if command -v nginx &>/dev/null; then
    ufw allow 80/tcp comment 'HTTP'
    ufw allow 443/tcp comment 'HTTPS'
fi
ufw --force enable
ufw status verbose

# ── 4. fail2ban ────────────────────────────────────────────
echo "[4/8] Installing fail2ban..."
apt-get install -y -qq fail2ban

cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5
backend = systemd

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 5

[nginx-http-auth]
enabled = true
filter = nginx-http-auth
port = http,https
logpath = /var/log/nginx/error.log
EOF

systemctl restart fail2ban
systemctl enable fail2ban
fail2ban-client status

# ── 5. SSH hardening (safe) ────────────────────────────────
echo "[5/8] Hardening SSH..."
SSH_KEY_EXISTS=false
if [ -s /root/.ssh/authorized_keys ]; then
    SSH_KEY_EXISTS=true
fi

cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%s)

# Apply safe settings regardless
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#*MaxAuthTries.*/MaxAuthTries 5/' /etc/ssh/sshd_config
sed -i 's/^#*ClientAliveInterval.*/ClientAliveInterval 300/' /etc/ssh/sshd_config
sed -i 's/^#*ClientAliveCountMax.*/ClientAliveCountMax 2/' /etc/ssh/sshd_config

if [ "$SSH_KEY_EXISTS" = true ]; then
    echo "SSH key detected. Disabling password auth."
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
else
    echo "WARNING: No SSH key found in /root/.ssh/authorized_keys."
    echo "Keeping PasswordAuthentication enabled for now."
    echo "Run 'ssh-copy-id' from your PC first, then re-run this script."
fi

systemctl restart sshd

# ── 6. logrotate ───────────────────────────────────────────
echo "[6/8] Setting up logrotate..."
cat > /etc/logrotate.d/moex-assistant <<EOF
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $USER_NAME $USER_NAME
    sharedscripts
    postrotate
        /usr/bin/killall -HUP rsyslogd 2>/dev/null || true
    endscript
}
EOF

# ── 7. Backup directory + cron ────────────────────────────
echo "[7/8] Setting up backups..."
mkdir -p "$BACKUP_DIR"
chown "$USER_NAME":"$USER_NAME" "$BACKUP_DIR"

# Create backup script
BACKUP_SCRIPT="$APP_LINK/scripts/backup-db.sh"
mkdir -p "$APP_LINK/scripts"
cat > "$BACKUP_SCRIPT" <<EOF
#!/bin/bash
# Backup SQLite DB — keep 7 latest copies
SRC="$DATA_DIR/moex.db"
DST="$BACKUP_DIR"
TS=\$(date +%Y%m%d_%H%M%S)
if [ -f "\$SRC" ]; then
    cp "\$SRC" "\$DST/moex_db_\$TS.db"
    gzip -f "\$DST/moex_db_\$TS.db"
    # Keep only 7 latest backups
    ls -t "\$DST"/moex_db_*.db.gz 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null || true
fi
EOF
chmod +x "$BACKUP_SCRIPT"

# Add to root crontab
(crontab -l 2>/dev/null || true; echo "0 3 * * * $BACKUP_SCRIPT") | sort -u | crontab -

if [ "$SSH_KEY_EXISTS" = false ]; then
    echo ""
    echo "⚠️  ACTION REQUIRED:"
    echo "   No SSH key detected. Your server still allows password login."
    echo "   Generate a key locally and run: ssh-copy-id root@201.51.1.244"
    echo "   Then re-run this script to disable password auth."
fi

# ── 8. Update supervisor config ─────────────────────────────
echo "[8/8] Updating supervisor config..."
cp "$APP_LINK/scripts/supervisor-moex.conf" /etc/supervisor/conf.d/moex.conf
supervisorctl reread
supervisorctl update
supervisorctl restart moex

echo ""
echo "==============================================="
echo "Security setup complete!"
echo "==============================================="
echo "- App symlink: $APP_LINK -> $REAL_DIR"
echo "- User: $USER_NAME (member of root group for file access)"
echo "- Firewall: $(ufw status | grep -c 'ALLOW') rules active"
echo "- fail2ban: running ($(fail2ban-client status sshd | grep 'Currently banned' || echo 'see fail2ban-client status'))"
echo "- Backups: $BACKUP_DIR (cron daily 03:00)"
echo "- Supervisor: moex restarted"
echo ""

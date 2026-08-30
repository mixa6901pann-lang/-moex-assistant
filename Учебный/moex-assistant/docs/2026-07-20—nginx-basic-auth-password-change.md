# Смена пароля HTTP Basic Auth для invest-assistant.ru

Дата: 20 июля 2026

## Что настроено

Сайт `https://invest-assistant.ru` защищён HTTP Basic Authentication через nginx.

- **Логин:** `mixa6901pann`
- **Файл паролей на сервере:** `/etc/nginx/.htpasswd-moex`
- **Конфиг nginx:** `/etc/nginx/sites-available/moex-assistant.ru`

## Как сменить пароль

### 1. Подключись к серверу по SSH

```bash
ssh -p 2222 root@201.51.1.244
```

### 2. Установи утилиту htpasswd (если ещё не установлена)

```bash
apt-get update && apt-get install -y apache2-utils
```

### 3. Задай новый пароль

```bash
htpasswd -B /etc/nginx/.htpasswd-moex mixa6901pann
```

Утилита спросит новый пароль дважды. Старый пароль при этом заменится.

### 4. Проверь права файла

```bash
chown root:www-data /etc/nginx/.htpasswd-moex
chmod 640 /etc/nginx/.htpasswd-moex
```

### 5. Перезагрузи nginx

```bash
nginx -t && systemctl reload nginx
```

### 6. Проверь вход

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u mixa6901pann:НОВЫЙ_ПАРОЛЬ https://invest-assistant.ru/
```

Должно вернуться `200`.

## Важно

- Храни пароль в надёжном месте (менеджер паролей).
- После смены старый пароль сразу перестаёт работать.
- Не добавляй второго пользователя в `/etc/nginx/.htpasswd-moex` без необходимости — доступ должен оставаться только у владельца.

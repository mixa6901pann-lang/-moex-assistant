# Деплой на Timeweb Cloud

## 1. Купить сервер
- timeweb.cloud → Облачные серверы
- Ubuntu 22.04 LTS, тариф на 1 ГБ RAM (~150–200 ₽/мес)
- Скопировать IP-адрес и пароль root из письма/панели

## 2. Подключиться к серверу
На своём компьютере открыть PowerShell (или Terminal) и ввести:
```bash
ssh root@IP_АДРЕС_СЕРВЕРА
```
Ввести пароль root. Готово — ты внутри сервера.

## 3. Загрузить проект на сервер
**Вариант А — через GitHub (рекомендую):**
```bash
cd /root
git clone https://github.com/ТВОЙ_НИК/moex-assistant.git
cd moex-assistant
```

**Вариант Б — без GitHub, прямо с компьютера:**
На своём компьютере в новом окне PowerShell:
```bash
cd C:\Users\МИХАИЛ\Учебный\moex-assistant
scp -r . root@IP_АДРЕС_СЕРВЕРА:/root/moex-assistant
```

## 4. Установить всё на сервере
Выполнить один скрипт (уже лежит в проекте):
```bash
cd /root/moex-assistant
bash scripts/setup-server.sh
```

Этот скрипт сам установит Python, создаст виртуальное окружение, поставит все библиотеки из `pyproject.toml`, установит supervisor, скопирует его конфиг и создаст папку для логов.

## 5. Создать файл `.env`
На сервере нужно создать `.env` с токенами:
```bash
nano /root/moex-assistant/.env
```

Минимальный `.env`:
```
TELEGRAM_BOT_TOKEN=токен_бота
TELEGRAM_CHAT_ID=id_чата
ANTHROPIC_API_KEY=ключ_claude
```

Если нужен VK:
```
VK_ACCESS_TOKEN=токен_vk
VK_GROUP_ID=id_группы
VK_ENABLED=true
```

**Важно про прокси:** переменная `TELEGRAM_PROXY` управляет прокси для Telegram. Если на сервере Telegram не заблокирован (Timeweb Cloud обычно не блокирует), просто не указывай `TELEGRAM_PROXY` в `.env` — бот подключится напрямую. Если заблокирован — укажи `TELEGRAM_PROXY=socks5://127.0.0.1:10808` и запусти Tor-прокси отдельно.

Сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`.

## 6. Запустить бота
```bash
supervisorctl reread
supervisorctl update
supervisorctl start moex
```

Проверить статус:
```bash
supervisorctl status moex
```

Смотреть логи:
```bash
tail -f /root/moex-assistant/logs/moex.out.log   # stdout
tail -f /root/moex-assistant/logs/moex.err.log   # stderr
```

## 7. (Опционально) Запуск Tor-прокси
Если Telegram заблокирован на сервере, нужен Tor:
```bash
apt install -y tor
# Настроить Tor на SOCKS5 порт 10808:
echo "SocksPort 10808" >> /etc/tor/torrc
systemctl restart tor
```
И добавить в `.env`:
```
TELEGRAM_PROXY=socks5://127.0.0.1:10808
```

## Перезапуск и управление
```bash
supervisorctl restart moex   # перезапуск
supervisorctl stop moex      # остановить
supervisorctl start moex     # запустить
```

## Обновление кода
```bash
cd /root/moex-assistant
git pull
pip install .
supervisorctl restart moex
```
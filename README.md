# Perplexity Gmail Bot

Telegram-бот, который парсит письма от Perplexity из Gmail и отправляет код верификации и ссылку для входа.

## Пример вывода

```
📩 Perplexity
📅 19 Mar 2026 12:57:20

🔑 Код: 463784

🔗 Войти
```

## Требования

- Python 3.11+
- Telegram Bot Token (от [@BotFather](https://t.me/BotFather))
- Google Cloud OAuth credentials (`credentials.json`)
- Ваш Telegram Chat ID (от [@userinfobot](https://t.me/userinfobot))

## Настройка Google Cloud

1. [Google Cloud Console](https://console.cloud.google.com/) — создать проект
2. Включить **Gmail API**: APIs & Services → Library → Gmail API → Enable
3. Создать **OAuth credentials**: APIs & Services → Credentials → Create Credentials → OAuth client ID → Desktop App
4. Скачать JSON и сохранить как `credentials.json`
5. OAuth consent screen → External → добавить scope `gmail.readonly` → добавить email в Test Users

## Установка

```bash
git clone <repo-url>
cd parsing_mail
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Конфигурация

Скопировать `.env.example` в `.env` и заполнить:

```env
TELEGRAM_BOT_TOKEN=your_token_here
ALLOWED_CHAT_IDS=123456789,987654321
CHECK_INTERVAL_SECONDS=300
PERPLEXITY_SENDER=perplexity
MAX_EMAILS_PER_CHECK=10
CREDENTIALS_FILE=credentials.json
TOKEN_FILE=token.json
```

## Запуск

```bash
python perplexity_gmail_bot.py
```

При первом запуске откроется браузер для авторизации Google.

## Деплой на VDS (systemd)

```bash
sudo nano /etc/systemd/system/perplexity-bot.service
```

```ini
[Unit]
Description=Perplexity Gmail Bot
After=network.target

[Service]
Type=simple
User=myuser
WorkingDirectory=/opt/parser_gmail
ExecStart=/opt/parser_gmail/.venv/bin/python perplexity_gmail_bot.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/parser_gmail/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable perplexity-bot
sudo systemctl start perplexity-bot
sudo systemctl status perplexity-bot
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/check` | Проверить новые письма |
| `/last` | Последние 5 писем |
| `/auto_on` | Включить автопроверку |
| `/auto_off` | Выключить автопроверку |
| `/status` | Статус бота |

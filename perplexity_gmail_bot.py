import os
import re
import base64
import html
import logging
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
PERPLEXITY_SENDER = os.getenv("PERPLEXITY_SENDER", "perplexity")
MAX_EMAILS_PER_CHECK = int(os.getenv("MAX_EMAILS_PER_CHECK", "10"))
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("TOKEN_FILE", "token.json")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

seen_message_ids: set[str] = set()


def get_gmail_service():
    creds = None
    token_path = Path(TOKEN_FILE)
    creds_path = Path(CREDENTIALS_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(f"{CREDENTIALS_FILE} not found")
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def extract_text_from_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def get_message_body(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if mime_type == "text/plain" and not parts:
        return decode_body(payload)
    if mime_type == "text/html" and not parts:
        return extract_text_from_html(decode_body(payload))

    text_parts = []
    html_parts = []
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain":
            text_parts.append(decode_body(part))
        elif part_mime == "text/html":
            html_parts.append(extract_text_from_html(decode_body(part)))
        elif "multipart" in part_mime:
            text_parts.append(get_message_body(part))

    if text_parts:
        return "\n".join(text_parts)
    if html_parts:
        return "\n".join(html_parts)
    return ""


def get_raw_html(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if mime_type == "text/html" and not parts:
        return decode_body(payload)

    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/html":
            return decode_body(part)
        elif "multipart" in part_mime:
            result = get_raw_html(part)
            if result:
                return result
    return ""


def parse_perplexity_email(payload: dict) -> dict | None:
    raw_html = get_raw_html(payload)
    body_text = get_message_body(payload)

    link = None
    code = None

    if raw_html:
        soup = BeautifulSoup(raw_html, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            text = a_tag.get_text(strip=True)

            if "perplexity.ai" in href:
                href_lower = href.lower()
                if any(kw in href_lower for kw in ("sign", "auth", "login", "token")):
                    link = href
                    break

            text_lower = text.lower()
            if any(kw in text_lower for kw in ("sign in", "log in", "verify")):
                link = href
                break

    if body_text:
        match = re.search(r"\b(\d{6})\b", body_text)
        if match:
            code = match.group(1)

    if not code and not link:
        return None

    return {"code": code, "link": link}


def get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch_perplexity_emails(service, max_results: int = 10) -> list[dict]:
    query = f"from:{PERPLEXITY_SENDER}"
    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )

    emails = []
    for msg_meta in results.get("messages", []):
        msg_id = msg_meta["id"]
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )

        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        parsed = parse_perplexity_email(payload)

        if not parsed:
            continue

        emails.append({
            "id": msg_id,
            "subject": get_header(headers, "Subject"),
            "sender": get_header(headers, "From"),
            "date": get_header(headers, "Date"),
            "code": parsed["code"],
            "link": parsed["link"],
        })

    return emails


def format_email(email: dict) -> str:
    lines = [
        "📩 <b>Perplexity</b>",
        f"📅 {html.escape(email['date'])}",
    ]
    if email.get("code"):
        lines.append(f"\n🔑 Код: <code>{html.escape(email['code'])}</code>")
    if email.get("link"):
        safe_link = email["link"].replace("&", "&amp;")
        lines.append(f'\n🔗 <a href="{safe_link}">Войти</a>')
    return "\n".join(lines)


def is_authorized(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    await update.message.reply_text(
        "👋 Привет! Я бот для парсинга писем от Perplexity.\n\n"
        "/check — проверить новые письма\n"
        "/last — последние 5 писем\n"
        "/auto_on — включить автопроверку\n"
        "/auto_off — выключить автопроверку\n"
        "/status — статус"
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return

    await update.message.reply_text("🔍 Проверяю...")

    try:
        service = get_gmail_service()
        emails = fetch_perplexity_emails(service, MAX_EMAILS_PER_CHECK)
        new_emails = [e for e in emails if e["id"] not in seen_message_ids]

        if not new_emails:
            await update.message.reply_text("📭 Новых писем нет.")
            return

        for email in new_emails:
            seen_message_ids.add(email["id"])
            await update.message.reply_text(format_email(email), parse_mode="HTML")

        await update.message.reply_text(f"✅ Найдено: {len(new_emails)}")

    except Exception as e:
        logger.error("Ошибка при проверке: %s", e)
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return

    await update.message.reply_text("📬 Загружаю...")

    try:
        service = get_gmail_service()
        emails = fetch_perplexity_emails(service, max_results=5)

        if not emails:
            await update.message.reply_text("📭 Писем не найдено.")
            return

        for email in emails:
            seen_message_ids.add(email["id"])
            await update.message.reply_text(format_email(email), parse_mode="HTML")

    except Exception as e:
        logger.error("Ошибка: %s", e)
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def auto_check_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    try:
        service = get_gmail_service()
        emails = fetch_perplexity_emails(service, MAX_EMAILS_PER_CHECK)
        new_emails = [e for e in emails if e["id"] not in seen_message_ids]

        for email in new_emails:
            seen_message_ids.add(email["id"])
            await context.bot.send_message(
                chat_id=chat_id, text=format_email(email), parse_mode="HTML"
            )

        if new_emails:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔔 Найдено {len(new_emails)} новых писем.",
            )

    except Exception as e:
        logger.error("Ошибка автопроверки: %s", e)


async def cmd_auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return

    chat_id = update.effective_chat.id
    job_name = f"auto_check_{chat_id}"

    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_repeating(
        auto_check_job,
        interval=CHECK_INTERVAL_SECONDS,
        first=10,
        chat_id=chat_id,
        name=job_name,
    )

    await update.message.reply_text(
        f"✅ Автопроверка включена! Каждые {CHECK_INTERVAL_SECONDS // 60} мин."
    )


async def cmd_auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return

    chat_id = update.effective_chat.id
    job_name = f"auto_check_{chat_id}"

    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    await update.message.reply_text("⏹ Автопроверка выключена.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return

    chat_id = update.effective_chat.id
    job_name = f"auto_check_{chat_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    auto_status = "✅ Включена" if jobs else "⏹ Выключена"

    await update.message.reply_text(
        f"📊 <b>Статус</b>\n\n"
        f"Автопроверка: {auto_status}\n"
        f"Интервал: {CHECK_INTERVAL_SECONDS // 60} мин\n"
        f"Обработано: {len(seen_message_ids)}",
        parse_mode="HTML",
    )


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Начало работы"),
        BotCommand("check", "Проверить новые письма"),
        BotCommand("last", "Последние 5 писем"),
        BotCommand("auto_on", "Включить автопроверку"),
        BotCommand("auto_off", "Выключить автопроверку"),
        BotCommand("status", "Статус бота"),
    ])


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Укажите TELEGRAM_BOT_TOKEN в .env")
        return

    print("🚀 Запуск бота...")

    try:
        service = get_gmail_service()
        profile = service.users().getProfile(userId="me").execute()
        print(f"✅ Gmail: {profile['emailAddress']}")
    except Exception as e:
        print(f"⚠️  Gmail: {e}")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("auto_on", cmd_auto_on))
    app.add_handler(CommandHandler("auto_off", cmd_auto_off))
    app.add_handler(CommandHandler("status", cmd_status))

    print("✅ Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

import os
import json
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)

from openai import OpenAI

# ---------- Логи ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "AI Idea Lab Leads")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")  # https://<your>.onrender.com
PORT = int(os.getenv("PORT", "8000"))

# Секретный путь вебхука. Если не задан, используем сам токен (надёжно и просто).
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", TELEGRAM_TOKEN)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан")

if not OPENAI_API_KEY:
    log.warning("⚠️ OPENAI_API_KEY не задан — идеи генерироваться не будут.")

# ---------- Google Sheets ----------
def connect_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON не задан")

    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON: невалидный JSON")

    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key("1uo3yOGDLrA5d9PCeZSEfVepcIuk3raYlFKTpFeVlWgQ")
    log.info("✅ Подключено к Google Sheet: %s", SPREADSHEET_NAME)

    # Гарантируем заголовки
    ws = sh.sheet1
    headers = ws.row_values(1)
    wanted = ["timestamp", "chat_id", "budget", "skills", "time_per_week", "ideas_text"]
    if headers != wanted:
        ws.clear()
        ws.append_row(wanted)
    return ws

SHEET = connect_sheet()

# ---------- OpenAI ----------
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def generate_ideas(budget: str, skills: str, time_per_week: str) -> str:
    fallback = (
        "✅ Готово! Вот 3 идеи под твои условия:\n\n"
        "1) Чат-ассистент для ниши, где ты шаришь (шаблоны + автозапуски).\n"
        "2) Микросервис с ИИ-ответами на часто задаваемые вопросы (подписка).\n"
        "3) Пакет шаблонов промптов/воркфлоу под конкретную боль (разовая продажа + апсейл).\n"
    )
    if not client:
        return fallback

    prompt = f"""
Ты — продуктовый консультант. Сгенерируй три реалистичные идеи микробизнеса на базе ИИ-чатов.
Условия:
- Бюджет на старт: {budget}
- Навыки/интересы: {skills}
- Время в неделю: {time_per_week}

Формат ответа:
— Короткое название
— Что это даёт пользователю (1–2 предложения)
— 3 шага старта
— Как монетизировать (1–2 варианта)
Сделай лаконично и по делу.
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты помогаешь запускать простые бизнесы на ИИ, отвечай кратко и практично."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=700,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("OpenAI error: %s", e)
        return fallback

# ---------- Состояния диалога ----------
BUDGET, SKILLS, TIMEPW = range(3)

START_TEXT = (
    "Привет! Я 🤖 *AI Idea Lab*.\n"
    "Задам 3 вопроса и подберу идеи микробизнеса под твои условия.\n\n"
    "💰 Сколько денег готов вложить на старте?\n"
    "_Примеры: 0, 1000, 5000_"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, parse_mode="Markdown")
    return BUDGET

async def catch_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["budget"] = (update.message.text or "").strip()
    await update.message.reply_text("🧠 Какие у тебя навыки или интересы? _Напиши через запятую_", parse_mode="Markdown")
    return SKILLS

async def catch_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["skills"] = (update.message.text or "").strip()
    await update.message.reply_text("⏱ Сколько времени готов уделять в неделю?\n_Пример: >10 часов/нед_", parse_mode="Markdown")
    return TIMEPW

async def catch_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["time_per_week"] = (update.message.text or "").strip()
    await update.message.reply_text("⏳ Генерирую идеи... это займёт пару секунд ⌛")

    budget = context.user_data.get("budget", "")
    skills = context.user_data.get("skills", "")
    timepw = context.user_data.get("time_per_week", "")

    ideas = generate_ideas(budget, skills, timepw)

    # Сохраняем лид в Google Sheet
    try:
        SHEET.append_row([
            datetime.utcnow().isoformat(),
            str(update.effective_chat.id),
            budget,
            skills,
            timepw,
            ideas
        ])
    except Exception as e:
        log.error("Ошибка записи в Google Sheet: %s", e)

    # ✉️ Уведомляем админа
    try:
        admin_chat_id = 6159527584  # твой chat_id
        await context.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "📥 Новый лид!\n\n"
                f"💰 Бюджет: {budget}\n"
                f"🧠 Навыки: {skills}\n"
                f"⏱ Время: {timepw}\n\n"
                f"💡 Сгенерированные идеи:\n{ideas}"
            )
        )
    except Exception as e:
        log.error("Не удалось отправить сообщение админу: %s", e)

    # Ответ пользователю
    text = (
        "✅ Готово! Вот идеи под твои условия:\n\n"
        f"{ideas}\n\n"
        "Если хочешь — напиши */more* и я докину дополнительные шаги запуска."
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END

async def more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 Доп.шаги:\n"
        "1) Выбери 1 идею и опиши её в 10 строк (что/для кого/ценность).\n"
        "2) Составь список 10 мест, где есть твоя аудитория (чаты/каналы/форумы).\n"
        "3) Подготовь 1 бесплатный лид-магнит (чек-лист/шаблон) и предложи его.\n"
        "4) Сделай 3 итерации по фидбеку.\n\n"
        "Готов выдать ещё? Напиши */start*.",
        parse_mode="Markdown",
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, завершаю. Можешь написать /start, когда будешь готов.")
    return ConversationHandler.END

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_budget)],
            SKILLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_skills)],
            TIMEPW: [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("more", more))
    return app

if __name__ == "__main__":
    app = build_app()
    webhook_url = f"{WEBHOOK_BASE_URL.rstrip('/')}/{WEBHOOK_PATH}"
    log.info("🌐 Запускаю webhook: %s", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True
    )

import os
import json
import logging
import hashlib
import time
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.constants import ParseMode
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
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")  # https://<your>.onrender.com (для Render)
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", TELEGRAM_TOKEN)  # секретный путь вебхука
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # строкой; напр., "6159527584"
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
HASH_SALT = os.getenv("HASH_SALT", "ai-idea-lab-salt")  # произвольная строка
LOG_SHEET_ID = os.getenv("LOG_SHEET_ID")  # ID Google Sheet для логов (open_by_key)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан")

if not OPENAI_API_KEY:
    log.warning("⚠️ OPENAI_API_KEY не задан — идеи генерироваться не будут.")

# ---------- Google Sheets ----------
def _gc_client():
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
    return gspread.authorize(creds)

def connect_sheet():
    client = _gc_client()
    sh = client.open_by_key("1uo3yOGDLrA5d9PCeZSEfVepcIuk3raYlFKTpFeVlWgQ")
    log.info("✅ Подключено к Google Sheet: %s", SPREADSHEET_NAME)
    ws = sh.sheet1
    headers = ws.row_values(1)
    wanted = ["timestamp", "chat_id_hash", "budget", "skills", "time_per_week", "ideas_text"]
    if headers != wanted:
        ws.clear()
        ws.append_row(wanted)
    return ws

def connect_log_sheet():
    """Подключение к таблице логов (минимальные поля). Безопасно: только хэш и тип события."""
    if not LOG_SHEET_ID:
        log.warning("⚠️ LOG_SHEET_ID не задан — логирование в Sheets выключено.")
        return None
    try:
        client = _gc_client()
        sh = client.open_by_key(LOG_SHEET_ID)
        ws = sh.sheet1  # используем первый лист
        headers = ws.row_values(1)
        wanted = ["timestamp", "chat_id_hash", "event"]
        if headers != wanted:
            ws.clear()
            ws.append_row(wanted)
        log.info("📝 Лог-таблица подключена")
        return ws
    except Exception as e:
        log.warning("Не удалось подключить лог-таблицу: %s", e)
        return None

SHEET = connect_sheet()
LOGS_WS = connect_log_sheet()

def prune_old_rows(ws, retention_days: int = 30):
    """Мягкая чистка: удаляем строки старше retention_days (если timestamp валиден)."""
    try:
        data = ws.get_all_values()
        if len(data) <= 1:
            return
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        to_delete = []
        for idx, row in enumerate(data[1:], start=2):
            ts = row[0].strip() if len(row) > 0 else ""
            try:
                dt = datetime.fromisoformat(ts)
                if dt < cutoff:
                    to_delete.append(idx)
            except Exception:
                continue
        deleted = 0
        for r in reversed(to_delete):
            ws.delete_rows(r)
            deleted += 1
        log.info("🧹 Очистка завершена: удалено %d строк, осталось %d записей", deleted, len(ws.get_all_values()) - 1)
    except Exception as e:
        log.warning("Не удалось выполнить очистку: %s", e)

prune_old_rows(SHEET, RETENTION_DAYS)

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

# ---------- Безопасные утилиты ----------
def hash_chat_id(chat_id: int) -> str:
    s = f"{HASH_SALT}:{chat_id}".encode("utf-8")
    return hashlib.sha256(s).hexdigest()

def log_event(chat_id: int, event: str):
    """Логируем минимум: timestamp, chat_id_hash, event."""
    if not LOGS_WS:
        return
    try:
        LOGS_WS.append_row([datetime.utcnow().isoformat(), hash_chat_id(chat_id), event])
    except Exception as e:
        log.warning("Не удалось записать лог (%s): %s", event, e)

# Антиспам: 1 событие / 2 сек на чат
_LAST_EVENT_AT = {}
RATE_WINDOW_SEC = 2.0
def rate_ok(chat_id: int) -> bool:
    now = time.monotonic()
    last = _LAST_EVENT_AT.get(chat_id, 0.0)
    if now - last < RATE_WINDOW_SEC:
        return False
    _LAST_EVENT_AT[chat_id] = now
    return True

# ---------- Тексты /privacy и /terms ----------
PRIVACY_TEXT = (
    "*Политика конфиденциальности*\n\n"
    "AI Idea Lab обрабатывает только данные, которые ты сам вводишь в чат-боте "
    "(бюджет, навыки, время и ответы на вопросы).\n\n"
    "Мы *не собираем и не храним*: имена, телефоны, email, адреса; финансовую/медицинскую информацию; "
    "иную персональную информацию, по которой тебя можно идентифицировать.\n\n"
    "Данные используются *исключительно* для подбора идей микробизнеса и их генерации с помощью ИИ. "
    "Мы не передаём данные третьим лицам и не используем их для рекламы.\n\n"
    "Данные автоматически удаляются через 30 дней или по твоему запросу командой /erase.\n"
)

TERMS_TEXT = (
    "*Условия использования*\n\n"
    "1) Используя бот AI Idea Lab, ты подтверждаешь, что ознакомился с /privacy и согласен с ним.\n"
    "2) Ответы бота носят информационный характер и не являются юридической/финансовой консультацией.\n"
    "3) Ответственность за применение идей лежит на пользователе.\n"
    "4) Функционал и условия могут обновляться — актуальная версия публикуется здесь.\n"
    "5) Если не согласен — не используй бот.\n"
)

# ---------- Состояния диалога ----------
CONSENT, BUDGET, SKILLS, TIMEPW = range(4)

START_TEXT = (
    "Привет! Я 🤖 *AI Idea Lab*.\n\n"
    "Перед стартом: я обработаю твои ответы (бюджет/навыки/время) только для подбора идей. "
    "Подробности: /privacy и /terms.\n\n"
    "Если согласен — напиши *СОГЛАСЕН* (именно это слово)."
)

# ---------- Хендлеры ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return
    log_event(update.effective_chat.id, "start")
    await update.message.reply_text(START_TEXT, parse_mode=ParseMode.MARKDOWN)
    return CONSENT

async def consent_catch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return CONSENT
    text = (update.message.text or "").strip().upper()
    if text != "СОГЛАСЕН":
        await update.message.reply_text(
            "Чтобы продолжить, напиши *СОГЛАСЕН*. "
            "Посмотреть правила: /privacy и /terms",
            parse_mode=ParseMode.MARKDOWN
        )
        return CONSENT

    log_event(update.effective_chat.id, "consent_accepted")
    await update.message.reply_text(
        "Ок! Начинаем.\n\n"
        "💰 Сколько денег готов вложить на старте?\n_Примеры: 0, 1000, 5000_",
        parse_mode=ParseMode.MARKDOWN
    )
    return BUDGET

async def catch_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return BUDGET
    context.user_data["budget"] = (update.message.text or "").strip()
    log_event(update.effective_chat.id, "budget_provided")
    await update.message.reply_text("🧠 Какие у тебя навыки или интересы? _Напиши через запятую_", parse_mode=ParseMode.MARKDOWN)
    return SKILLS

async def catch_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return SKILLS
    context.user_data["skills"] = (update.message.text or "").strip()
    log_event(update.effective_chat.id, "skills_provided")
    await update.message.reply_text("⏱ Сколько времени готов уделять в неделю?\n_Пример: >10 часов/нед_", parse_mode=ParseMode.MARKDOWN)
    return TIMEPW

async def catch_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return ConversationHandler.END
    context.user_data["time_per_week"] = (update.message.text or "").strip()
    await update.message.reply_text("⏳ Генерирую идеи... это займёт пару секунд ⌛")

    budget = context.user_data.get("budget", "")
    skills = context.user_data.get("skills", "")
    timepw = context.user_data.get("time_per_week", "")

    ideas = generate_ideas(budget, skills, timepw)

    # Сохраняем минимум и только хэш чата
    try:
        chat_id_hash = hash_chat_id(update.effective_chat.id)
        SHEET.append_row([
            datetime.utcnow().isoformat(),
            chat_id_hash,
            budget,
            skills,
            timepw,
            ideas
        ])
    except Exception as e:
        log.error("Ошибка записи в Google Sheet: %s", e)

    log_event(update.effective_chat.id, "ideas_generated")

    # Уведомление админу (если задан)
    try:
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=(
                    "📥 *Новый лид!*\n\n"
                    f"💰 Бюджет: {budget}\n"
                    f"🧠 Навыки: {skills}\n"
                    f"⏱ Время: {timepw}\n\n"
                    f"💡 Идеи:\n{ideas}"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        log.warning("Не удалось отправить уведомление админу: %s", e)

    await update.message.reply_text(
        "✅ Готово! Вот идеи под твои условия:\n\n"
        f"{ideas}\n\n"
        "Если хочешь — напиши */more* и я докину дополнительные шаги запуска.\n\n"
        "Команды: /privacy /terms /erase /about",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return
    log_event(update.effective_chat.id, "more")
    await update.message.reply_text(
        "🔧 Доп.шаги:\n"
        "1) Выбери 1 идею и опиши её в 10 строк (что/для кого/ценность).\n"
        "2) Составь список 10 мест, где есть твоя аудитория (чаты/каналы/форумы).\n"
        "3) Подготовь 1 бесплатный лид-магнит (чек-лист/шаблон) и предложи его.\n"
        "4) Сделай 3 итерации по фидбеку.\n\n"
        "Готов выдать ещё? Напиши */start*.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return
    log_event(update.effective_chat.id, "privacy")
    await update.message.reply_text(PRIVACY_TEXT, parse_mode=ParseMode.MARKDOWN)

async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return
    log_event(update.effective_chat.id, "terms")
    await update.message.reply_text(TERMS_TEXT, parse_mode=ParseMode.MARKDOWN)

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return
    log_event(update.effective_chat.id, "about")
    await update.message.reply_text(
        "🤖 *AI Idea Lab*\n\n"
        "Этот бот подбирает идеи микробизнеса под твой бюджет, навыки и время.\n\n"
        "📊 Как это работает:\n"
        "1️⃣ Отвечаешь на 3 вопроса.\n"
        "2️⃣ Получаешь 3 реальные идеи с пошаговым планом.\n"
        "3️⃣ Можешь запустить проект всего за 7 дней.\n\n"
        "🔧 Основные команды:\n"
        "/start — начать подбор идей\n"
        "/privacy — политика конфиденциальности\n"
        "/terms — условия использования\n"
        "/erase — удалить свои данные",
        parse_mode=ParseMode.MARKDOWN
    )

async def erase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return
    """Удаляет все строки, относящиеся к этому пользователю (по chat_id_hash)."""
    log_event(update.effective_chat.id, "erase_called")
    try:
        chat_id_hash = hash_chat_id(update.effective_chat.id)
        data = SHEET.get_all_values()
        if len(data) <= 1:
            await update.message.reply_text("Нечего удалять — данных нет.")
            return

        to_delete = []
        for idx, row in enumerate(data[1:], start=2):
            if len(row) > 1 and row[1] == chat_id_hash:
                to_delete.append(idx)

        if not to_delete:
            await update.message.reply_text("Данных по тебе не найдено. Уже чисто ✨")
            return

        for r in reversed(to_delete):
            SHEET.delete_rows(r)
        log_event(update.effective_chat.id, f"erase_done:{len(to_delete)}")
        await update.message.reply_text(f"Готово. Удалено записей: {len(to_delete)} ✅")
    except Exception as e:
        log.error("Ошибка при /erase: %s", e)
        await update.message.reply_text("Не удалось удалить данные. Попробуй позже.")

# ---------- Глобальная очистка всех данных (только для администратора) ----------
async def admin_clear_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return ConversationHandler.END
    if str(update.effective_chat.id) != str(ADMIN_CHAT_ID):
        await update.message.reply_text("🚫 У тебя нет прав для этой команды.")
        return ConversationHandler.END

    log_event(update.effective_chat.id, "admin_clear_requested")
    await update.message.reply_text(
        "⚠️ ВНИМАНИЕ: это удалит *все данные всех пользователей* без возможности восстановления.\n\n"
        "Если ты точно уверен — напиши: ПОДТВЕРЖДАЮ"
    )
    return 1

async def admin_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return ConversationHandler.END
    if str(update.effective_chat.id) != str(ADMIN_CHAT_ID):
        await update.message.reply_text("🚫 У тебя нет прав для этой команды.")
        return ConversationHandler.END

    if update.message.text.strip().upper() == "ПОДТВЕРЖДАЮ":
        try:
            SHEET.clear()
            SHEET.append_row(["timestamp", "chat_id_hash", "budget", "skills", "time_per_week", "ideas_text"])
            log_event(update.effective_chat.id, "admin_clear_done")
            await update.message.reply_text("🧹 Все данные успешно удалены ✅")
        except Exception as e:
            log.error("Ошибка при глобальной очистке: %s", e)
            await update.message.reply_text("❌ Ошибка при удалении данных.")
    else:
        await update.message.reply_text("❌ Очистка отменена.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, завершаю. Можешь написать /start, когда будешь готов.")
    return ConversationHandler.END

async def not_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_ok(update.effective_chat.id):
        return
    log_event(update.effective_chat.id, "non_text_message")
    await update.message.reply_text("Пожалуйста, ответь текстом. Если хочешь начать заново — /start")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Ошибка в обработке апдейта: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Ой! Что-то пошло не так. Попробуй ещё раз 🙏")
    except Exception:
        pass

# ---------- Application ----------
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CONSENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, consent_catch)],
            BUDGET:  [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_budget)],
            SKILLS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_skills)],
            TIMEPW:  [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("more", more))
    app.add_handler(CommandHandler("privacy", privacy))
    app.add_handler(CommandHandler("terms", terms))
    app.add_handler(CommandHandler("erase", erase))
    app.add_handler(CommandHandler("about", about))
    # --- Команда полной очистки (только админ) ---
    admin_clear_conv = ConversationHandler(
        entry_points=[CommandHandler("admin_clear", admin_clear_start)],
        states={1: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_clear_confirm)]},
        fallbacks=[],
    )
    app.add_handler(admin_clear_conv)

    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, not_text))
    app.add_error_handler(error_handler)
    return app

# ---------- Запуск ----------
if __name__ == "__main__":
    app = build_app()

    if WEBHOOK_BASE_URL:
        webhook_url = f"{WEBHOOK_BASE_URL.rstrip('/')}/{WEBHOOK_PATH}"
        log.info("🌐 Запускаю webhook: %s", webhook_url)

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=webhook_url,
            drop_pending_updates=True,
            stop_signals=None,
        )
    else:
        log.info("🤖 Бот запущен в режиме polling")
        app.run_polling(drop_pending_updates=True)

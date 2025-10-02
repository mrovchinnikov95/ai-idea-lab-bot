import os
import json
import logging
import hashlib
from datetime import datetime, timedelta

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

WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")  # если задан, работаем через webhook
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", TELEGRAM_TOKEN)  # секретный путь вебхука

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # 6159527584 (заполни в Render)
DATA_RETENTION_DAYS = int(os.getenv("DATA_RETENTION_DAYS", "90"))  # срок хранения

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан")

if not OPENAI_API_KEY:
    log.warning("⚠️ OPENAI_API_KEY не задан — идеи генерироваться не будут.")

# ---------- Тексты политики и условий (встраиваем прямо в бота) ----------
PRIVACY_TEXT = (
    "🔒 *Политика конфиденциальности — AI Idea Lab*\n"
    "_Дата: 02.10.2025_\n\n"
    "1) Какие данные: только то, что вы вводите в чат (бюджет/навыки/время), а также тех.метаданные Telegram.\n"
    "2) Зачем: подбор идей и улучшение подсказок.\n"
    "3) Хранение: в защищённой Google-таблице, по умолчанию 90 дней. chat_id сохраняем в виде хэша.\n"
    "4) Передача: Telegram, Google (Sheets), OpenAI — только для работы сервиса. Не продаём данные.\n"
    "5) Безопасность: минимум данных, хеширование chat_id, логи без ПДн, регулярная очистка.\n"
    "6) Ваши права: доступ/исправление/удаление/копия — пишите на contact.aiidealab@gmail.com или команда /erase.\n"
)

TERMS_TEXT = (
    "📄 *Пользовательское соглашение — AI Idea Lab*\n"
    "_Дата: 02.10.2025_\n\n"
    "1) Сервис: бот выдаёт идеи и шаги. Не финсовет/не гарантия дохода.\n"
    "2) Запрещено: незаконный контент, спам, ПДн третьих лиц без их согласия.\n"
    "3) Ответственность: материалы «как есть», без гарантий результатов.\n"
    "4) ИС: тексты бота защищены; всё, что создаёте по идеям — ваше.\n"
    "5) Конфиденциальность: см. /privacy.\n"
    "6) Изменения: можем обновлять условия.\n"
    "7) Контакты: contact.aiidealab@gmail.com\n"
)

# ---------- Google Sheets ----------
HEADERS = ["timestamp", "chat_id", "budget", "skills", "time_per_week", "ideas_text"]

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
    ws = sh.sheet1

    # Гарантируем заголовки ровно как нужно
    headers = ws.row_values(1)
    if headers != HEADERS:
        ws.clear()
        ws.append_row(HEADERS)

    log.info("✅ Подключено к Google Sheet: %s", SPREADSHEET_NAME)
    return ws

def cleanup_old_rows(ws, days: int):
    """Удаляет записи старше N дней, оставляя заголовки."""
    if days <= 0:
        return
    try:
        records = ws.get_all_records()  # список словарей, начиная со 2-й строки
        cutoff = datetime.utcnow() - timedelta(days=days)
        to_keep = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts >= cutoff:
                    to_keep.append([r[h] for h in HEADERS])
            except Exception:
                # если timestamp кривой — не удаляем на всякий
                to_keep.append([r[h] for h in HEADERS])

        ws.clear()
        ws.append_row(HEADERS)
        if to_keep:
            ws.append_rows(to_keep)
        log.info("🧹 Очистка завершена: осталось %d записей", len(to_keep))
    except Exception as e:
        log.error("Ошибка очистки: %s", e)

SHEET = connect_sheet()
cleanup_old_rows(SHEET, DATA_RETENTION_DAYS)

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

Формат:
— Короткое название
— 1–2 предложения ценности
— 3 шага старта
— 1–2 варианта монетизации
Лаконично.
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

# ---------- Хелперы ----------
def hash_chat_id(cid: int) -> str:
    return hashlib.sha256(str(cid).encode("utf-8")).hexdigest()[:16]

# ---------- Состояния диалога ----------
CONSENT, BUDGET, SKILLS, TIMEPW = range(4)

CONSENT_TEXT = (
    "Привет! Я 🤖 *AI Idea Lab*.\n\n"
    "Перед стартом: я обработаю твои ответы (бюджет/навыки/время) только для подбора идей. "
    "Подробности: /privacy и /terms.\n\n"
    "Если согласен — напиши *СОГЛАСЕН* (именно это слово)."
)

START_TEXT = (
    "Ок! Начинаем.\n\n"
    "💰 Сколько денег готов вложить на старте?\n"
    "_Примеры: 0, 1000, 5000_"
)

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(CONSENT_TEXT, parse_mode="Markdown")
    return CONSENT

async def catch_consent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().upper()
    if text != "СОГЛАСЕН":
        await update.message.reply_text("Ок! Если передумаешь — напиши /start.")
        return ConversationHandler.END
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
    budget = (context.user_data.get("budget") or "").strip()
    skills = (context.user_data.get("skills") or "").strip()
    timepw = (update.message.text or "").strip()
    context.user_data["time_per_week"] = timepw

    await update.message.reply_text("⏳ Генерирую идеи... это займёт пару секунд ⌛")

    ideas = generate_ideas(budget, skills, timepw)

    # Сохраняем только минимум, chat_id обезличиваем
    cid_hashed = hash_chat_id(update.effective_chat.id)
    try:
        SHEET.append_row([
            datetime.utcnow().isoformat(),
            cid_hashed,   # вместо реального chat_id
            budget,
            skills,
            timepw,
            ideas
        ])
    except Exception as e:
        log.error("Ошибка записи в Google Sheet: %s", e)

    # Админу — только обезличенное уведомление
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    "📥 Новый лид (обезличено)\n"
                    f"• chat: {cid_hashed}\n"
                    "• статус: идеи сгенерированы и отправлены пользователю."
                )
            )
        except Exception as e:
            log.error("Не удалось оповестить админа: %s", e)

    # Ответ пользователю
    text = (
        "✅ Готово! Вот идеи под твои условия:\n\n"
        f"{ideas}\n\n"
        "Если хочешь — напиши */more* и я докину дополнительные шаги запуска."
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END

async def cmd_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 Доп.шаги:\n"
        "1) Выбери 1 идею и опиши её в 10 строк (что/для кого/ценность).\n"
        "2) Составь список 10 мест, где есть твоя аудитория (чаты/каналы/форумы).\n"
        "3) Подготовь 1 бесплатный лид-магнит (чек-лист/шаблон) и предложи его.\n"
        "4) Сделай 3 итерации по фидбеку.\n\n"
        "Готов выдать ещё? Напиши */start*.",
        parse_mode="Markdown",
    )

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PRIVACY_TEXT, parse_mode="Markdown")

async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TERMS_TEXT, parse_mode="Markdown")

async def cmd_erase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление всех записей пользователя (по хэшу chat_id)."""
    cid_hashed = hash_chat_id(update.effective_chat.id)
    try:
        all_values = SHEET.get_all_values()  # список списков
        if not all_values or len(all_values) < 2:
            await update.message.reply_text("У тебя нет сохранённых данных.")
            return

        # Найти индекс столбца chat_id по заголовку
        headers = all_values[0]
        chat_idx = headers.index("chat_id")

        # Удаляем строки снизу вверх, где chat_id == cid_hashed
        to_delete = []
        for i in range(len(all_values) - 1, 0, -1):
            row = all_values[i]
            if len(row) > chat_idx and row[chat_idx] == cid_hashed:
                to_delete.append(i + 1)  # индексация листа с 1

        for row_index in to_delete:
            SHEET.delete_rows(row_index)

        if to_delete:
            await update.message.reply_text("Готово! Твои данные удалены.")
        else:
            await update.message.reply_text("У тебя нет сохранённых данных.")
    except Exception as e:
        log.error("Ошибка удаления: %s", e)
        await update.message.reply_text("Не удалось удалить данные. Попробуй позже.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок, завершаю. Можешь написать /start, когда будешь готов.")
    return ConversationHandler.END

# Фолбэки на не-текст в диалоге
async def non_text_in_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пожалуйста, отвечай текстом 🙏")

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CONSENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_consent),
                      MessageHandler(~filters.TEXT, non_text_in_flow)],
            BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_budget),
                     MessageHandler(~filters.TEXT, non_text_in_flow)],
            SKILLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_skills),
                     MessageHandler(~filters.TEXT, non_text_in_flow)],
            TIMEPW: [MessageHandler(filters.TEXT & ~filters.COMMAND, catch_time),
                     MessageHandler(~filters.TEXT, non_text_in_flow)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("more", cmd_more))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("terms", cmd_terms))
    app.add_handler(CommandHandler("erase", cmd_erase))
    return app

if __name__ == "__main__":
    app = build_app()

    # Автовыбор режима: если есть BASE_URL — вебхук (Render), иначе polling (локально)
    if WEBHOOK_BASE_URL:
        webhook_url = f"{WEBHOOK_BASE_URL.rstrip('/')}/{WEBHOOK_PATH}"
        log.info("🌐 Запускаю webhook: %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=webhook_url,
            drop_pending_updates=True
        )
    else:
        log.info("🤖 Бот запущен в режиме polling")
        app.run_polling()

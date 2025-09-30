import os
import json
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import openai
import gspread
from google.oauth2.service_account import Credentials

# 🔧 Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔑 Настройки
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SPREADSHEET_NAME = "AI Idea Lab Leads"

# ✅ Подключение к OpenAI
openai.api_key = OPENAI_API_KEY

# 📁 Авторизация Google
creds_json = os.getenv("GOOGLE_CREDENTIALS")
if not creds_json:
    raise Exception("❌ Переменная окружения GOOGLE_CREDENTIALS не найдена!")

creds_dict = json.loads(creds_json)

creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
)

gc = gspread.authorize(creds)

# 📊 Подключаемся к таблице
try:
    sheet = gc.open(SPREADSHEET_NAME).sheet1
    logger.info(f"✅ Подключено к Google Sheet: {SPREADSHEET_NAME}")
except Exception as e:
    logger.error(f"❌ Ошибка при подключении к таблице: {e}")
    raise

# 📩 Обработчики Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Отправь мне данные лида, и я сохраню их в таблицу.")

async def save_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    logger.info(f"📥 Получено сообщение: {text}")

    # 🧠 Пример простого сохранения: одна ячейка на сообщение
    try:
        sheet.append_row([text])
        await update.message.reply_text("✅ Лид успешно сохранён в таблицу!")
    except Exception as e:
        logger.error(f"❌ Ошибка при сохранении лида: {e}")
        await update.message.reply_text("⚠️ Ошибка при сохранении данных. Проверь настройки.")

# 🚀 Запуск бота
def main():
    if not TELEGRAM_TOKEN:
        raise Exception("❌ TELEGRAM_TOKEN не найден!")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_lead))

    logger.info("🤖 Бот запущен и готов к работе!")
    application.run_polling()

if __name__ == "__main__":
    main()
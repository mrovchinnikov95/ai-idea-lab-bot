import os
import json
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import openai
import gspread
from google.oauth2.service_account import Credentials

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# === НАСТРОЙКИ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# === GOOGLE SHEETS ===
google_creds_json = os.environ.get("GOOGLE_CREDENTIALS")
if not google_creds_json:
    raise RuntimeError("❌ Переменная GOOGLE_CREDENTIALS не найдена. Добавь её в Render.")

creds_dict = json.loads(google_creds_json)
creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)

SPREADSHEET_NAME = "AI Idea Lab Leads"
sheet = gc.open(SPREADSHEET_NAME).sheet1

# === СТЕЙТ ДЛЯ ДИАЛОГА ===
user_states = {}

QUESTIONS = [
    "💰 Сколько денег ты готов вложить на старте? (можно 0, если хочешь без вложений)",
    "🧠 Какие у тебя навыки или интересы? (через запятую)",
    "⏱ Сколько времени готов уделять в неделю?"
]

# === ХЕНДЛЕР СТАРТА ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = {"step": 0, "answers": []}
    await update.message.reply_text("Привет! Я 🤖 AI Idea Lab. Задам 3 вопроса и подберу идеи микробизнеса под твои условия.")
    await update.message.reply_text(QUESTIONS[0])

# === ОБРАБОТКА ОТВЕТОВ ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in user_states:
        await update.message.reply_text("Напиши /start чтобы начать 🚀")
        return

    state = user_states[user_id]
    state["answers"].append(update.message.text)
    state["step"] += 1

    if state["step"] < len(QUESTIONS):
        await update.message.reply_text(QUESTIONS[state["step"]])
    else:
        await update.message.reply_text("⏳ Генерирую идеи... это может занять 10–20 секунд ⌛")
        ideas = await generate_ideas(state["answers"])
        await update.message.reply_text(ideas)

        # === Сохраняем лид в Google Sheets ===
        try:
            sheet.append_row([
                str(update.effective_user.first_name),
                str(update.effective_user.username),
                state["answers"][0],
                state["answers"][1],
                state["answers"][2]
            ])
            await update.message.reply_text("✅ Твои ответы сохранены в таблице!")
        except Exception as e:
            logging.error("Ошибка при записи в Google Sheets: %s", e)
            await update.message.reply_text("⚠️ Не удалось сохранить данные в таблицу.")

        del user_states[user_id]

# === ГЕНЕРАЦИЯ ИДЕЙ ===
async def generate_ideas(answers):
    budget, skills, time = answers

    prompt = f"""
Ты эксперт по запуску микробизнесов. Дай 3 идеи под такие условия:
💰 Бюджет: {budget}
🧠 Навыки: {skills}
⏱ Время в неделю: {time}

Для каждой идеи:
- 💡 Название
- 📋 Краткое описание
- 🚀 3 шага к запуску
- 💰 Как зарабатывать
"""

    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "Ты консультант по бизнесу."},
                  {"role": "user", "content": prompt}]
    )

    return response.choices[0].message.content.strip()

# === ЗАПУСК БОТА ===
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("🤖 Бот запущен...")
    app.run_polling()
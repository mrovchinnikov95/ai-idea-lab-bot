import os
import csv
import datetime
import asyncio
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from typing import Dict
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI

# Состояния опроса
STATE_BUDGET = "budget"
STATE_SKILLS = "skills"
STATE_TIME = "time"

# Приветственное сообщение
WELCOME = (
    "Привет! Я 🤖 AI Idea Lab. Задам 3 вопроса и подберу идеи микробизнеса под твои условия.\n\n"
    "💰 Сколько денег ты готов вложить на старте? (можно 0, если хочешь без вложений)\n"
    "Примеры: 0, 1000, 5000"
)

# CSV для лидов
def ensure_csv():
    if not os.path.exists("leads.csv"):
        with open("leads.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "user_id", "username", "budget", "skills", "time"])


def save_lead(user_id: int, username: str, data: Dict[str, str]):
    ensure_csv()
    with open("leads.csv", "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                datetime.datetime.utcnow().isoformat(),
                user_id,
                username or "",
                data.get("budget", ""),
                data.get("skills", ""),
                data.get("time", ""),
            ]
        )


# GPT генерация 3 идей
async def gen_ideas_gpt(budget: int, skills: str, time_week: int) -> str:
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    response = await asyncio.to_thread(
        lambda: client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Ты — эксперт по запуску микробизнесов."},
                {
                    "role": "user",
                    "content": f"""
💰 Бюджет: {budget}₽
🧠 Навыки и интересы: {skills}
⏱ Доступное время: {time_week} часов в неделю

Сгенерируй 3 разные идеи микробизнеса, которые можно запустить за 7–14 дней.
Формат каждой идеи:
💡 Название  
📋 Что это и зачем нужно (2–3 предложения)  
🚀 3 шага запуска  
💰 Как монетизировать
                    """,
                },
            ],
            temperature=0.9,
            max_tokens=1000,
        )
    )

    return response.choices[0].message.content.strip()


# Команды
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = STATE_BUDGET
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я спрошу 💰 бюджет, 🧠 навыки и ⏱ доступное время, а затем предложу 3 идеи.\nКоманды: /start, /pro, /help"
    )


async def pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📩 PRO-отчёт: пришли e-mail одним сообщением, я занесу тебя в список и пришлю, когда будет готово."
    )


# Основная логика диалога
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    if state == STATE_BUDGET:
        try:
            budget = int(text.replace("$", " ").replace("₽", " ").split()[0])
        except Exception:
            return await update.message.reply_text("Укажи число. Примеры: 0, 1000, 5000")
        context.user_data["budget"] = str(budget)
        context.user_data["state"] = STATE_SKILLS
        return await update.message.reply_text(
            "🧠 Какие у тебя навыки или интересы? (через запятую)", parse_mode="Markdown"
        )

    if state == STATE_SKILLS:
        context.user_data["skills"] = text
        context.user_data["state"] = STATE_TIME
        kb = ReplyKeyboardMarkup(
            [["3–5 часов/нед", "5–10 часов/нед", ">10 часов/нед"]],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
        return await update.message.reply_text(
            "⏱ Сколько времени готов уделять в неделю?", reply_markup=kb
        )

    if state == STATE_TIME:
        context.user_data["time"] = text
        context.user_data["state"] = None

        # Сохраняем лид
        save_lead(
            update.effective_user.id,
            update.effective_user.username,
            context.user_data,
        )

        # Уведомляем о генерации
        await update.message.reply_text(
            "⏳ Генерирую идеи... это может занять 10–20 секунд ⌛"
        )

        # Генерация идей через GPT
        budget = int(context.user_data.get("budget", "0"))
        skills = context.user_data.get("skills", "")
        time_week = 5
        ideas = await gen_ideas_gpt(budget, skills, time_week)

        # Отправляем результат
        await update.message.reply_text(
            "✅ Готово! Вот 3 идеи под твои условия:\n\n" + ideas,
            reply_markup=ReplyKeyboardRemove(),
        )
        return await update.message.reply_text(
            "📩 Хочешь получить PRO-отчёт с подробным планом, инструментами и промптами? Напиши свой e-mail одним сообщением или используй /pro"
        )

    # Если прислали e-mail
    if "@" in text and "." in text:
        ensure_csv()
        with open("pro_requests.csv", "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    datetime.datetime.utcnow().isoformat(),
                    update.effective_user.id,
                    update.effective_user.username,
                    text,
                ]
            )
        return await update.message.reply_text(
            "✅ Супер! Ты в списке ожидания PRO-версии 📬."
        )

    return await update.message.reply_text("Нажми /start, чтобы запустить генератор, или /help")


# 🚀 Запуск бота
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("❌ TELEGRAM_BOT_TOKEN не найден. Добавь его в Secrets.")

    app = (
        ApplicationBuilder()
        .token(token)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pro", pro_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    # 💡 Фейковый веб-сервер для Render
    def run_server():
        port = int(os.environ.get("PORT", 8000))
        server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
        print(f"🌐 Dummy server running on port {port}")
        server.serve_forever()

    threading.Thread(target=run_server, daemon=True).start()

    main()
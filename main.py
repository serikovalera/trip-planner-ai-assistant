import logging
import json
from datetime import datetime, timedelta

from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from mcp import Agentpip search mcp
from mcp.llms import OllamaLLM
from mcp.tools import google_maps, openweathermap

# === 🔐 API-ключи ===
GOOGLE_MAPS_API_KEY = "your_google_maps_api_key"
OPENWEATHER_API_KEY = "your_openweather_api_key"
TELEGRAM_TOKEN = "your_telegram_token"

# === MCP Агент с локальными инструментами ===
llm = OllamaLLM(model="mistral")  # Или "mistral:instruct" (опечатка в оригинале)
agent = Agent("travel-agent", llm=llm)
agent.add_tool(google_maps.Tool(api_key=GOOGLE_MAPS_API_KEY))
agent.add_tool(openweathermap.Tool(api_key=OPENWEATHER_API_KEY))

# === FastAPI (опционально) ===
app = FastAPI()

# === Логирование ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(name)  # Исправлено: было 'name', нужно 'name'

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✈️ Привет! Я помогу спланировать путешествие.\n\n"
        "Напиши мне:\n"
        "📍 Город, 📅 даты (например, 10–13 июля), 💰 бюджет в день (например, 40 евро)"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text

    try:
        await update.message.reply_text("📥 Обрабатываю запрос...")

        # === 1. Извлекаем параметры из запроса пользователя ===
        extract_prompt = f"""
        Извлеки из этого текста:
        - Город (city)
        - Дату начала (start_date) в формате YYYY-MM-DD
        - Дату окончания (end_date)
        - Бюджет в евро (budget)

        Верни JSON:
        {{
            "city": "...",
            "start_date": "...",
            "end_date": "...",
            "budget": ...
        }}

        Текст:
        \"\"\"{user_input}\"\"\"
        """
        extract_response = await agent.run(extract_prompt)  # Добавлен await
        trip_info = json.loads(extract_response)

        city = trip_info["city"]
        start_date = trip_info["start_date"]
        end_date = trip_info["end_date"]
        budget = trip_info["budget"]

        # === 2. Генерируем список дат поездки ===
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days + 1)]

        await update.message.reply_text(f"📍 Город: {city}\n📅 Даты: {start_date} – {end_date}\n💰 Бюджет: {budget}€/день")

        # === 3. Получаем достопримечательности ===
        places_response = await agent.run(f"Покажи 5 самых интересных достопримечательностей в {city}")  # Добавлен await
        # === 4. Получаем прогноз погоды ===
        weather_query = f"Прогноз погоды в {city} на даты: {', '.join(dates)}"
        weather_response = await agent.run(weather_query)  # Добавлен await

        # === 5. Генерируем маршрут ===
        itinerary_prompt = f"""
        Составь подробный план поездки в {city} с {start_date} по {end_date}, бюджет {budget}€/день.

        Вот достопримечательности:
        {places_response}

        Вот прогноз погоды:
        {weather_response}

        Учитывай погоду:
        - дождь — крытые места
        - солнце — прогулки
        Добавь названия мест, краткие описания и примерные расходы.
        """

        itinerary = await agent.run(itinerary_prompt)  # Добавлен await

        await update.message.reply_text(f"🗺️ Ваш план:\n\n{itinerary}")

    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON: {e}")
        await update.message.reply_text("⚠️ Не могу распознать параметры поездки. Укажите в формате: Город, даты (10-13 июля), бюджет (40 евро)")
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Произошла ошибка. Проверьте формат и попробуйте снова.")

def main():
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Запуск бота
    bot_app.run_polling()

if name == "main":  # Исправлено: было 'name', нужно 'name'
    main()
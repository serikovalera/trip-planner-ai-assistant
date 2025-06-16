import re
import json
import random
import requests
import asyncio
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
from collections import defaultdict

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from langchain_community.llms import Ollama
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# Константы
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}
MONTH_NAMES = list(MONTHS.keys())
CATEGORY_PRICES = {
    "cafe": 700, "restaurant": 2000, "museum": 500, 
    "park": 0, "art_gallery": 500, "hotel": 3000
}
CATEGORY_TRANSLATIONS = {
    "cafe": "Кафе", "restaurant": "Ресторан", "museum": "Музей",
    "park": "Парк", "art_gallery": "Галерея", "hotel": "Отель"
}
TELEGRAM_TOKEN = "токен"
SCOPES = ['https://www.googleapis.com/auth/calendar.events']
CLIENT_SECRET_FILE = 'google_credentials.json'

# Глобальные переменные
calendar_context = defaultdict(list)
user_credentials = {}

# Инициализация LLM
llm = Ollama(
    model='mistral:7b-instruct',
    temperature=0.5,
    num_ctx=1024,
    top_k=40,
    repeat_penalty=1.2,
    num_thread=4
)

# ========= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========

def ask_mistral(prompt):
    try:
        return llm.invoke(prompt).strip()
    except Exception as e:
        print(f"LLM error: {e}")
        return ""

def parse_dates(text):
    text = text.lower().replace("–", "-").replace("—", "-").replace("по", "-").replace("с ", "")
    pattern = re.compile(r"(\d{1,2})\s*([а-я]+)?\s*-\s*(\d{1,2})\s*([а-я]+)?")
    match = pattern.search(text)
    if match:
        day1, month1, day2, month2 = match.groups()
        month1 = month1 or month2
        month2 = month2 or month1
        m1 = MONTHS.get(month1)
        m2 = MONTHS.get(month2)
        year = datetime.now().year
        try:
            start = datetime(year, m1, int(day1))
            end = datetime(year, m2, int(day2))
            if end < start:
                end = datetime(year + 1, m2, int(day2))
            return start, end
        except:
            return None, None
    return None, None

def parse_budget(text):
    match = re.search(r"(\d{3,})", text.replace(" ", "").replace(",", ""))
    return int(match.group(1)) if match else None

def parse_flexible_input_with_llm(text):
    prompt = f"""Пользователь написал: '{text}'. Выдели город, даты и бюджет для поездки в формате JSON.
Пример ответа: {{"city": "Москва", "dates": "15-16 июня", "budget": "5000 рублей"}}"""
    try:
        response = ask_mistral(prompt)
        json_str = re.search(r'\{.*\}', response, re.DOTALL)
        if json_str:
            data = json.loads(json_str.group(0))
            city = data.get('city', '').strip()
            budget = parse_budget(str(data.get('budget', '')))
            start, end = parse_dates(data.get('dates', ''))
            if city and start and end and budget:
                return city, start, end, budget
    except Exception as e:
        print(f"LLM parsing error: {e}")
    return None, None, None, None

def parse_user_input(text):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 3:
        city = parts[0]
        start, end = parse_dates(parts[1])
        budget = parse_budget(parts[2])
        if all([city, start, end, budget]):
            return city, start, end, budget
    return parse_flexible_input_with_llm(text)

def get_coordinates(city):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": city, "format": "json", "limit": 1}
    resp = requests.get(url, params=params, headers={"User-Agent": "trip-bot"})
    data = resp.json()
    if data:
        return float(data[0]['lat']), float(data[0]['lon'])
    return None, None

def get_weather_forecast(city, start_dt, end_dt):
    lat, lon = get_coordinates(city)
    if lat is None:
        return {}
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "auto",
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d")
    }
    resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params)
    if resp.status_code != 200:
        return {}
    data = resp.json()
    weather = {}
    for i, date in enumerate(data.get("daily", {}).get("time", [])):
        temp_max = data["daily"]["temperature_2m_max"][i]
        temp_min = data["daily"]["temperature_2m_min"][i]
        rain = data["daily"]["precipitation_sum"][i]
        avg_temp = (temp_max + temp_min) / 2
        weather[date] = {
            "temp": round(avg_temp),
            "rain": rain,
            "main": "rain" if rain > 0 else "clear"
        }
    return weather

def get_attractions(city, categories):
    category_tags = {
        "museum": "tourism=museum",
        "park": "leisure=park",
        "restaurant": "amenity=restaurant",
        "cafe": "amenity=cafe",
        "art_gallery": "tourism=art_gallery",
        "hotel": "tourism=hotel"
    }
    places = []
    for cat in categories:
        tag = category_tags.get(cat)
        if not tag:
            continue
        query = f"""
        [out:json];
        area["name"="{city}"]->.searchArea;
        (
        node[{tag}](area.searchArea);
        way[{tag}](area.searchArea);
        relation[{tag}](area.searchArea);
        );
        out center 50;
        """
        resp = requests.post("https://overpass-api.de/api/interpreter", data={'data': query})
        try:
            for el in resp.json().get('elements', []):
                name = el.get('tags', {}).get('name')
                lat = el.get('lat') or el.get('center', {}).get('lat')
                lon = el.get('lon') or el.get('center', {}).get('lon')
                if name and lat and lon:
                    places.append({
                        "name": name, "type": cat,
                        "lat": lat, "lon": lon,
                        "price": CATEGORY_PRICES.get(cat, 1000 if cat != "hotel" else 4000)
                    })
        except:
            continue
    unique = {p['name']: p for p in places}
    return list(unique.values())

def haversine(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 2 * asin(sqrt(a)) * 6371

def sort_by_proximity(places):
    if not places:
        return []
    places = places[:]
    sorted_places = [places.pop(0)]
    while places:
        last = sorted_places[-1]
        places.sort(key=lambda x: haversine(last['lon'], last['lat'], x['lon'], x['lat']))
        sorted_places.append(places.pop(0))
    return sorted_places

def generate_daily_plan(date_str, weather, places, daily_budget, used_places):
    date = datetime.strptime(date_str, "%Y-%m-%d")
    temp = weather.get("temp", "?")
    main = weather.get("main", "clear")
    desc = {"rain": "дождь 🌧️", "clear": "ясно ☀️", "clouds": "облачно ⛅", "snow": "снег ❄️"}

    plan = f"{date.strftime('%d ')}{MONTH_NAMES[date.month - 1]}:\n"
    plan += f"  Погода: {temp}°C, {desc.get(main, 'ясно ☀️')}\n"

    indoor_types = ("museum", "art_gallery", "cafe", "restaurant")
    outdoor_types = ("park",)
    suitable_types = indoor_types if main == "rain" else indoor_types + outdoor_types
    day_places = [p for p in places if p['type'] in suitable_types and p['name'] not in used_places]
    random.shuffle(day_places)
    day_places = sort_by_proximity(day_places)

    segments = {
        "🍳 Завтрак": "cafe",
        "🏛️ Утреннее занятие": ["museum", "art_gallery"],
        "🍽️ Обед": "restaurant",
        "🚶 После обеда": ["park", "cafe", "museum", "art_gallery"],
        "🌙 Вечером": ["cafe", "restaurant", "art_gallery", "park"]
    }
    used_budget = 0
    day_plan = []

    for i, (part, types) in enumerate(segments.items()):
        if isinstance(types, str):
            types = [types]
        for p in day_places:
            if p['type'] in types and p['name'] not in used_places:
                cost = p['price']
                if used_budget + cost <= daily_budget:
                    event_desc = f"{part}: {p['name']} ({CATEGORY_TRANSLATIONS[p['type']]}, ~{cost}₽)"
                    day_plan.append(f"  {event_desc}")
                    used_places.add(p['name'])
                    used_budget += cost
                    calendar_context[date_str].append(event_desc)
                    break

    if not day_plan:
        plan += "  Нет подходящих мест на сегодня.\n"
    else:
        plan += "\n".join(day_plan) + "\n"
    return plan

def get_google_calendar_service(user_id):
    creds = None
    if user_id in user_credentials:
        creds = Credentials.from_authorized_user_info(user_credentials[user_id], SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        user_credentials[user_id] = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes
        }
    
    return build('calendar', 'v3', credentials=creds)

async def add_event_to_google_calendar(user_id, title, start_dt, duration_hours=1):
    try:
        service = get_google_calendar_service(user_id)
        end_dt = start_dt + timedelta(hours=duration_hours)
        event = {
            'summary': title,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Europe/Moscow'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Europe/Moscow'},
        }
        service.events().insert(calendarId='primary', body=event).execute()
        return True
    except Exception as e:
        print(f"Google Calendar error: {e}")
        return False

async def generate_trip_plan(city, start, end, budget):
    days = (end - start).days + 1
    daily_budget = budget // days
    
    weather, places = await asyncio.gather(
        asyncio.to_thread(get_weather_forecast, city, start, end),
        asyncio.to_thread(get_attractions, city, list(CATEGORY_PRICES.keys()) + ["hotel"])
    )
    
    hotels = [p for p in places if p['type'] == "hotel" and p['price'] * days <= budget]

    reply = f"🛫 План путешествия в {city} с {start.strftime('%d ')}{MONTH_NAMES[start.month-1]} по {end.strftime('%d ')}{MONTH_NAMES[end.month-1]}\n"
    reply += f"💰 Бюджет: {budget}₽ (~{daily_budget}₽ в день)\n\n"
    
    if hotels:
        reply += "🏨 Предложенные отели:\n" + "\n".join([f"  • {h['name']} (~{h['price']}₽/день)" for h in hotels[:3]]) + "\n\n"

    used_places = set()
    for i in range(days):
        date = start + timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        w = weather.get(date_str, {"temp": "?", "main": "clear"})
        reply += generate_daily_plan(date_str, w, places, daily_budget, used_places) + "\n"

    enriched = await asyncio.to_thread(
        ask_mistral,
        f"Пользователь поедет в {city} с {start.strftime('%d ')}{MONTH_NAMES[start.month-1]} по {end.strftime('%d ')}{MONTH_NAMES[end.month-1]} с бюджетом {budget}₽. Придумай 3 популярных идеи для отдыха, связанных с местной культурой или природой. Формат: 1)... 2)... 3)... Только идеи. На РУССКОМ."
    )
    reply += "🌟 Дополнительные рекомендации:\n" + enriched
    
    return reply

# ========= TELEGRAM HANDLERS =========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Добро пожаловать! Нажмите '🚀 Начать' для планирования поездки.",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🚀 Начать")]], resize_keyboard=True)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text
    
    if text == "🚀 Начать":
        await update.message.reply_text(
            "Введите параметры поездки в формате: Город, даты (например, 15-16 июня), бюджет",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📅 Сохранить в календарь")]], resize_keyboard=True)
        )
        return

    if text == "📅 Сохранить в календарь":
        if not calendar_context:
            await update.message.reply_text("Сначала создайте план поездки, введя параметры!")
            return
            
        try:
            auth_msg = await update.message.reply_text("🔐 Авторизуемся в Google Calendar...")
            
            # Сначала проверяем авторизацию
            service = get_google_calendar_service(user_id)
            
            await auth_msg.edit_text("📅 Добавляем события в календарь...")
            
            for date_str, events in calendar_context.items():
                date = datetime.strptime(date_str, "%Y-%m-%d")
                for i, event_desc in enumerate(events):
                    event_time = date + timedelta(hours=9 + i * 2)
                    success = await add_event_to_google_calendar(user_id, event_desc, event_time)
                    if not success:
                        raise Exception("Ошибка добавления события")
            
            await auth_msg.edit_text("✅ Поездка успешно добавлена в ваш Google Календарь!")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка: {str(e)}")
        return

    planning_msg = await update.message.reply_text("⏳ Планирую ваш отдых...")
    
    try:
        # Очищаем предыдущий контекст перед созданием нового плана
        calendar_context.clear()
        
        city, start, end, budget = parse_user_input(text)
        if not all([city, start, end, budget]):
            await planning_msg.edit_text("Не удалось распознать параметры поездки. Пример:\nМосква, 15-16 июня, 5000")
            return

        reply = await generate_trip_plan(city, start, end, budget)
        await planning_msg.edit_text(reply)
        
    except Exception as e:
        await planning_msg.edit_text(f"⚠️ Произошла ошибка: {str(e)}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()

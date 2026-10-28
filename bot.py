import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from google import genai

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
CHANNEL = os.environ["CHANNEL"]
OWNER = os.environ["OWNER_CHAT_ID"]
PUBLISH = os.environ.get("PUBLISH", "") == "true"

MODEL = "gemini-3.8-flash"

CODES = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман", 51: "морось", 53: "морось", 55: "морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег",
    80: "ливни", 81: "ливни", 82: "сильные ливни", 95: "гроза",
}

def http_json(url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def http_download(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def weather():
    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=54.366&longitude=77.298"
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Asia%2FNovosibirsk&forecast_days=2&wind_speed_unit=ms"
    )
    d = http_json(url)
    c, dl = d["current"], d["daily"]

    def w(code):
        return CODES.get(code, "переменная погода")

    return {
        "current": f"Сейчас {round(c['temperature_2m'])}°, ощущается как {round(c['apparent_temperature'])}°, {w(c['weather_code'])}, ветер {c['wind_speed_10m']:.0f} м/с.",
        "today": f"Сегодня: от {round(dl['temperature_2m_min'][0])} до {round(dl['temperature_2m_max'][0])}°, {w(dl['weather_code'][0])}, осадки {dl['precipitation_probability_max'][0] or 0}%.",
        "tomorrow": f"Завтра: от {round(dl['temperature_2m_min'][1])} до {round(dl['temperature_2m_max'][1])}°, {w(dl['weather_code'][1])}, осадки {dl['precipitation_probability_max'][1] or 0}%."
    }

def send_text(chat, text):
    http_json(f"https://api.telegram.org/bot{TOKEN}/sendMessage", {"chat_id": chat, "text": text})

def send_photo(chat, photo_url, caption):
    # Отправляем картинку с текстом (caption)
    http_json(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", {
        "chat_id": chat, 
        "photo": photo_url, 
        "caption": caption[:1024], # Telegram ограничивает подпись до 1024 символов
        "parse_mode": "HTML"
    })

def main():
    client = genai.Client(api_key=GEMINI_KEY)
    now = datetime.now(timezone(timedelta(hours=7)))
    today = now.strftime("%d.%m.%Y")
    
    w_data = weather()

    prompt = (
        "Ты — бот для Telegram-канала. Твоя задача — собрать пост СТРОГО по шаблону. "
        "Нарушать шаблон, менять эмодзи или склеивать абзацы ЗАПРЕЩЕНО.\n\n"
        "Шаблон (скопируй его и просто вставь нужные данные):\n"
        "☀️ Всем привет!\n\n"
        "🌡Сегодня на улице: {current_weather}\n\n"
        "📅 Прогноз погоды\n"
        "• {today_weather}\n"
        "• {tomorrow_weather}\n\n"
        "📰 Из новостей: {news}\n\n"
        "Всем хорошего дня и побольше позитива! 👋\n\n"
        f"Данные для подстановки:\n"
        f"- current_weather: {w_data['current']}\n"
        f"- today_weather: {w_data['today']}\n"
        f"- tomorrow_weather: {w_data['tomorrow']}\n"
        "- news: Твоя задача найти САМУЮ СВЕЖУЮ новость (за сегодня или вчера) про Купино, "
        "Купинский район, Новосибирск или Новосибирскую область. Ищи конкретные события. "
        "Напиши 1-2 предложения с сутью новости. Если новостей нет — напиши ровно: "
        "«за последние дни ничего интересного не найдено».\n\n"
        "ВАЖНО: В самом конце твоего ответа, ПОСЛЕ всего текста, добавь строку с прямой ссылкой "
        "на главную картинку из этой новости в формате: IMAGE_URL: https://... "
        "Если новости нет или картинки нет, не пиши эту строку вообще."
    )

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config={'tools': [{'google_search': {}}]}
        )
        full_text = response.text
    except Exception as e:
        print("С поиском не вышло:", type(e).__name__)
        full_text = (
            f"☀️ Всем привет!\n\n"
            f"🌡Сегодня на улице: {w_data['current']}\n\n"
            f"📅 Прогноз погоды\n"
            f"• {w_data['today']}\n"
            f"• {w_data['tomorrow']}\n\n"
            f"📰 Из новостей: за последние дни ничего интересного не найдено\n\n"
            f"Всем хорошего дня и побольше позитива! 👋"
        )

    # Вытаскиваем ссылку на картинку, если ИИ её нашел
    image_url = None
    lines = full_text.split("\n")
    text_lines = []
    
    for line in lines:
        if line.strip().startswith("IMAGE_URL:"):
            image_url = line.replace("IMAGE_URL:", "").strip()
        else:
            text_lines.append(line)
            
    clean_text = "\n".join(text_lines).strip()

    if not clean_text:
        sys.exit("Пустой ответ от Gemini")

    # Отправка
    if PUBLISH:
        if image_url:
            try:
                # Скачиваем картинку в память, чтобы Telegram её точно принял
                img_data = http_download(image_url)
                # Отправляем как файл
                import io
                files = {'photo': ('image.jpg', io.BytesIO(img_data))}
                req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendPhoto")
                # Тут используется multipart/form-data, что сложнее, поэтому
                # давай попробуем просто отправить по ссылке. Если не выйдет, 
                # отправим текстом.
                send_photo(CHANNEL, image_url, clean_text)
            except Exception as e:
                print("Не вышло с картинкой, шлем текст:", e)
                send_text(CHANNEL, clean_text)
        else:
            send_text(CHANNEL, clean_text)
    else:
        send_text(OWNER, "Черновик (в канал не ушёл):\n\n" + clean_text)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            send_text(OWNER, f"Автопост не сработал: {type(e).__name__}")
        except Exception:
            pass
        raise

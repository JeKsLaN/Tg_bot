"""Утренний пост для Telegram-канала: погода в Купино + новость.
Текст пишет Gemini, публикует бот. Запускается из GitHub Actions."""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from google import genai

# Настройки из GitHub Secrets
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
CHANNEL = os.environ["CHANNEL"]
OWNER = os.environ["OWNER_CHAT_ID"]
PUBLISH = os.environ.get("PUBLISH", "") == "true"

# Используем актуальную модель
MODEL = "gemini-3.8-flash"

CODES = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман", 51: "морось", 53: "морось", 55: "морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег",
    80: "ливни", 81: "ливни", 82: "сильные ливни", 95: "гроза",
}

STYLE = (
    "Пиши как обычное сообщение друзьям: коротко, просто, без пафоса. "
    "Обязательно используй подходящие эмодзи. Выбирай их из этих вариантов, чтобы посты не повторялись: "
    "Для приветствия: 👋, ☀️, 🌤, 🔥, ✨. "
    "Для погоды: если ясно — ☀️, 🌞, 😎; если облачно — ☁️, 🌥, ⛅️; если дождь — 🌧, ☔️, 💧; "
    "если снег — ❄️, 🌨, ⛄️; если гроза — ⚡️, 🌩, ⛈. "
    "Для пожелания в конце используй случайный вариант: «Хорошего дня!», «Отличного настроения!», "
    "«Продуктивного дня!», «Хорошей [день недели]!»."
)

def http_json(url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

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

    def day(i):
        return (
            f"от {round(dl['temperature_2m_min'][i])}° до {round(dl['temperature_2m_max'][i])}°, "
            f"{w(dl['weather_code'][i])}, вероятность осадков {dl['precipitation_probability_max'][i] or 0}%"
        )

    return (
        f"Сейчас {round(c['temperature_2m'])}°, ощущается как {round(c['apparent_temperature'])}°, "
        f"{w(c['weather_code'])}, ветер {c['wind_speed_10m']:.0f} м/с. "
        f"Сегодня {day(0)}. Завтра {day(1)}."
    )

def send(chat, text):
    http_json(f"https://api.telegram.org/bot{TOKEN}/sendMessage", {"chat_id": chat, "text": text})

def main():
    client = genai.Client(api_key=GEMINI_KEY)
    now = datetime.now(timezone(timedelta(hours=7)))
    today = now.strftime("%d.%m.%Y")
    weekday = now.strftime("%A")
    
    weekdays_ru = {
        "Monday": "понедельника", "Tuesday": "вторника", "Wednesday": "среды",
        "Thursday": "четверга", "Friday": "пятницы", "Saturday": "субботы", "Sunday": "воскресенья"
    }
    day_ru = weekdays_ru.get(weekday, "дня")

    prompt = (
        "Напиши короткий пост для Telegram-канала про город Купино "
        "(Купинский район, Новосибирская область, Россия). "
        f"Сегодня {today}, {day_ru}.\n"
        f"Погода (используй только эти данные, ничего не выдумывай): {weather()}\n"
        f"{STYLE}\n"
        "Структура: «Всем привет.» + одно-два предложения про погоду + ОБЯЗАТЕЛЬНО найди настоящую "
        "свежую новость (не старше 3 дней). Сначала ищи именно про Купино или Купинский район. "
        "Если там новостей нет — ищи любую интересную новость по Новосибирской области или Новосибирску "
        "(НЕ про Купино в Харьковской области). Напиши одно предложение в стиле "
        "«Из новостей региона: ...». Если совсем никаких новостей нет — напиши ровно фразу: "
        "«Новостей за последние дни не нашлось» и больше ничего не добавляй. "
        "Обязательно раздели текст на три абзаца (используй перенос строки дважды между ними): "
        "1) Приветствие и погода сейчас. 2) Погода на сегодня и завтра. 3) Новость (если есть) и пожелание хорошего дня. "
        "Ничего не выдумывай. Ответь только текстом поста, без пояснений, ссылок и кавычек. Не больше 400 символов."
    )

    try:
        # Пытаемся сгенерировать с новостями
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config={'tools': [{'google_search': {}}]}
        )
        text = response.text
    except Exception as e:
        print("С поиском не вышло:", type(e).__name__)
        # Если не получилось с новостями, пишем только погоду
        text = f"Всем привет! ☀️\n\n{weather()}\n\n📰 Новостей за последние дни не нашлось. Хорошего {day_ru}! 👋"

    if not text:
        sys.exit("Пустой ответ от Gemini")

    if PUBLISH:
        send(CHANNEL, text)
    else:
        send(OWNER, "Черновик (в канал не ушёл):\n\n" + text)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            send(OWNER, f"Автопост не сработал: {type(e).__name__}")
        except Exception:
            pass
        raise

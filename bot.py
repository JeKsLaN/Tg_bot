"""Утренний пост для Telegram-канала: погода в Купино + новость.
Текст пишет Gemini, публикует бот. Запускается из GitHub Actions."""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
CHANNEL = os.environ["CHANNEL"]
OWNER = os.environ["OWNER_CHAT_ID"]
PUBLISH = os.environ.get("PUBLISH", "") == "true"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest)

CODES = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман", 51: "морось", 53: "морось", 55: "морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег",
    80: "ливни", 81: "ливни", 82: "сильные ливни", 95: "гроза",
}

STYLE = (
    "Пиши как обычное сообщение друзьям: коротко, просто, без пафоса и без смайлов. "
    "Пример стиля: «Всем привет. В Купино сегодня +4 градуса, из новостей я тут узнал что ...»"
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


def gemini(prompt, search):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if search:
        body["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    r = http_json(url, body, {"x-goog-api-key": GEMINI_KEY})
    parts = r["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def send(chat, text):
    http_json(f"https://api.telegram.org/bot{TOKEN}/sendMessage", {"chat_id": chat, "text": text})


def main():
    today = datetime.now(timezone(timedelta(hours=7))).strftime("%d.%m.%Y")
    prompt = (
        "Напиши короткий пост для Telegram-канала про город Купино "
        "(Купинский район, Новосибирская область, Россия). "
        f"Сегодня {today}.\n"
        f"Погода (используй только эти данные, ничего не выдумывай): {weather()}\n"
        f"{STYLE}\n"
        "Структура: «Всем привет.» + одно-два предложения про погоду + если найдёшь настоящую "
        "свежую новость (не старше 3 дней) именно про Купино или Купинский район "
        "Новосибирской области (НЕ про Купино в Харьковской области) — одно предложение "
        "«Из новостей я тут узнал, что ...». Если надёжной свежей новости нет — не упоминай "
        "новости вообще. Ничего не выдумывай. Ответь только текстом поста, без пояснений, "
        "ссылок и кавычек. Не больше 400 символов."
    )
    try:
        text = gemini(prompt, True)
    except Exception as e:
        print("С поиском не вышло:", type(e).__name__)
        text = gemini(prompt + "\nНовости не используй, только погода.", False)
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

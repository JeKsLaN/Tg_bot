
import os
import re
import json
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
CHANNEL = os.environ["CHANNEL"]
OWNER = os.environ["OWNER_CHAT_ID"]
PUBLISH = os.environ.get("PUBLISH", "").strip().lower() == "true"

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
TZ = timezone(timedelta(hours=7))
MAX_NEWS = 12

CODES = {
    0: "ясно", 1: "в основном ясно",
    2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман",
    51: "морось", 53: "морось", 55: "сильная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег",
    80: "ливни", 81: "ливни", 82: "сильные ливни",
    95: "гроза",
}


def http_request(url, data=None, headers=None, timeout=25):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LocalNewsBot/2.0)",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def http_json(url, data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    raw = http_request(
        url, body,
        {"Content-Type": "application/json"},
        timeout=25,
    )
    result = json.loads(raw.decode("utf-8"))
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(
            "Telegram API: " + str(result.get("description", "ошибка"))
        )
    return result


def send_text(chat, text):
    # Одно сообщение Telegram — не более 4096 символов.
    if len(text) > 4000:
        text = text[:3980].rsplit("\n", 1)[0] + "\n…"

    return http_json(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        {
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": True,
        },
    )


def weather():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=54.366&longitude=77.298"
        "&current=temperature_2m,apparent_temperature,"
        "weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max"
        "&timezone=Asia%2FNovosibirsk&forecast_days=2"
        "&wind_speed_unit=ms"
    )
    data = http_json(url)
    c, d = data["current"], data["daily"]

    def describe(code):
        return CODES.get(code, "переменная погода")

    def chance(value):
        return 0 if value is None else value

    return (
        "☀️ Всем привет!\n\n"
        f"🌡 Сейчас: {round(c['temperature_2m'])}°, "
        f"ощущается как {round(c['apparent_temperature'])}°, "
        f"{describe(c['weather_code'])}, "
        f"ветер {c['wind_speed_10m']:.0f} м/с.\n\n"
        "📅 Прогноз погоды\n"
        f"• Сегодня: от {round(d['temperature_2m_min'][0])} "
        f"до {round(d['temperature_2m_max'][0])}°, "
        f"{describe(d['weather_code'][0])}, "
        f"осадки {chance(d['precipitation_probability_max'][0])}%.\n"
        f"• Завтра: от {round(d['temperature_2m_min'][1])} "
        f"до {round(d['temperature_2m_max'][1])}°, "
        f"{describe(d['weather_code'][1])}, "
        f"осадки {chance(d['precipitation_probability_max'][1])}%."
    )


def clean_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def get_rss_news(query, area):
    params = urllib.parse.urlencode({
        "q": query + " when:7d",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
    })
    url = "https://news.google.com/rss/search?" + params

    try:
        raw = http_request(url, timeout=20)
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"RSS недоступен ({area}): {type(exc).__name__}")
        return []

    found = []
    now = datetime.now(TZ)
    cutoff = now - timedelta(days=7)

    for entry in root.findall(".//item"):
        title = clean_html(entry.findtext("title", ""))
        link = (entry.findtext("link", "") or "").strip()
        description = clean_html(entry.findtext("description", ""))
        date_text = entry.findtext("pubDate", "")
        source_el = entry.find("source")
        source = (
            clean_html(source_el.text or "")
            if source_el is not None else ""
        )

        if not title or not link or not date_text:
            continue

        try:
            published = parsedate_to_datetime(date_text)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(TZ)
        except (ValueError, TypeError, OverflowError):
            continue

        if published < cutoff or published > now + timedelta(hours=1):
            continue

        found.append({
            "area": area,
            "title": title,
            "description": description[:900],
            "source": source,
            "date": published.strftime("%d.%m.%Y"),
            "timestamp": published.timestamp(),
            "url": link,
        })

    return found


def search_news():
    # Отдельные поиски для каждой территории.
    searches = [
        ("Купино Купинский район", "Купино и Купинский район"),
        ("Новосибирск ДТП происшествия", "Новосибирск"),
        (
            "Новосибирск Октябрьский район улица Выборная",
            "Новосибирск",
        ),
        (
            "Новосибирск благоустройство городские события",
            "Новосибирск",
        ),
        (
            "Новосибирская область новости происшествия",
            "Новосибирская область",
        ),
    ]

    collected = []
    for query, area in searches:
        print("Ищу RSS:", query)
        collected.extend(get_rss_news(query, area))

    # Убираем дубликаты по заголовкам.
    unique = {}
    for item in sorted(
        collected, key=lambda x: x["timestamp"], reverse=True
    ):
        key = re.sub(r"\W+", "", item["title"].lower())
        if key and key not in unique:
            unique[key] = item

    results = list(unique.values())[:MAX_NEWS]
    print("Найдено свежих публикаций:", len(results))
    return results


def fallback_news_post(news):
    # Работает и тогда, когда Gemini временно недоступен.
    sections = {
        "Купино и Купинский район": [],
        "Новосибирск": [],
        "Новосибирская область": [],
    }

    for item in news:
        area = item["area"]
        headline = f"• {item['title']} ({item['date']})"
        if item["description"]:
            headline += "\n" + item["description"]

        sections.setdefault(area, []).append(headline)

    output = ["📰 Новости региона"]
    for area, items in sections.items():
        if items:
            output.append("\n" + area + "\n")
            output.extend(items)

    return "\n".join(output)


def make_news_post(client, news):
    if not news:
        return (
            "📰 Новости региона\n\n"
            "За последние 7 дней не удалось найти "
            "подходящие публикации."
        )

    source_text = []
    for i, item in enumerate(news, 1):
        source_text.append(
            f"{i}. Территория: {item['area']}\n"
            f"Дата: {item['date']}\n"
            f"Заголовок: {item['title']}\n"
            f"Издание: {item['source']}\n"
            f"Описание из RSS: {item['description']}\n"
        )

    prompt = f"""
Ты редактор местного новостного Telegram-канала.
Составь ОДИН общий пост на русском языке по материалам ниже.

Разделы строго в таком порядке:
1. Купино и Купинский район
2. Новосибирск
3. Новосибирская область

Включай в каждый раздел только новости, относящиеся к нему.
Не повторяй одинаковые события. Если есть новости об Октябрьском
районе или улице Выборной, оставь их в разделе Новосибирск.

Каждую новость изложи понятным языком: что произошло,
где и когда, какие последствия известны.
По возможности дай содержательный пересказ, но не выдумывай
подробности, причины ДТП, число участников или пострадавших.
Если описания мало, не дополняй его догадками.
Не придумывай факты, которых нет в предоставленных материалах.
Не добавляй ссылки на источники.
Не пиши вступление о том, что ты искусственный интеллект.
Не используй markdown-ссылки.
Сделай пост компактным, максимум 3500 символов.
Если информации мало, лучше напиши короче.

Материалы:
{chr(10).join(source_text)}

Верни только готовый текст публикации.
"""

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        text = (response.text or "").strip()
        if text:
            return text[:3980]
        print("Gemini вернул пустой ответ; использую RSS.")
    except Exception as exc:
        print(
            "Gemini недоступен; использую заголовки RSS:",
            type(exc).__name__,
            str(exc)[:300],
        )

    return fallback_news_post(news)


def main():
    destination = CHANNEL if PUBLISH else OWNER

    print("Получаю прогноз погоды...")
    try:
        weather_text = weather()
        send_text(destination, weather_text)
        print("Погода отправлена.")
    except Exception as exc:
        print("Не удалось получить/отправить погоду:", type(exc).__name__)
        try:
            send_text(
                OWNER,
                "⚠️ Не удалось получить или отправить прогноз погоды. "
                "Подробности смотри в GitHub Actions."
            )
        except Exception:
            pass

    print("Ищу свежие местные новости...")
    try:
        news = search_news()
    except Exception as exc:
        print("Ошибка RSS-поиска:", type(exc).__name__)
        news = []

    try:
        client = genai.Client(api_key=GEMINI_KEY)
        post = make_news_post(client, news)
    except Exception as exc:
        print("Не удалось создать пост:", type(exc).__name__)
        post = fallback_news_post(news) if news else (
            "📰 Новости региона\n\n"
            "Сейчас не удалось получить свежие публикации. "
            "Попробуем снова при следующем запуске."
        )

    try:
        send_text(destination, post)
        print("Общий пост отправлен.")
        if not PUBLISH:
            print("PUBLISH=false: отправлено владельцу, не в канал.")
    except Exception as exc:
        print("Не удалось отправить общий пост:", type(exc).__name__)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Ошибка запуска:", type(exc).__name__, str(exc)[:500])
        try:
            send_text(
                OWNER,
                "Автопост не сработал.\n"
                f"Ошибка: {type(exc).__name__}\n"
                "Подробности смотри в GitHub Actions."
            )
        except Exception:
            pass
        raise

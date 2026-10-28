
import os
import re
import json
import html
import uuid
import urllib.request
import urllib.parse
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
MAX_NEWS = 3

CODES = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность",
    3: "пасмурно", 45: "туман", 48: "туман",
    51: "морось", 53: "морось", 55: "сильная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег",
    80: "ливни", 81: "ливни", 82: "сильные ливни", 95: "гроза",
}


def http_request(url, data=None, headers=None, timeout=25):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LocalNewsBot/1.0)",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def http_json(url, data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    raw = http_request(
        url, body, {"Content-Type": "application/json"}, timeout=25
    )
    result = json.loads(raw.decode("utf-8"))
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError("Telegram API: " + str(result.get("description", "ошибка")))
    return result


def weather():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=54.366&longitude=77.298"
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max"
        "&timezone=Asia%2FNovosibirsk&forecast_days=2&wind_speed_unit=ms"
    )
    data = http_json(url)
    c, d = data["current"], data["daily"]

    def describe(code):
        return CODES.get(code, "переменная погода")

    def chance(value):
        return 0 if value is None else value

    return (
        "☀️ Всем привет!\n\n"
        f"🌡 Сегодня на улице: {round(c['temperature_2m'])}°, "
        f"ощущается как {round(c['apparent_temperature'])}°, "
        f"{describe(c['weather_code'])}, ветер {c['wind_speed_10m']:.0f} м/с.\n\n"
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


def normalize_url(url):
    try:
        p = urllib.parse.urlsplit(url.strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        return (p.hostname.lower(), p.path.rstrip("/"))
    except Exception:
        return None


def news_prompt(hours):
    now = datetime.now(TZ)
    start = now - timedelta(hours=hours)

    return f"""
Ты — редактор местного новостного Telegram-канала.

СЕЙЧАС: {now.strftime("%d.%m.%Y %H:%M")} по времени Новосибирска.
Ищи публикации, опубликованные с {start.strftime("%d.%m.%Y %H:%M")}.

ИЩИ ОТДЕЛЬНО ПО ВСЕМ ЭТИМ ТЕРРИТОРИЯМ:
1. Купино и Купинский район.
2. Новосибирск — все районы, включая Октябрьский район
   и улицу Выборную.
3. Вся Новосибирская область: города, сёла и районы.

Сделай несколько поисковых запросов для разных территорий.
Используй свежие публикации местных СМИ, городских изданий,
официальных служб и администраций.

Ищи реальные события: ДТП, пожары, происшествия, городские
мероприятия, благоустройство, работу служб, важные изменения
и другие новости, которые интересны местным жителям.

Найди до {MAX_NEWS} разных новостей. Сначала выбирай самые свежие
и важные. Не повторяй одно событие дважды.

ДЛЯ КАЖДОЙ НОВОСТИ:
- Проверь дату публикации и открой первоисточник.
- Не используй старые события как свежие новости.
- Не выдумывай события, ссылки, цитаты и обстоятельства.
- Не выдавай предположение за установленный факт.
- Для ДТП указывай причину, участников, последствия и пострадавших
  только тогда, когда эта информация подтверждена.
- Если причина неизвестна, не пытайся её угадать.

ТЕКСТ НОВОСТИ:
Напиши подробный пересказ примерно на 5–8 предложений,
если в источнике достаточно информации.
Расскажи самое важное: что произошло, где и когда,
как развивались события, кто участвовал, что сообщили
официальные службы и чем всё закончилось.
Добавляй конкретные факты, но не повторяй одну мысль.
Если в источнике мало сведений, пиши короче, не выдумывай.

Верни каждую новость в следующем формате:

NEWS_START
PLACE: населённый пункт и район
DATE: дата публикации ДД.ММ.ГГГГ
TITLE: короткий заголовок
TEXT: подробный пересказ новости
URL: прямая ссылка на статью
NEWS_END

Ссылку используй для внутренней проверки и поиска фотографии.
В готовой публикации Telegram ссылка показываться не должна.

Не добавляй IMAGE_URL.
Если подходящих подтверждённых новостей нет, напиши ровно:
НОВОСТЕЙ НЕТ

Возвращай только блоки новостей или фразу НОВОСТЕЙ НЕТ.
"""


def get_grounded_urls(response):
    urls = set()
    try:
        for candidate in response.candidates or []:
            metadata = candidate.grounding_metadata
            if not metadata:
                continue
            for chunk in metadata.grounding_chunks or []:
                web = getattr(chunk, "web", None)
                url = getattr(web, "uri", None) if web else None
                if url and normalize_url(url):
                    urls.add(normalize_url(url))
    except Exception as exc:
        print("Не удалось прочитать ссылки поиска:", type(exc).__name__)
    return urls


def parse_news(text, grounded):
    blocks = re.findall(
        r"NEWS_START(.*?)NEWS_END", text or "", flags=re.S | re.I
    )
    results = []
    today = datetime.now(TZ).date()
    earliest = today - timedelta(days=7)

    for block in blocks:
        fields = {}
        for line in block.strip().splitlines():
            m = re.match(
                r"^\s*(PLACE|DATE|TITLE|TEXT|URL):\s*(.*)$",
                line
            )
            if m:
                key, value = m.group(1).upper(), m.group(2).strip()
                if key == "TEXT" and key in fields:
                    fields[key] += "\n" + value
                else:
                    fields[key] = value

        place = fields.get("PLACE", "")
        date = fields.get("DATE", "")
        title = fields.get("TITLE", "")
        description = fields.get("TEXT", "")
        url = fields.get("URL", "")
        normalized = normalize_url(url)

        if not all((place, date, title, description, url)):
            continue

        # URL должна совпадать с источником, найденным Google Search.
        if not normalized or normalized not in grounded:
            print("Пропуск: ссылка не подтверждена поиском:", title)
            continue

        try:
            published = datetime.strptime(date, "%d.%m.%Y").date()
        except ValueError:
            continue

        if published < earliest or published > today:
            continue

        results.append({
            "place": place,
            "date": date,
            "title": title,
            "text": description,
            "url": url,
        })

    unique = {}
    for item in results:
        unique.setdefault(normalize_url(item["url"]), item)

    return list(unique.values())[:MAX_NEWS]


def search_news(client):
    for hours in (48, 168):
        print(f"Поиск местных новостей за {hours} часов...")

        response = client.models.generate_content(
            model=MODEL,
            contents=news_prompt(hours),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
            ),
        )

        text = response.text or ""
        grounded = get_grounded_urls(response)
        news = parse_news(text, grounded)
        print("Подтверждённых новостей:", len(news))

        if news:
            return news

    return []


def get_image_url(article_url):
    try:
        page = http_request(article_url, timeout=15).decode(
            "utf-8", errors="replace"
        )
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, page, re.I)
            if match:
                image_url = html.unescape(match.group(1).strip())
                image_url = urllib.parse.urljoin(article_url, image_url)
                parsed = urllib.parse.urlsplit(image_url)
                if parsed.scheme in ("http", "https") and parsed.hostname:
                    return image_url
    except Exception as exc:
        print("Картинку получить не удалось:", type(exc).__name__)

    return None


def send_text(chat, text):
    return http_json(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        {
            "chat_id": chat,
            "text": text[:4096],
            "disable_web_page_preview": True,
        },
    )


def send_photo(chat, photo_url, caption):
    image_data = http_request(photo_url, timeout=20)
    boundary = "----BotBoundary" + uuid.uuid4().hex
    body = bytearray()

    for name, value in (
        ("chat_id", str(chat)),
        ("caption", caption[:1024]),
    ):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        b'Content-Disposition: form-data; name="photo"; filename="news-image"\r\n'
    )
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(image_data)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "LocalNewsBot/1.0",
        },
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram не принял фотографию")

    return result


def news_caption(item):
    # Ссылка нужна для проверки и загрузки фото, но в Telegram не выводится.
    return (
        f"📰 {item['title']}\n\n"
        f"📍 {item['place']}\n"
        f"📅 {item['date']}\n\n"
        f"{item['text']}"
    )


def deliver_news(chat, news):
    for item in news:
        caption = news_caption(item)
        photo = get_image_url(item["url"])

        try:
            if photo:
                send_photo(chat, photo, caption)
            else:
                send_text(chat, caption)
        except Exception as exc:
            print("Фото не отправлено, отправляю текст:", type(exc).__name__)
            send_text(chat, caption)


def main():
    client = genai.Client(api_key=GEMINI_KEY)

    print("Получаю прогноз погоды...")
    weather_text = weather()

    print("Ищу свежие местные новости...")
    news = search_news(client)

    if not PUBLISH:
        send_text(OWNER, "Черновик — в канал не опубликован.\n\n" + weather_text)
        if news:
            deliver_news(OWNER, news)
        else:
            send_text(
                OWNER,
                "Свежих новостей с подтверждёнными ссылками не найдено. "
                "Подробности смотри в GitHub Actions.",
            )
        return

    send_text(CHANNEL, weather_text)

    if news:
        deliver_news(CHANNEL, news)
        print("Новости отправлены:", len(news))
    else:
        send_text(
            CHANNEL,
            "📰 Пока не удалось найти свежие новости с подтверждёнными источниками."
        )
        send_text(
            OWNER,
            "Предупреждение: свежие новости не найдены. "
            "Проверь журнал GitHub Actions.",
        )


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


import os
import re
import json
import html
import uuid
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["CHANNEL"]
OWNER = os.environ["OWNER_CHAT_ID"]
PUBLISH = os.environ.get("PUBLISH", "").strip().lower() == "true"

TZ = timezone(timedelta(hours=7))
NEWS_HOURS = 72

# Максимум новостей по каждому разделу.
MAX_KUPINO = 2
MAX_NOVOSIBIRSK = 3
MAX_REGION = 2

# Не скачиваем слишком большие файлы.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
    )
}

CODES = {
    0: "ясно",
    1: "в основном ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман",
    51: "морось",
    53: "морось",
    55: "сильная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    80: "ливни",
    81: "ливни",
    82: "сильные ливни",
    95: "гроза",
}

EMOJI = {
    0: "☀️",
    1: "🌤",
    2: "⛅",
    3: "☁️",
    45: "🌫",
    48: "🌫",
    51: "🌧",
    53: "🌧",
    55: "🌧",
    61: "🌦",
    63: "🌧",
    65: "🌧",
    71: "🌨",
    73: "❄️",
    75: "❄️",
    80: "🌦",
    81: "🌧",
    82: "🌧",
    95: "⛈",
}

SECTIONS = [
    ("Купино и Купинский район", MAX_KUPINO),
    ("Новосибирск", MAX_NOVOSIBIRSK),
    ("Новосибирская область", MAX_REGION),
]

# Населённые пункты Новосибирской области.
OBLAST_PLACES = [
    "бердск", "искитим", "караcук", "карасук",
    "татарск", "куйбышев", "барабинск", "чулым",
    "сузун", "ордынское", "колывань", "маслянино",
    "черепаново", "тогучин", "коченево", "линево",
    "обь", "каргат", "здвинск", "убинское",
    "краснозерское", "кочки", "ордынский район",
    "искитимский район", "барабинский район",
    "купино", "купинский район",
]

# Очевидно посторонние регионы и города.
FOREIGN_PLACES = [
    "кузбасс",
    "кемеровская область",
    "кемеровской области",
    "кемерово",
    "прокопьевск",
    "новокузнецк",
    "ленинск-кузнецкий",
    "томская область",
    "томске",
    "омская область",
    "красноярский край",
    "алтайский край",
    "республика алтай",
]


# ============================================================
# HTTP И TELEGRAM
# ============================================================

def http_request(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(
        url,
        data=data,
        headers={**HEADERS, **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def http_json(url, data=None):
    body = None
    headers = {}

    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    raw = http_request(url, body, headers)
    result = json.loads(raw.decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Ошибка Telegram API")
        )

    return result


def send_text(chat, text):
    text = text.strip()

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


def send_photo(chat, image_url, caption):
    """
    Скачивает фотографию с сайта и загружает её в Telegram.
    Если загрузка не удалась, вызывающий код отправит текст новости.
    """

    req = urllib.request.Request(image_url, headers=HEADERS)

    with urllib.request.urlopen(req, timeout=20) as response:
        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if not content_type.startswith("image/"):
            raise ValueError("Ссылка не ведёт на изображение")

        image_data = response.read(MAX_IMAGE_BYTES + 1)

    if not image_data or len(image_data) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение пустое или слишком большое")

    boundary = "----LocalNews" + uuid.uuid4().hex
    chunks = []

    def field(name, value):
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            "\r\n"
            f"{value}\r\n".encode("utf-8")
        )

    field("chat_id", str(chat))

    caption = caption[:1000]
    field("caption", caption)

    parsed = urlparse(image_url)
    ext = os.path.splitext(parsed.path)[1].lower()

    mime_ext = {
        ".jpg": ("image/jpeg", "photo.jpg"),
        ".jpeg": ("image/jpeg", "photo.jpg"),
        ".png": ("image/png", "photo.png"),
        ".webp": ("image/webp", "photo.webp"),
    }

    mime, filename = mime_ext.get(
        ext, ("image/jpeg", "photo.jpg")
    )

    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(image_data)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(chunks)

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=40) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Не удалось отправить фото")
        )

    return result


# ============================================================
# ПОГОДА
# ============================================================

def temperature(value):
    value = round(value)
    return f"+{value}" if value > 0 else str(value).replace("-", "−")


def weather_description(code):
    return CODES.get(code, "переменная облачность")


def weather_emoji(code):
    return EMOJI.get(code, "🌤")


def weather(news_found):
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=54.366&longitude=77.298"
        "&current=temperature_2m,apparent_temperature,"
        "weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max"
        "&timezone=Asia%2FNovosibirsk"
        "&forecast_days=2"
        "&wind_speed_unit=ms"
    )

    data = http_json(url)
    current = data["current"]
    daily = data["daily"]

    today_code = daily["weather_code"][0]
    tomorrow_code = daily["weather_code"][1]

    today_rain = daily["precipitation_probability_max"][0] or 0
    tomorrow_rain = daily["precipitation_probability_max"][1] or 0

    today_text = weather_description(today_code)
    tomorrow_text = weather_description(tomorrow_code)

    if today_rain >= 60 and today_code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        today_text += " 🌧"

    if tomorrow_rain >= 60 and tomorrow_code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        tomorrow_text += " 🌧"

    message = (
        "☀️ Всем привет!\n\n"
        f"🌡Сегодня на улице: "
        f"{temperature(current['temperature_2m'])}°C, "
        f"ощущается как "
        f"{temperature(current['apparent_temperature'])}°C. "
        f"{weather_description(current['weather_code']).capitalize()}, "
        f"ветер {current['wind_speed_10m']:.0f} м/с.\n\n"
        "📅 Прогноз погоды\n"
        f"• Сегодня: от "
        f"{temperature(daily['temperature_2m_min'][0])} "
        f"до {temperature(daily['temperature_2m_max'][0])}°C, "
        f"{today_text}.\n"
        f"• Завтра: от "
        f"{temperature(daily['temperature_2m_min'][1])} "
        f"до {temperature(daily['temperature_2m_max'][1])}°C, "
        f"{tomorrow_text}.\n\n"
    )

    if news_found:
        message += "📰 Свежие новости — ниже.\n\n"
    else:
        message += (
            "📰 Из новостей: за последние дни "
            "ничего интересного не найдено.\n\n"
        )

    message += "Всем хорошего дня и побольше позитива! 👋"
    return message


# ============================================================
# RSS-НОВОСТИ
# ============================================================

def clean_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalise(value):
    return re.sub(r"[\W_]+", "", value.lower())


def article_is_relevant(title, description, area):
    text = (title + " " + description).lower()

    # Не допускаем новости явно из других регионов.
    if any(place in text for place in FOREIGN_PLACES):
        # Исключение — если материал явно описывает событие
        # в Новосибирской области, например на региональной трассе.
        if not any(place in text for place in (
            "новосибирская область",
            "новосибирской области",
            "в новосибирске",
            "новосибирск",
            "бердск",
            "искитим",
            "купино",
            "купинский район",
        )):
            return False

    if area == "Купино и Купинский район":
        return "купино" in text or "купинский район" in text

    if area == "Новосибирск":
        return (
            "новосибирск" in text
            or "новосибирске" in text
            or (
                "выборная" in text
                and "октябрьский район" in text
            )
        )

    if area == "Новосибирская область":
        return (
            "новосибирская область" in text
            or "новосибирской области" in text
            or any(place in text for place in OBLAST_PLACES)
        )

    return False


def get_rss_news(query, area):
    params = urllib.parse.urlencode({
        "q": query + " when:3d",
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
    })

    url = "https://news.google.com/rss/search?" + params

    try:
        root = ET.fromstring(http_request(url, timeout=20))
    except Exception as exc:
        print(f"RSS недоступен ({area}): {type(exc).__name__}")
        return []

    now = datetime.now(TZ)
    cutoff = now - timedelta(hours=NEWS_HOURS)
    found = []

    for entry in root.findall(".//item"):
        title = clean_html(entry.findtext("title", ""))
        link = (entry.findtext("link", "") or "").strip()
        description = clean_html(
            entry.findtext("description", "")
        )
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

        if not article_is_relevant(title, description, area):
            continue

        # Google News RSS иногда повторяет заголовок в описании.
        if description.lower().startswith(title.lower()):
            description = description[len(title):].strip(" -—:|")

        if source and description.lower().endswith(source.lower()):
            description = description[:-len(source)].strip(" -—:|")

        if normalise(description) == normalise(title):
            description = ""

        found.append({
            "area": area,
            "title": title,
            "description": description[:500],
            "source": source,
            "date": published.strftime("%d.%m.%Y"),
            "timestamp": published.timestamp(),
            "url": link,
        })

    return found


def search_news():
    searches = [
        ('"Купино" новости', "Купино и Купинский район"),
        ('"Купинский район" происшествия OR новости', "Купино и Купинский район"),

        ('"Новосибирск" ДТП OR происшествие', "Новосибирск"),
        ('"Новосибирск" городские новости благоустройство', "Новосибирск"),
        ('"Новосибирск" "улица Выборная"', "Новосибирск"),
        ('"Октябрьский район" Новосибирск новости', "Новосибирск"),

        ('"Новосибирская область" происшествия OR новости', "Новосибирская область"),
        ('Бердск OR Искитим новости происшествия', "Новосибирская область"),
    ]

    collected = []

    for query, area in searches:
        print(f"Ищу новости: {query}")
        collected.extend(get_rss_news(query, area))

    # Удаляем дубликаты заголовков.
    unique = {}
    for item in sorted(
        collected,
        key=lambda news: news["timestamp"],
        reverse=True,
    ):
        key = normalise(item["title"])

        if not key or key in unique:
            continue

        unique[key] = item

    # Сначала более свежие публикации, но сохраняем порядок разделов.
    results = []
    for area, limit in SECTIONS:
        area_items = [
            item for item in unique.values()
            if item["area"] == area
        ]
        area_items.sort(
            key=lambda item: item["timestamp"],
            reverse=True,
        )
        results.extend(area_items[:limit])

    print("Подходящих новостей:", len(results))
    return results


# ============================================================
# ФОТО ИЗ ОРИГИНАЛЬНЫХ СТАТЕЙ
# ============================================================

class ImageMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag.lower() == "meta":
            key = (
                attrs.get("property")
                or attrs.get("name")
                or ""
            ).lower()

            value = attrs.get("content", "").strip()

            if key in (
                "og:image",
                "og:image:url",
                "og:image:secure_url",
                "twitter:image",
                "twitter:image:src",
            ) and value:
                self.images.append(value)


def get_article_image(article_url):
    try:
        req = urllib.request.Request(
            article_url,
            headers=HEADERS,
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            final_url = response.geturl()
            raw = response.read(2 * 1024 * 1024)

        page = raw.decode("utf-8", errors="replace")

        parser = ImageMetaParser()
        parser.feed(page)

        for candidate in parser.images:
            candidate = html.unescape(candidate.strip())
            image_url = urljoin(final_url, candidate)
            parsed = urlparse(image_url)

            if parsed.scheme not in ("http", "https"):
                continue

            if not parsed.netloc:
                continue

            if any(
                word in image_url.lower()
                for word in ("logo", "favicon", "avatar", "icon")
            ):
                continue

            return image_url

    except Exception as exc:
        print("Не удалось получить фото:", type(exc).__name__)

    return None


# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================

def short_description(description, title):
    description = clean_html(description)

    if not description:
        return ""

    if normalise(description) == normalise(title):
        return ""

    # Не превращаем RSS-описание в длинный абзац.
    if len(description) > 240:
        description = description[:237].rsplit(" ", 1)[0] + "…"

    return description


def news_caption(item):
    area = item["area"]
    title = item["title"].strip()
    description = short_description(
        item["description"], title
    )

    caption = f"📰 {area}\n\n{title}"

    if description:
        caption += "\n\n" + description

    caption += f"\n\n📅 {item['date']}"

    return caption[:1000]


def publish_news(chat, news):
    if not news:
        print("Подходящих новостей нет.")
        return

    last_area = None

    for item in news:
        if item["area"] != last_area:
            heading = f"📍 {item['area']}"
            send_text(chat, heading)
            last_area = item["area"]

        caption = news_caption(item)
        image_url = get_article_image(item["url"])

        if image_url:
            try:
                send_photo(chat, image_url, caption)
                print("Новость с фото опубликована:", item["title"])
                continue
            except Exception as exc:
                print(
                    "Не удалось отправить фото, отправляю текст:",
                    type(exc).__name__,
                )

        send_text(chat, caption)
        print("Новость без фото опубликована:", item["title"])


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    destination = CHANNEL if PUBLISH else OWNER

    print("Ищу свежие местные новости...")
    try:
        news = search_news()
    except Exception as exc:
        print("Ошибка поиска новостей:", type(exc).__name__)
        news = []

    print("Получаю прогноз погоды...")
    try:
        send_text(destination, weather(bool(news)))
        print("Погода опубликована.")
    except Exception as exc:
        print("Ошибка погоды:", type(exc).__name__)

        if destination != OWNER:
            try:
                send_text(
                    OWNER,
                    "⚠️ Не удалось получить прогноз погоды. "
                    "Подробности смотри в GitHub Actions."
                )
            except Exception:
                pass

    try:
        publish_news(destination, news)
        print("Публикация новостей завершена.")
    except Exception as exc:
        print("Ошибка публикации:", type(exc).__name__, str(exc)[:300])

        try:
            send_text(
                OWNER,
                "⚠️ Ошибка публикации новостей. "
                "Подробности смотри в GitHub Actions."
            )
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Критическая ошибка:", type(exc).__name__, str(exc)[:300])
        try:
            send_text(
                OWNER,
                "⚠️ Бот завершился с ошибкой. "
                "Подробности смотри в GitHub Actions."
            )
        except Exception:
            pass
        raise

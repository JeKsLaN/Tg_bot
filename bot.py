import os
import re
import json
import time
import html
import hashlib
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["CHANNEL"]
PUBLISH = os.getenv("PUBLISH", "false").lower() == "true"

STATE_FILE = "bot_state.json"
NOVOSIBIRSK_TZ = timezone(timedelta(hours=7))
KUPINO_LAT = 54.366
KUPINO_LON = 77.298

INCIDENT_WORDS = (
    "взрыв", "взорвал", "пожар", "горит", "загорел", "дтп",
    "авари", "происшеств", "погиб", "погибш", "пострадал",
    "ранен", "обруш", "эвакуац", "пропал", "розыск",
    "перекрыли", "отключение", "коммунальная авария",
    "массовое отравление", "нападение", "стрельба",
    "сход вагонов", "разлив", "утечка газа"
)

RETRACTION_WORDS = (
    "новость опровергли",
    "информация не подтвердилась",
    "сообщение не подтвердилось",
    "фейк",
    "опровергли информацию"
)

BAD_REGIONS = (
    "кемеров", "кузбасс", "красноярск", "иркутск",
    "омск", "барнаул", "томск", "алтайский край"
)

FEEDS = [
    ("kupino", '"Купино" новости when:3d'),
    ("kupino", '"Купинский район" новости when:3d'),
    ("novosibirsk", '"Новосибирск" новости when:2d'),
    ("novosibirsk", '"Новосибирск" происшествия when:2d'),
]


def now_local():
    return datetime.now(NOVOSIBIRSK_TZ)


def request(url, timeout=15, headers=None):
    req = urllib.request.Request(
        url,
        headers=headers or {
            "User-Agent": "Mozilla/5.0 (compatible; LocalNewsBot/1.0)"
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(), response.geturl(), response.headers.get("Content-Type", "")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        state = {}

    state.setdefault("daily_date", "")
    state.setdefault("posted", [])
    state.setdefault("pending", [])
    return state


def save_state(state):
    # Ограничиваем историю, чтобы файл не рос бесконечно.
    state["posted"] = state.get("posted", [])[-1000:]
    state["pending"] = state.get("pending", [])[-200:]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def story_id(link, title):
    key = link or title
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def fetch_feed(location, query):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query,
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru"
    })

    try:
        raw, _, _ = request(url)
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"RSS error ({location}): {exc}")
        return []

    results = []
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title", ""))
        link = item.findtext("link", "").strip()
        description = clean_text(item.findtext("description", ""))
        pub_date = item.findtext("pubDate", "")

        if not title or not link:
            continue

        results.append({
            "id": story_id(link, title),
            "location": location,
            "title": title,
            "description": description[:700],
            "link": link,
            "pub_date": pub_date,
        })

    return results


def get_all_news():
    found = []
    seen = set()

    for location, query in FEEDS:
        for item in fetch_feed(location, query):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            found.append(item)

    return found


def is_local_story(item, location):
    text = (item["title"] + " " + item["description"]).lower()

    if location == "kupino":
        return (
            "купино" in text
            or "купинский район" in text
        )

    if location == "novosibirsk":
        if "новосибирск" not in text and "новосибирской области" not in text:
            return False

        # Не публикуем новости соседних регионов только из-за случайного
        # упоминания Новосибирска в тексте.
        title = item["title"].lower()
        if any(region in title for region in BAD_REGIONS):
            return False

        return True

    return False


def is_incident(item):
    text = (item["title"] + " " + item["description"]).lower()
    return any(word in text for word in INCIDENT_WORDS)


def get_weather():
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={KUPINO_LAT}&longitude={KUPINO_LON}"
        "&current=temperature_2m,apparent_temperature,wind_speed_10m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code"
        "&timezone=Asia%2FNovosibirsk&forecast_days=2"
    )

    raw, _, _ = request(url)
    data = json.loads(raw.decode("utf-8"))

    current = data["current"]
    daily = data["daily"]

    def weather_name(code):
        if code == 0:
            return "ясно ☀️"
        if code in (1, 2):
            return "переменная облачность 🌤"
        if code == 3:
            return "облачно ☁️"
        if code in (45, 48):
            return "туман 🌫"
        if code in (51, 53, 55, 56, 57):
            return "морось 🌧"
        if code in (61, 63, 65, 66, 67):
            return "дождь 🌧"
        if code in (71, 73, 75, 77):
            return "снег ❄️"
        if code in (80, 81, 82):
            return "ливень 🌧"
        if code in (85, 86):
            return "снегопад ❄️"
        if code in (95, 96, 99):
            return "гроза ⛈"
        return "переменная погода"

    temp = round(current["temperature_2m"])
    feels = round(current["apparent_temperature"])
    wind = round(current["wind_speed_10m"])
    today_min = round(daily["temperature_2m_min"][0])
    today_max = round(daily["temperature_2m_max"][0])
    tomorrow_min = round(daily["temperature_2m_min"][1])
    tomorrow_max = round(daily["temperature_2m_max"][1])

    today_desc = weather_name(daily["weather_code"][0])
    tomorrow_desc = weather_name(daily["weather_code"][1])

    def signed(n):
        return f"+{n}" if n > 0 else str(n)

    return (
        "☀️ Всем привет!\n\n"
        f"🌡 Сейчас в Купино: {signed(temp)}°C, ощущается как "
        f"{signed(feels)}°C. {weather_name(current['weather_code'])}, "
        f"ветер {wind} м/с.\n\n"
        "📅 Прогноз погоды\n"
        f"• Сегодня: от {signed(today_min)} до {signed(today_max)}°C, {today_desc}.\n"
        f"• Завтра: от {signed(tomorrow_min)} до {signed(tomorrow_max)}°C, "
        f"{tomorrow_desc}.\n\n"
        "Всем хорошего дня и побольше позитива! 👋"
    )


def telegram_api(method, fields=None, photo=None, photo_type=None):
    if not PUBLISH:
        print(f"[TEST MODE] Telegram {method}: {fields}")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/{method}"

    if photo is None:
        body = urllib.parse.urlencode(fields or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
    else:
        boundary = "----LocalNewsBotBoundary7MA4YWxkTrZu0gW"
        chunks = []

        for key, value in (fields or {}).items():
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            )
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")

        filename = "news.jpg" if photo_type == "image/jpeg" else "news.png"
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {photo_type}\r\n\r\n".encode())
        chunks.append(photo)
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())

        req = urllib.request.Request(
            url,
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    return result


def send_text(text):
    telegram_api("sendMessage", {
        "chat_id": CHANNEL,
        "text": text,
        "disable_web_page_preview": "true"
    })


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = None
        self.title = None
        self.description = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return

        a = {k.lower(): v for k, v in attrs if k}
        key = (a.get("property") or a.get("name") or "").lower()
        content = a.get("content", "").strip()

        if key in ("og:image", "twitter:image") and content and not self.image:
            self.image = content
        elif key == "og:title" and content:
            self.title = content
        elif key == "og:description" and content:
            self.description = content


def get_article_photo(article_url):
    try:
        raw, final_url, content_type = request(article_url, timeout=20)
        page = raw.decode("utf-8", errors="ignore")

        # Иногда RSS ведёт через Google News на настоящий сайт.
        parser = MetaParser()
        parser.feed(page)

        if not parser.image:
            return None, None

        image_url = urllib.parse.urljoin(final_url, html.unescape(parser.image))
        image_raw, _, image_content_type = request(image_url, timeout=20)

        # Не отправляем HTML или произвольные файлы вместо фотографий.
        if image_raw.startswith(b"\xff\xd8\xff"):
            return image_raw, "image/jpeg"
        if image_raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return image_raw, "image/png"

        if "image/jpeg" in image_content_type and image_raw:
            return image_raw, "image/jpeg"
        if "image/png" in image_content_type and image_raw:
            return image_raw, "image/png"

    except Exception as exc:
        print(f"Photo unavailable: {exc}")

    return None, None


def publish_story(item, label=""):
    title = item["title"].strip()
    description = item["description"].strip()

    if len(description) > 350:
        description = description[:347].rsplit(" ", 1)[0] + "..."

    caption = f"📰 {title}"
    if description and description.lower() not in title.lower():
        caption += f"\n\n{description}"
    caption += f"\n\nИсточник: {item['link']}"

    if label:
        caption = f"{label}\n\n{caption}"

    caption = caption[:1000]

    photo, photo_type = get_article_photo(item["link"])

    if photo:
        try:
            telegram_api("sendPhoto", {
                "chat_id": CHANNEL,
                "caption": caption
            }, photo=photo, photo_type=photo_type)
            return
        except Exception as exc:
            print(f"Photo send failed, sending text: {exc}")

    send_text(caption)


def choose_story(news, location, already_posted):
    candidates = [
        item for item in news
        if item["location"] == location
        and is_local_story(item, location)
        and item["id"] not in already_posted
    ]

    if not candidates:
        return None

    # Сначала выбираем материал с наиболее свежей датой из RSS.
    return candidates[0]


def daily_post(state, news):
    today = now_local().date().isoformat()
    if state.get("daily_date") == today:
        return

    print("Publishing daily weather and news")

    try:
        send_text(get_weather())
    except Exception as exc:
        print(f"Weather error: {exc}")
        send_text("☀️ Всем привет!\n\nНе удалось загрузить погоду. Попробуем обновить её позже.")

    posted = set(state.get("posted", []))

    kupino_story = choose_story(news, "kupino", posted)
    if kupino_story:
        publish_story(kupino_story, "📍 Купино и Купинский район")
        posted.add(kupino_story["id"])
    else:
        print("No suitable Kupino story found; skipping it.")

    novosibirsk_story = choose_story(news, "novosibirsk", posted)
    if novosibirsk_story:
        publish_story(novosibirsk_story, "📍 Новосибирск")
        posted.add(novosibirsk_story["id"])
    else:
        print("No suitable Novosibirsk story found.")

    state["posted"] = list(posted)
    state["daily_date"] = today
    save_state(state)


def process_incidents(state, news):
    posted = set(state.get("posted", []))
    pending = state.get("pending", [])
    pending_ids = {item["id"] for item in pending}
    current_time = time.time()

    # Запоминаем новые происшествия; обычные новости в очередь не попадают.
    for item in news:
        if item["id"] in posted or item["id"] in pending_ids:
            continue
        if not is_local_story(item, item["location"]):
            continue
        if not is_incident(item):
            continue

        item["detected_at"] = current_time
        # Разброс задержки 15–20 минут, стабильный для конкретной новости.
        delay = 900 + (int(item["id"][:4], 16) % 301)
        item["publish_after"] = current_time + delay
        pending.append(item)
        pending_ids.add(item["id"])
        print(f"Queued incident for delayed publication: {item['title']}")

    remaining = []

    for item in pending:
        if item["id"] in posted:
            continue

        if current_time < float(item.get("publish_after", current_time + 900)):
            remaining.append(item)
            continue

        # Проверяем, доступна ли исходная публикация и не содержит ли
        # явного сообщения об опровержении.
        try:
            raw, _, _ = request(item["link"], timeout=15)
            page_text = clean_text(raw.decode("utf-8", errors="ignore")).lower()

            if any(word in page_text for word in RETRACTION_WORDS):
                print(f"Skipping possibly retracted story: {item['title']}")
                posted.add(item["id"])
                continue
        except Exception as exc:
            print(f"Could not recheck story, keeping it pending: {exc}")
            remaining.append(item)
            continue

        try:
            publish_story(item, "🚨 Новое происшествие")
            posted.add(item["id"])
            print(f"Published incident: {item['title']}")
        except Exception as exc:
            print(f"Incident publication failed: {exc}")
            remaining.append(item)

    state["posted"] = list(posted)
    state["pending"] = remaining
    save_state(state)


def main():
    state = load_state()

    try:
        news = get_all_news()
    except Exception as exc:
        print(f"Could not fetch news: {exc}")
        news = []

    now = now_local()
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    # Ежедневный выпуск: после 09:00 по Новосибирску, только один раз в день.
    if now.hour >= 9 or is_manual:
        daily_post(state, news)

    # Новые происшествия публикуются отдельно после задержки 15–20 минут.
    process_incidents(state, news)


if __name__ == "__main__":
    main()

import os
import re
import json
import time
import html
import hashlib
import difflib
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from io import BytesIO
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

try:
    from PIL import Image
except ImportError:
    Image = None


# =========================
# НАСТРОЙКИ
# =========================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["CHANNEL"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PUBLISH = os.getenv("PUBLISH", "false").lower() == "true"

STATE_FILE = "bot_state.json"
LOCAL_TZ = timezone(timedelta(hours=7))

KUPINO_LAT = 54.366
KUPINO_LON = 77.298

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
)

GOOGLE_HOSTS = (
    "news.google.com",
    "google.com",
    "www.google.com",
    "gstatic.com",
    "googleusercontent.com",
)

INCIDENT_WORDS = (
    "взрыв", "взорвал", "пожар", "горит", "загорел",
    "дтп", "авари", "происшеств", "погиб", "пострадал",
    "ранен", "обруш", "эвакуац", "пропал", "розыск",
    "перекрыли", "отключение", "коммунальная авария",
    "массовое отравление", "нападение", "стрельба",
    "сход вагонов", "разлив", "утечка газа",
)

RETRACTION_WORDS = (
    "информация не подтвердилась",
    "сообщение не подтвердилось",
    "новость опровергли",
    "опровергли информацию",
    "официально опровергли",
    "оказалось фейком",
)

BAD_REGIONS = (
    "кемеров", "кузбасс", "красноярск", "иркутск",
    "омск", "барнаул", "томск", "алтайский край",
)

FEEDS = [
    ("kupino", '"Купино" новости when:3d'),
    ("kupino", '"Купинский район" новости when:3d'),
    ("novosibirsk", '"Новосибирск" новости when:2d'),
    ("novosibirsk", '"Новосибирск" происшествия when:2d'),
]


# =========================
# СЕТЬ И СОСТОЯНИЕ
# =========================

def now_local():
    return datetime.now(LOCAL_TZ)


def request(url, timeout=20, headers=None, max_bytes=8_000_000):
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }

    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(url, headers=request_headers)

    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)

        if len(data) > max_bytes:
            raise ValueError("Ответ слишком большой")

        return (
            data,
            response.geturl(),
            response.headers.get("Content-Type", ""),
        )


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError):
        state = {}

    state.setdefault("daily_date", "")
    state.setdefault("posted", [])
    state.setdefault("posted_titles", [])
    state.setdefault("pending", [])

    return state


def save_state(state):
    state["posted"] = state.get("posted", [])[-1500:]
    state["posted_titles"] = state.get("posted_titles", [])[-1000:]
    state["pending"] = state.get("pending", [])[-200:]

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(value):
    value = html.unescape(value or "").lower().replace("ё", "е")
    value = re.sub(r"\s+[—–-]\s+[^—–-]{2,60}$", "", value)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-zа-я0-9 ]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def story_id(link, title):
    key = (link or "") + "|" + normalize_title(title)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def same_story(title, previous_titles):
    normalized = normalize_title(title)

    if not normalized:
        return False

    for old in previous_titles:
        old_title = normalize_title(old)

        if not old_title:
            continue

        if normalized == old_title:
            return True

        if min(len(normalized), len(old_title)) >= 22:
            similarity = difflib.SequenceMatcher(
                None, normalized, old_title
            ).ratio()

            if similarity >= 0.86:
                return True

    return False


def host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_google_host(url):
    host = host_of(url)

    return any(
        host == domain or host.endswith("." + domain)
        for domain in GOOGLE_HOSTS
    )


# =========================
# ПОЛУЧЕНИЕ НОВОСТЕЙ
# =========================

class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.image_urls = []
        self.canonical = None
        self.title = None
        self.description = None
        self.anchors = []
        self.paragraphs = []

        self.anchor_href = None
        self.anchor_text = []

        self.in_paragraph = False
        self.paragraph_text = []

        self.in_title = False
        self.title_text = []

        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {key.lower(): value for key, value in attrs if key}

        if tag in ("script", "style", "noscript", "svg"):
            self.skip_depth += 1
            return

        if self.skip_depth:
            return

        if tag == "meta":
            key = (
                attrs.get("property")
                or attrs.get("name")
                or ""
            ).lower()

            content = html.unescape(
                (attrs.get("content") or "").strip()
            )

            if key in (
                "og:image",
                "og:image:secure_url",
                "twitter:image",
                "twitter:image:src",
            ):
                if content:
                    self.image_urls.append(content)

            elif key in ("og:title", "twitter:title"):
                if content and not self.title:
                    self.title = content

            elif key in (
                "og:description",
                "description",
                "twitter:description",
            ):
                if content and not self.description:
                    self.description = content

        elif tag == "link":
            rel = (attrs.get("rel") or "").lower().split()

            if (
                "canonical" in rel
                and attrs.get("href")
                and not self.canonical
            ):
                self.canonical = attrs["href"]

        elif tag == "img":
            for key in (
                "src",
                "data-src",
                "data-original",
                "data-lazy-src",
                "data-lazy",
            ):
                if attrs.get(key):
                    self.image_urls.append(attrs[key])

            srcset = attrs.get("srcset")

            if srcset:
                candidate = srcset.split(",")[-1].strip().split(" ")[0]

                if candidate:
                    self.image_urls.append(candidate)

        elif tag == "a":
            self.anchor_href = attrs.get("href")
            self.anchor_text = []

        elif tag == "p":
            self.in_paragraph = True
            self.paragraph_text = []

        elif tag == "title":
            self.in_title = True
            self.title_text = []

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("script", "style", "noscript", "svg"):
            if self.skip_depth:
                self.skip_depth -= 1
            return

        if self.skip_depth:
            return

        if tag == "a" and self.anchor_href:
            self.anchors.append((
                self.anchor_href,
                clean_text(" ".join(self.anchor_text)),
            ))

            self.anchor_href = None
            self.anchor_text = []

        elif tag == "p" and self.in_paragraph:
            paragraph = clean_text(" ".join(self.paragraph_text))

            if len(paragraph) > 35:
                self.paragraphs.append(paragraph)

            self.in_paragraph = False
            self.paragraph_text = []

        elif tag == "title":
            title = clean_text(" ".join(self.title_text))

            if title and not self.title:
                self.title = title

            self.in_title = False
            self.title_text = []

    def handle_data(self, data):
        if self.skip_depth:
            return

        if self.anchor_href is not None:
            self.anchor_text.append(data)

        if self.in_paragraph:
            self.paragraph_text.append(data)

        if self.in_title:
            self.title_text.append(data)


def fetch_feed(location, query):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query,
        "hl": "ru",
        "gl": "RU",
        "ceid": "RU:ru",
    })

    try:
        raw, _, _ = request(url)
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"RSS error ({location}): {exc}")
        return []

    results = []

    for node in root.findall(".//item"):
        title = clean_text(node.findtext("title", ""))
        link = node.findtext("link", "").strip()
        description_html = node.findtext("description", "")
        description = clean_text(description_html)
        pub_date = node.findtext("pubDate", "")

        source_el = node.find("source")

        source_name = (
            clean_text(source_el.text or "")
            if source_el is not None
            else ""
        )

        source_url = (
            source_el.attrib.get("url", "").strip()
            if source_el is not None
            else ""
        )

        if not title or not link:
            continue

        rss_parser = ArticleParser()

        try:
            rss_parser.feed(description_html)
        except Exception:
            pass

        results.append({
            "id": story_id(link, title),
            "location": location,
            "title": title,
            "description": description[:1600],
            "link": link,
            "pub_date": pub_date,
            "source_name": source_name,
            "source_url": source_url,
            "rss_images": rss_parser.image_urls,
        })

    return results


def get_all_news():
    found = []

    for location, query in FEEDS:
        found.extend(fetch_feed(location, query))

    unique = []

    for item in found:
        duplicate = any(
            item["location"] == old["location"]
            and same_story(item["title"], [old["title"]])
            for old in unique
        )

        if not duplicate:
            unique.append(item)

    def date_key(item):
        try:
            return datetime.strptime(
                item["pub_date"],
                "%a, %d %b %Y %H:%M:%S %Z",
            ).timestamp()
        except Exception:
            return 0

    unique.sort(key=date_key, reverse=True)

    return unique


def is_local_story(item, location):
    text = (
        item["title"] + " " + item["description"]
    ).lower()

    if location == "kupino":
        return (
            "купино" in text
            or "купинский район" in text
        )

    if location == "novosibirsk":
        if (
            "новосибирск" not in text
            and "новосибирской области" not in text
        ):
            return False

        title = item["title"].lower()

        if any(region in title for region in BAD_REGIONS):
            return False

        return True

    return False


def is_incident(item):
    text = (
        item["title"] + " " + item["description"]
    ).lower()

    return any(word in text for word in INCIDENT_WORDS)


# =========================
# ПОГОДА
# =========================

def weather_name(code):
    code = int(code)

    if code == 0:
        return "ясно ☀️"
    if code == 1:
        return "преимущественно ясно 🌤"
    if code == 2:
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


def signed(value):
    value = int(round(float(value)))
    return f"+{value}" if value > 0 else str(value)


def get_weather():
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={KUPINO_LAT}&longitude={KUPINO_LON}"
        "&current=temperature_2m,apparent_temperature,"
        "wind_speed_10m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code"
        "&timezone=Asia%2FNovosibirsk&forecast_days=2"
    )

    raw, _, _ = request(url)
    data = json.loads(raw.decode("utf-8"))

    current = data["current"]
    daily = data["daily"]

    return (
        "☀️ Всем привет!\n\n"
        f"🌡 Сейчас в Купино: "
        f"{signed(current['temperature_2m'])}°C, "
        f"ощущается как {signed(current['apparent_temperature'])}°C. "
        f"{weather_name(current['weather_code'])}, "
        f"ветер {round(current['wind_speed_10m'])} м/с.\n\n"
        "📅 Прогноз погоды\n"
        f"• Сегодня: от {signed(daily['temperature_2m_min'][0])} "
        f"до {signed(daily['temperature_2m_max'][0])}°C, "
        f"{weather_name(daily['weather_code'][0])}.\n"
        f"• Завтра: от {signed(daily['temperature_2m_min'][1])} "
        f"до {signed(daily['temperature_2m_max'][1])}°C, "
        f"{weather_name(daily['weather_code'][1])}.\n\n"
        "Всем хорошего дня и побольше позитива! 👋"
    )


# =========================
# ОРИГИНАЛЬНАЯ СТАТЬЯ
# =========================

def resolve_article(item):
    """
    Пытается перейти с Google News на сайт издателя.
    Если оригинальный адрес не удаётся подтвердить,
    оставляет ссылку RSS, не выдавая её за адрес издателя.
    """
    original = item["link"]

    try:
        raw, final_url, content_type = request(
            original,
            timeout=20,
        )

        if (
            final_url.startswith("https://")
            and not is_google_host(final_url)
        ):
            return final_url

        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="ignore"))

        target_title = normalize_title(item["title"])
        candidates = []

        for href, anchor_text in parser.anchors:
            absolute = urllib.parse.urljoin(
                final_url,
                html.unescape(href),
            )

            if (
                not absolute.startswith("https://")
                or is_google_host(absolute)
            ):
                continue

            if any(part in absolute.lower() for part in (
                "accounts.google",
                "support.google",
                "policies.google",
                "consent.google",
                "youtube.com",
            )):
                continue

            normalized_anchor = normalize_title(anchor_text)

            score = (
                difflib.SequenceMatcher(
                    None,
                    target_title,
                    normalized_anchor,
                ).ratio()
                if normalized_anchor
                else 0
            )

            candidates.append((score, absolute))

        if candidates:
            candidates.sort(reverse=True)

            if candidates[0][0] >= 0.45:
                return candidates[0][1]

    except Exception as exc:
        print(f"Article URL resolution failed: {exc}")

    source_url = item.get("source_url", "")

    if (
        source_url.startswith("https://")
        and not is_google_host(source_url)
    ):
        return source_url

    return original


def fetch_article(article_url):
    try:
        raw, final_url, content_type = request(
            article_url,
            timeout=20,
        )

        if (
            is_google_host(final_url)
            or "html" not in content_type.lower()
        ):
            return final_url, None, []

        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="ignore"))

        canonical = urllib.parse.urljoin(
            final_url,
            parser.canonical or final_url,
        )

        if not is_google_host(canonical):
            final_url = canonical

        description = parser.description

        if not description or len(description) < 80:
            description = " ".join(parser.paragraphs[:8])

        return final_url, description, parser.image_urls

    except Exception as exc:
        print(f"Article fetch failed: {exc}")
        return article_url, None, []


# =========================
# ПЕРЕСКАЗ НОВОСТИ
# =========================

def openai_summary(title, rss_description, article_description):
    source_text = clean_text(
        article_description or rss_description
    )

    if not source_text:
        return (
            "Подробный текст статьи недоступен. "
            "Проверьте информацию по ссылке на источник."
        )

    if not OPENAI_API_KEY:
        return source_text[:700]

    prompt = (
        "Ты редактор новостного канала на русском языке. "
        "Сделай содержательный пересказ в 3–5 предложениях. "
        "Сохрани конкретные цифры, даты, периоды сравнения, "
        "имена и причины, если они прямо указаны в исходном тексте. "
        "Не выдумывай факты и не добавляй собственные оценки. "
        "Не повторяй заголовок дословно. "
        "Если данных мало, кратко сообщи только то, что известно. "
        "Не пиши редакторские инструкции и фразы вроде "
        "«в публикации нужно указать».\n\n"
        f"Заголовок: {title}\n\n"
        f"Текст источника: {source_text[:7000]}"
    )

    payload = json.dumps({
        "model": "gpt-4.1-mini",
        "input": prompt,
        "max_output_tokens": 320,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )

        texts = []

        for output in data.get("output", []):
            if output.get("type") != "message":
                continue

            for part in output.get("content", []):
                if (
                    part.get("type") == "output_text"
                    and part.get("text")
                ):
                    texts.append(part["text"])

        summary = clean_text(" ".join(texts))

        if summary:
            return summary[:1200]

    except urllib.error.HTTPError as exc:
        print(
            f"OpenAI API error {exc.code}: "
            f"{exc.read().decode('utf-8', errors='ignore')[:400]}"
        )
    except Exception as exc:
        print(f"OpenAI summary error: {exc}")

    return source_text[:700]


# =========================
# ФОТОГРАФИИ
# =========================

def get_real_photo(article_url, image_urls, rss_images=None):
    if Image is None:
        print("Pillow unavailable; photo sending disabled.")
        return None

    if is_google_host(article_url):
        print("Original article URL unavailable; no Google image will be used.")
        return None

    candidates = list(rss_images or []) + list(image_urls or [])
    seen = set()

    for candidate in candidates[:24]:
        image_url = urllib.parse.urljoin(
            article_url,
            html.unescape(candidate),
        )

        if image_url in seen:
            continue

        seen.add(image_url)

        if (
            not image_url.startswith("https://")
            or is_google_host(image_url)
        ):
            continue

        if any(word in image_url.lower() for word in (
            "favicon", "logo", "avatar", "icon",
            "sprite", "placeholder", "default-image",
        )):
            continue

        try:
            raw, final_url, content_type = request(
                image_url,
                timeout=20,
                headers={"Referer": article_url},
                max_bytes=12_000_000,
            )

            if (
                is_google_host(final_url)
                or not content_type.lower().startswith("image/")
            ):
                continue

            with Image.open(BytesIO(raw)) as image:
                image.verify()

            with Image.open(BytesIO(raw)) as image:
                width, height = image.size

                if width < 400 or height < 250:
                    continue

                if width / max(height, 1) > 3.5:
                    continue

                image = image.convert("RGB")
                image.thumbnail((1600, 1600))

                output = BytesIO()
                image.save(
                    output,
                    format="JPEG",
                    quality=88,
                    optimize=True,
                )

                photo = output.getvalue()

                if len(photo) >= 10_000:
                    return photo

        except Exception as exc:
            print(f"Rejected image {image_url[:120]}: {exc}")

    return None


# =========================
# TELEGRAM
# =========================

def telegram_api(method, fields=None, photo=None):
    if not PUBLISH:
        print(f"[TEST MODE] Telegram {method}: {fields}")
        return {"ok": True, "result": {}}

    url = f"https://api.telegram.org/bot{TOKEN}/{method}"

    if photo is None:
        body = urllib.parse.urlencode(fields or {}).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    else:
        boundary = "----LocalNewsBotBoundary7MA4YWxkTrZu0gW"
        chunks = []

        for key, value in (fields or {}).items():
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                ).encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ])

        chunks.extend([
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="photo"; '
                'filename="news.jpg"\r\n'
            ).encode(),
            b"Content-Type: image/jpeg\r\n\r\n",
            photo,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])

        req = urllib.request.Request(
            url,
            data=b"".join(chunks),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    with urllib.request.urlopen(req, timeout=40) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    return result


def send_text(text):
    return telegram_api("sendMessage", {
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


def escape_html(value):
    return html.escape(str(value or ""), quote=True)


def build_story_caption(item, label, summary, article_url):
    title = escape_html(item["title"])
    safe_url = escape_html(article_url)

    prefix = f"{label}\n\n📰 <b>{title}</b>"
    source_link = f'\n\n<a href="{safe_url}">Источник</a>'
    summary_html = "\n\n" + escape_html(summary) if summary else ""

    caption = prefix + summary_html + source_link

    # Telegram allows up to 1024 characters in a photo caption.
    if len(caption) > 1000:
        available = 1000 - len(prefix) - len(source_link) - 4

        if available > 40 and summary:
            shortened = summary[:available].rsplit(" ", 1)[0]
            caption = (
                prefix
                + "\n\n"
                + escape_html(shortened.rstrip(" .,!?:;"))
                + source_link
            )
        else:
            caption = prefix[:900] + source_link

    return caption


def publish_story(item, label="📰 Новости"):
    article_url = resolve_article(item)

    article_url, article_description, image_urls = fetch_article(
        article_url
    )

    summary = openai_summary(
        item["title"],
        item.get("description", ""),
        article_description or "",
    )

    caption = build_story_caption(
        item,
        label,
        summary,
        article_url,
    )

    photo = get_real_photo(
        article_url,
        image_urls,
        item.get("rss_images", []),
    )

    if photo:
        try:
            telegram_api(
                "sendPhoto",
                {
                    "chat_id": CHANNEL,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                photo=photo,
            )

            print(f"Published with article photo: {item['title']}")
            return

        except Exception as exc:
            print(f"Photo send failed; trying text-only: {exc}")

    # HTML mode makes only the word "Источник" clickable.
    send_text(caption)

    print(f"Published text-only: {item['title']}")


# =========================
# ЗАЩИТА ОТ ДУБЛЕЙ
# =========================

def was_posted(item, state):
    if item["id"] in set(state.get("posted", [])):
        return True

    return same_story(
        item["title"],
        state.get("posted_titles", []),
    )


def mark_posted(item, state):
    posted = state.setdefault("posted", [])

    if item["id"] not in posted:
        posted.append(item["id"])

    titles = state.setdefault("posted_titles", [])

    if not same_story(item["title"], titles):
        titles.append(item["title"])


def choose_story(news, location, state):
    for item in news:
        if item["location"] != location:
            continue

        if not is_local_story(item, location):
            continue

        if was_posted(item, state):
            continue

        return item

    return None


# =========================
# ЕЖЕДНЕВНАЯ ПУБЛИКАЦИЯ
# =========================

def daily_post(state, news):
    today = now_local().date().isoformat()

    if state.get("daily_date") == today:
        print("Daily post already sent today.")
        return

    print("Publishing daily weather and local news.")

    try:
        send_text(get_weather())
    except Exception as exc:
        print(f"Weather error: {exc}")

        send_text(
            "☀️ Всем привет!\n\n"
            "Не удалось загрузить погоду. Попробуем обновить её позже."
        )

    kupino_story = choose_story(news, "kupino", state)

    if kupino_story:
        try:
            publish_story(
                kupino_story,
                "📍 Купино и Купинский район",
            )
            mark_posted(kupino_story, state)
        except Exception as exc:
            print(f"Kupino news error: {exc}")

    novosibirsk_story = choose_story(news, "novosibirsk", state)

    if novosibirsk_story:
        try:
            publish_story(
                novosibirsk_story,
                "📍 Новосибирск",
            )
            mark_posted(novosibirsk_story, state)
        except Exception as exc:
            print(f"Novosibirsk news error: {exc}")

    state["daily_date"] = today
    save_state(state)


# =========================
# ПРОИСШЕСТВИЯ С ЗАДЕРЖКОЙ
# =========================

def retraction_confirmed(item):
    try:
        article_url = resolve_article(item)

        if is_google_host(article_url):
            return False

        raw, final_url, content_type = request(
            article_url,
            timeout=15,
        )

        if (
            is_google_host(final_url)
            or "html" not in content_type.lower()
        ):
            return False

        page = clean_text(
            raw.decode("utf-8", errors="ignore")
        ).lower()[:12000]

        return any(word in page for word in RETRACTION_WORDS)

    except Exception as exc:
        print(f"Could not recheck source: {exc}")
        return False


def process_incidents(state, news):
    pending = state.get("pending", [])
    pending_ids = {item["id"] for item in pending}
    current_time = time.time()

    for item in news:
        if was_posted(item, state):
            continue

        if item["id"] in pending_ids:
            continue

        if not is_local_story(item, item["location"]):
            continue

        if not is_incident(item):
            continue

        item["detected_at"] = current_time

        delay = 900 + (int(item["id"][:4], 16) % 301)
        item["publish_after"] = current_time + delay

        pending.append(item)
        pending_ids.add(item["id"])

        print(f"Queued incident: {item['title']}")

    remaining = []

    for item in pending:
        if was_posted(item, state):
            continue

        if current_time < float(
            item.get("publish_after", current_time + 900)
        ):
            remaining.append(item)
            continue

        try:
            if retraction_confirmed(item):
                print(f"Skipped retracted item: {item['title']}")
                mark_posted(item, state)
                continue

            publish_story(item, "🚨 Новое происшествие")
            mark_posted(item, state)

            print(f"Published incident: {item['title']}")

        except Exception as exc:
            print(f"Incident publication failed: {exc}")
            remaining.append(item)

    state["pending"] = remaining
    save_state(state)


# =========================
# ЗАПУСК
# =========================

def main():
    state = load_state()

    try:
        news = get_all_news()
    except Exception as exc:
        print(f"Could not fetch news: {exc}")
        news = []

    is_manual = (
        os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    )

    if now_local().hour >= 9 or is_manual:
        daily_post(state, news)

    process_incidents(state, news)
    save_state(state)


if __name__ == "__main__":
    main()

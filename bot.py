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
import difflib

from io import BytesIO
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

try:
    from PIL import Image
except ImportError:
    Image = None


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["CHANNEL"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
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
    "информация не подтвердилась",
    "сообщение не подтвердилось",
    "ранее распространенная информация не подтвердилась",
    "новость опровергли",
    "опровергли информацию",
    "официально опровергли",
    "оказалось фейком",
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

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
)

GOOGLE_HOSTS = (
    "news.google.com", "google.com", "www.google.com",
    "gstatic.com", "googleusercontent.com"
)


def now_local():
    return datetime.now(NOVOSIBIRSK_TZ)


def request(url, timeout=20, headers=None, max_bytes=8_000_000):
    headers = headers or {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }
    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Ответ слишком большой")
        return data, response.geturl(), response.headers.get("Content-Type", "")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
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

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
            ratio = difflib.SequenceMatcher(
                None, normalized, old_title
            ).ratio()
            if ratio >= 0.86:
                return True

    return False


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

        source_el = item.find("source")
        source_name = clean_text(source_el.text or "") if source_el is not None else ""

        if not title or not link:
            continue

        results.append({
            "id": story_id(link, title),
            "location": location,
            "title": title,
            "description": description[:1200],
            "link": link,
            "pub_date": pub_date,
            "source_name": source_name,
        })

    return results


def get_all_news():
    found = []

    for location, query in FEEDS:
        found.extend(fetch_feed(location, query))

    # Удаляем дубли из разных поисковых лент.
    unique = []
    for item in found:
        duplicate = False

        for existing in unique:
            if item["location"] != existing["location"]:
                continue
            if same_story(item["title"], [existing["title"]]):
                duplicate = True
                break

        if not duplicate:
            unique.append(item)

    # Сначала свежие материалы, если дату удалось распознать.
    def date_key(item):
        try:
            parsed = datetime.strptime(
                item["pub_date"], "%a, %d %b %Y %H:%M:%S %Z"
            )
            return parsed.timestamp()
        except Exception:
            return 0

    unique.sort(key=date_key, reverse=True)
    return unique


def is_local_story(item, location):
    text = (item["title"] + " " + item["description"]).lower()

    if location == "kupino":
        return "купино" in text or "купинский район" in text

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
    text = (item["title"] + " " + item["description"]).lower()
    return any(word in text for word in INCIDENT_WORDS)


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
        "&current=temperature_2m,apparent_temperature,wind_speed_10m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code"
        "&timezone=Asia%2FNovosibirsk&forecast_days=2"
    )

    raw, _, _ = request(url)
    data = json.loads(raw.decode("utf-8"))

    current = data["current"]
    daily = data["daily"]

    temp = signed(current["temperature_2m"])
    feels = signed(current["apparent_temperature"])
    wind = round(current["wind_speed_10m"])

    today_min = signed(daily["temperature_2m_min"][0])
    today_max = signed(daily["temperature_2m_max"][0])
    tomorrow_min = signed(daily["temperature_2m_min"][1])
    tomorrow_max = signed(daily["temperature_2m_max"][1])

    today_desc = weather_name(daily["weather_code"][0])
    tomorrow_desc = weather_name(daily["weather_code"][1])
    current_desc = weather_name(current["weather_code"])

    return (
        "☀️ Всем привет!\n\n"
        f"🌡 Сейчас в Купино: {temp}°C, ощущается как {feels}°C. "
        f"{current_desc}, ветер {wind} м/с.\n\n"
        "📅 Прогноз погоды\n"
        f"• Сегодня: от {today_min} до {today_max}°C, {today_desc}.\n"
        f"• Завтра: от {tomorrow_min} до {tomorrow_max}°C, "
        f"{tomorrow_desc}.\n\n"
        "Всем хорошего дня и побольше позитива! 👋"
    )


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image_urls = []
        self.canonical = None
        self.title = None
        self.description = None
        self.anchors = []
        self._anchor_href = None
        self._anchor_text = []
        self.paragraphs = []
        self._in_paragraph = False
        self._paragraph_text = []
        self._in_title = False
        self._title_text = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): v for k, v in attrs if k}

        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
            return

        if self._skip_depth:
            return

        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            content = html.unescape((a.get("content") or "").strip())

            if key in ("og:image", "og:image:secure_url", "twitter:image"):
                if content:
                    self.image_urls.append(content)

            elif key in ("og:title", "twitter:title") and content and not self.title:
                self.title = content

            elif key in ("og:description", "description", "twitter:description"):
                if content and not self.description:
                    self.description = content

        elif tag == "link":
            rel = (a.get("rel") or "").lower().split()
            href = a.get("href")
            if href and "canonical" in rel and not self.canonical:
                self.canonical = href

        elif tag == "img":
            for key in ("src", "data-src", "data-original", "data-lazy-src"):
                if a.get(key):
                    self.image_urls.append(a[key])

            srcset = a.get("srcset")
            if srcset:
                candidate = srcset.split(",")[-1].strip().split(" ")[0]
                if candidate:
                    self.image_urls.append(candidate)

        elif tag == "a":
            self._anchor_href = a.get("href")
            self._anchor_text = []

        elif tag == "p":
            self._in_paragraph = True
            self._paragraph_text = []

        elif tag == "title":
            self._in_title = True
            self._title_text = []

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("script", "style", "noscript", "svg") and self._skip_depth:
            self._skip_depth -= 1
            return

        if self._skip_depth:
            return

        if tag == "a" and self._anchor_href:
            self.anchors.append((
                self._anchor_href,
                clean_text(" ".join(self._anchor_text))
            ))
            self._anchor_href = None
            self._anchor_text = []

        elif tag == "p" and self._in_paragraph:
            paragraph = clean_text(" ".join(self._paragraph_text))
            if len(paragraph) > 35:
                self.paragraphs.append(paragraph)
            self._in_paragraph = False
            self._paragraph_text = []

        elif tag == "title":
            value = clean_text(" ".join(self._title_text))
            if value and not self.title:
                self.title = value
            self._in_title = False
            self._title_text = []

    def handle_data(self, data):
        if self._skip_depth:
            return

        if self._anchor_href is not None:
            self._anchor_text.append(data)

        if self._in_paragraph:
            self._paragraph_text.append(data)

        if self._in_title:
            self._title_text.append(data)


def host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_google_host(url):
    host = host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in GOOGLE_HOSTS)


def resolve_article(item):
    """
    Пробуем получить оригинальный URL.
    Если Google News показывает только свою промежуточную страницу,
    ищем внешние ссылки в её HTML. Если оригинал не найден, возвращаем
    исходную ссылку, но не считаем страницу Google источником фотографии.
    """
    url = item["link"]

    try:
        raw, final_url, content_type = request(url, timeout=20)
        if "html" not in content_type.lower() and not raw.lstrip().startswith(b"<"):
            return url, None

        page = raw.decode("utf-8", errors="ignore")
        parser = ArticleParser()
        parser.feed(page)

        if not is_google_host(final_url):
            canonical = urllib.parse.urljoin(final_url, parser.canonical or final_url)
            if not is_google_host(canonical):
                return canonical, parser

        title = normalize_title(item["title"])
        candidates = []

        for href, anchor_text in parser.anchors:
            absolute = urllib.parse.urljoin(final_url, html.unescape(href))
            if not absolute.startswith(("http://", "https://")):
                continue
            if is_google_host(absolute):
                continue
            if any(x in absolute.lower() for x in (
                "accounts.google", "support.google", "policies.google",
                "consent.google", "youtube.com"
            )):
                continue

            anchor = normalize_title(anchor_text)
            score = 0
            if anchor and title:
                score = difflib.SequenceMatcher(
                    None, title, anchor
                ).ratio()
            candidates.append((score, absolute))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1], None

        # Внешний URL не удалось надёжно определить.
        return url, None

    except Exception as exc:
        print(f"Could not resolve article URL: {exc}")
        return url, None


def fetch_article(item):
    """
    Загружает оригинальную статью и извлекает метаданные.
    Не использует изображения с домена Google News.
    """
    article_url, parser = resolve_article(item)

    if parser is None or is_google_host(article_url):
        try:
            raw, final_url, content_type = request(article_url, timeout=20)
            if is_google_host(final_url) or "html" not in content_type.lower():
                return article_url, None, None, []
            parser = ArticleParser()
            parser.feed(raw.decode("utf-8", errors="ignore"))
            article_url = final_url
        except Exception as exc:
            print(f"Article fetch failed: {exc}")
            return article_url, None, None, []

    description = parser.description
    if not description or len(description) < 45:
        description = " ".join(parser.paragraphs[:4])[:2000]

    return article_url, description, parser.title, parser.image_urls


def openai_summary(title, rss_description, article_description):
    """
    Создаёт короткую сводку только по тексту, который реально найден.
    Если API недоступен, используем описание источника без выдуманных фактов.
    """
    source_text = clean_text(article_description or rss_description)

    if not source_text:
        return ""

    if not OPENAI_API_KEY:
        return source_text[:600]

    prompt = (
        "Составь понятную новостную сводку на русском языке в 2–4 предложениях. "
        "Используй только факты из предоставленного заголовка и текста. "
        "Не придумывай даты, адреса, причины, пострадавших или последствия. "
        "Если информации мало, кратко перескажи только то, что известно. "
        "Не добавляй собственные оценки и не повторяй заголовок дословно.\n\n"
        f"Заголовок: {title}\n\n"
        f"Текст источника: {source_text[:5000]}"
    )

    payload = json.dumps({
        "model": "gpt-4.1-mini",
        "input": prompt,
        "max_output_tokens": 220
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
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        texts = []
        for output in data.get("output", []):
            if output.get("type") != "message":
                continue
            for part in output.get("content", []):
                if part.get("type") == "output_text" and part.get("text"):
                    texts.append(part["text"])

        summary = clean_text(" ".join(texts))
        if summary:
            return summary[:900]

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")[:500]
        print(f"OpenAI API error {exc.code}: {details}")
    except Exception as exc:
        print(f"OpenAI summary error: {exc}")

    return source_text[:600]


def get_real_photo(article_url, image_urls):
    """
    Проверяет кандидатов на фотографию.
    Отклоняет Google/иконки, слишком маленькие изображения и невалидные файлы.
    Возвращает нормализованный JPEG либо None.
    """
    if Image is None:
        print("Pillow is not installed; photo sending disabled.")
        return None

    if is_google_host(article_url):
        print("Google News page is not accepted as photo source.")
        return None

    tried = set()

    for candidate in image_urls[:12]:
        image_url = urllib.parse.urljoin(article_url, html.unescape(candidate))
        if image_url in tried:
            continue
        tried.add(image_url)

        if not image_url.startswith("https://"):
            continue
        if is_google_host(image_url):
            continue

        lowered = image_url.lower()
        if any(word in lowered for word in (
            "favicon", "logo", "avatar", "icon", "sprite",
            "placeholder", "default-image"
        )):
            continue

        try:
            raw, final_url, content_type = request(
                image_url,
                timeout=20,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": article_url,
                },
                max_bytes=12_000_000,
            )

            if is_google_host(final_url):
                continue

            if not content_type.lower().startswith("image/"):
                continue

            with Image.open(BytesIO(raw)) as im:
                im.verify()

            with Image.open(BytesIO(raw)) as im:
                width, height = im.size

                # Большинство логотипов и иконок отсекается по размеру.
                if width < 400 or height < 250:
                    continue
                if width / max(height, 1) > 3.5:
                    continue
                if height / max(width, 1) > 2.8:
                    continue

                im = im.convert("RGB")
                im.thumbnail((1600, 1600))

                out = BytesIO()
                im.save(out, format="JPEG", quality=88, optimize=True)
                jpeg = out.getvalue()

                if len(jpeg) < 15_000:
                    continue

                return jpeg

        except Exception as exc:
            print(f"Rejected image {image_url[:150]}: {exc}")

    return None


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
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    else:
        boundary = "----LocalNewsBotBoundary7MA4YWxkTrZu0gW"
        chunks = []

        for key, value in (fields or {}).items():
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ])

        chunks.extend([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="photo"; filename="news.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            photo,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])

        req = urllib.request.Request(
            url,
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    with urllib.request.urlopen(req, timeout=40) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    return result


def send_text(text):
    return telegram_api("sendMessage", {
        "chat_id": CHANNEL,
        "text": text,
        "disable_web_page_preview": "true",
    })


def escape_html(value):
    return html.escape(str(value or ""), quote=True)


def build_story_caption(item, label, summary, article_url):
    title = escape_html(item["title"])
    caption = f"{label}\n\n📰 <b>{title}</b>"

    if summary:
        caption += "\n\n" + escape_html(summary)

    safe_url = escape_html(article_url)
    caption += f'\n\n<a href="{safe_url}">Источник</a>'

    # Telegram photo caption limit is 1024 characters.
    if len(caption) > 1000:
        caption = caption[:970].rsplit(" ", 1)[0] + f'\n\n<a href="{safe_url}">Источник</a>'

    return caption


def publish_story(item, label="📰 Новости"):
    article_url, article_description, article_title, image_urls = fetch_article(item)

    # Берём текст статьи, если он действительно извлечён, иначе RSS.
    summary = openai_summary(
        item["title"],
        item.get("description", ""),
        article_description or "",
    )

    caption = build_story_caption(item, label, summary, article_url)
    photo = get_real_photo(article_url, image_urls)

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
            print("Published with validated photo.")
            return
        except Exception as exc:
            print(f"Photo send failed, sending text: {exc}")

    # Никаких логотипов вместо фотографий.
    send_text(caption.replace("<b>", "").replace("</b>", ""))
    print("Published text-only: no suitable article photo found.")


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


def daily_post(state, news):
    today = now_local().date().isoformat()
    if state.get("daily_date") == today:
        return

    print("Publishing daily weather and news.")

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
            publish_story(kupino_story, "📍 Купино и Купинский район")
            mark_posted(kupino_story, state)
        except Exception as exc:
            print(f"Kupino news error: {exc}")

    novosibirsk_story = choose_story(news, "novosibirsk", state)
    if novosibirsk_story:
        try:
            publish_story(novosibirsk_story, "📍 Новосибирск")
            mark_posted(novosibirsk_story, state)
        except Exception as exc:
            print(f"Novosibirsk news error: {exc}")

    state["daily_date"] = today
    save_state(state)


def retraction_confirmed(item):
    try:
        article_url, _, _, _ = fetch_article(item)
        if is_google_host(article_url):
            return False

        raw, final_url, content_type = request(article_url, timeout=15)
        if is_google_host(final_url) or "html" not in content_type.lower():
            return False

        page = clean_text(raw.decode("utf-8", errors="ignore")).lower()
        title = normalize_title(item["title"])

        # Проверяем только начало статьи, чтобы меньше ловить чужие упоминания.
        start = page[:10000]
        if not any(word in start for word in RETRACTION_WORDS):
            return False

        # Слова об опровержении должны быть рядом с контекстом новости.
        return any(word in start for word in RETRACTION_WORDS)

    except Exception as exc:
        print(f"Could not recheck source: {exc}")
        return False


def process_incidents(state, news):
    pending = state.get("pending", [])
    pending_ids = {item["id"] for item in pending}
    current_time = time.time()

    for item in news:
        if was_posted(item, state) or item["id"] in pending_ids:
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

        if current_time < float(item.get("publish_after", current_time + 900)):
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


def main():
    state = load_state()

    try:
        news = get_all_news()
    except Exception as exc:
        print(f"Could not fetch news: {exc}")
        news = []

    now = now_local()
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    if now.hour >= 9 or is_manual:
        daily_post(state, news)

    process_incidents(state, news)
    save_state(state)


if __name__ == "__main__":
    main()

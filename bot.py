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

# -------------------------
# Configuration
# -------------------------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["CHANNEL"]
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PUBLISH = os.getenv("PUBLISH", "false").lower() == "true"
STATE_FILE = "bot_state.json"
LOCAL_TZ = timezone(timedelta(hours=7))
KUPINO_LAT, KUPINO_LON = 54.366, 77.298
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/130.0 Safari/537.36"
)
GOOGLE_HOSTS = ("news.google.com", "google.com", "gstatic.com", "googleusercontent.com")

FEEDS = [
    ("kupino", '"Купино" новости when:3d'),
    ("kupino", '"Купинский район" новости when:3d'),
    ("novosibirsk", '"Новосибирск" новости when:2d'),
    ("novosibirsk", '"Новосибирск" происшествия when:2d'),
]
INCIDENT_WORDS = (
    "взрыв", "взорвал", "пожар", "горит", "загорел", "дтп", "авари",
    "происшеств", "погиб", "пострадал", "ранен", "обруш", "эвакуац",
    "пропал", "розыск", "перекрыли", "отключение", "отравлен",
    "нападение", "стрельба", "разлив", "утечка газа", "без вести",
)
RETRACTION_WORDS = (
    "информация не подтвердилась", "сообщение не подтвердилось",
    "новость опровергли", "опровергли информацию", "официально опровергли",
    "оказалось фейком", "не соответствует действительности",
)
STOP_WORDS = set("""
и в во на по к ко от до за из у о об обо с со для при про под над без не ни что как это этот эта эти тот та те там здесь уже еще или а но да же ли бы был была были быть будет будут стало стали есть где когда который которая которые кто его ее их им ему она они он мы вы я все весь всего также после перед между через около более менее такой такая такие могут может планирует планируют собирается собираются сообщили сообщил сообщает рассказали рассказал заявили заявил рассказала согласно из-за из-под года год лет рублей рубля рублей городе города области район района купино купинский новосибирск новосибирской области сегодня вчера завтра сейчас местный местные местных сообщили что со слов сообщает пресс-служба пресс-службы источник главная новости редакция комментарии контакты телефон почта реклама вакансии сотрудничество
""".split())


def now_local():
    return datetime.now(LOCAL_TZ)


def request(url, timeout=20, headers=None, max_bytes=8_000_000):
    h = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Ответ слишком большой")
        return data, response.geturl(), response.headers.get("Content-Type", "")


def notify_owner(message):
    """Send a private alert without hiding the original error if Telegram is unavailable."""
    if not OWNER_CHAT_ID:
        print("OWNER_CHAT_ID is not configured; alert: " + message[:500])
        return
    try:
        telegram_api("sendMessage", {
            "chat_id": OWNER_CHAT_ID,
            "text": "⚠️ Блог|Жеки — уведомление\n\n" + message[:3500],
            "disable_web_page_preview": "true",
        })
    except Exception as exc:
        print(f"Could not notify owner: {exc}; original alert: {message[:300]}")


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
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_summary(value):
    """Keep intentional paragraph breaks in AI summaries."""
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", value)]
    return "\n\n".join(part for part in paragraphs if part)


def display_title(item):
    """Remove only a trailing publisher suffix, not meaningful headline text."""
    title = clean_text(item.get("title", ""))
    source = clean_text(item.get("source_name", ""))
    if source:
        title = re.sub(rf"\s+[—–-]\s*{re.escape(source)}\s*$", "", title, flags=re.I)
    return title.strip(" —–-")


def normalize_title(value):
    value = html.unescape(value or "").lower().replace("ё", "е")
    value = re.sub(r"\s*[|—–-]\s*[^|—–-]{2,70}$", "", value)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-zа-я0-9 ]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokens(value):
    return {w for w in normalize_title(value).split() if len(w) >= 4 and w not in STOP_WORDS}


def relevance_score(title, text):
    """Deterministic topic overlap. Used to reject unrelated article text."""
    title_tokens = tokens(title)
    text_tokens = tokens(text)
    if not title_tokens or not text_tokens:
        return 0.0
    matched = 0
    for word in title_tokens:
        if any(t == word or t.startswith(word[:5]) or word.startswith(t[:5]) for t in text_tokens):
            matched += 1
    return matched / max(len(title_tokens), 1)


def titles_match(expected, actual):
    a, b = normalize_title(expected), normalize_title(actual)
    if not a or not b:
        return False
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = tokens(a), tokens(b)
    overlap = len(ta & tb) / max(len(ta), 1) if ta else 0
    return seq >= 0.42 or overlap >= 0.45


def story_id(link, title):
    return hashlib.sha256(((link or "") + "|" + normalize_title(title)).encode()).hexdigest()[:24]


def same_story(title, previous_titles):
    current = normalize_title(title)
    if not current:
        return False
    for old in previous_titles:
        old = normalize_title(old)
        if not old:
            continue
        if current == old:
            return True
        if min(len(current), len(old)) >= 22 and difflib.SequenceMatcher(None, current, old).ratio() >= 0.86:
            return True
    return False


def host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_google_host(url):
    host = host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in GOOGLE_HOSTS)


class ArticleParser(HTMLParser):
    """Extract metadata, article/main paragraphs and images; ignore page chrome."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta_title = None
        self.description = None
        self.canonical = None
        self.image_urls = []
        self.anchors = []
        self.article_paragraphs = []
        self.article_depth = 0
        self.main_depth = 0
        self.skip_depth = 0
        self.in_paragraph = False
        self.paragraph_parts = []
        self.in_title = False
        self.title_parts = []
        self.anchor_href = None
        self.anchor_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {k.lower(): v for k, v in attrs if k}
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "article":
            self.article_depth += 1
        elif tag == "main":
            self.main_depth += 1
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            content = (attrs.get("content") or "").strip()
            if key in ("og:title", "twitter:title") and content and not self.meta_title:
                self.meta_title = html.unescape(content)
            elif key in ("og:description", "description", "twitter:description") and content and not self.description:
                self.description = html.unescape(content)
            elif key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src") and content:
                self.image_urls.append(content)
        elif tag == "link":
            if "canonical" in (attrs.get("rel") or "").lower().split() and attrs.get("href") and not self.canonical:
                self.canonical = attrs["href"]
        elif tag == "img":
            for key in ("src", "data-src", "data-original", "data-lazy-src", "data-lazy"):
                if attrs.get(key):
                    self.image_urls.append(attrs[key])
            if attrs.get("srcset"):
                self.image_urls.append(attrs["srcset"].split(",")[-1].strip().split()[0])
        elif tag == "p" and (self.article_depth or self.main_depth):
            self.in_paragraph = True
            self.paragraph_parts = []
        elif tag == "title":
            self.in_title = True
            self.title_parts = []
        elif tag == "a":
            self.anchor_href = attrs.get("href")
            self.anchor_parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"):
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "article" and self.article_depth:
            self.article_depth -= 1
        elif tag == "main" and self.main_depth:
            self.main_depth -= 1
        elif tag == "p" and self.in_paragraph:
            p = clean_text(" ".join(self.paragraph_parts))
            if len(p) >= 35 and not looks_like_boilerplate(p):
                self.article_paragraphs.append(p)
            self.in_paragraph = False
            self.paragraph_parts = []
        elif tag == "title":
            if not self.meta_title:
                self.meta_title = clean_text(" ".join(self.title_parts))
            self.in_title = False
        elif tag == "a" and self.anchor_href:
            self.anchors.append((self.anchor_href, clean_text(" ".join(self.anchor_parts))))
            self.anchor_href = None
            self.anchor_parts = []

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_paragraph:
            self.paragraph_parts.append(data)
        if self.in_title:
            self.title_parts.append(data)
        if self.anchor_href is not None:
            self.anchor_parts.append(data)


def looks_like_boilerplate(text):
    low = text.lower()
    signals = (
        "главный редактор", "шеф-редактор", "телефон редакции", "электронная почта",
        "вакансии", "реклама", "сотрудничество", "политика конфиденциальности",
        "пользовательское соглашение", "все права защищены", "подписывайтесь на нас",
    )
    return any(s in low for s in signals)


def fetch_feed(location, query):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"
    })
    try:
        raw, _, _ = request(url)
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"RSS error ({location}): {exc}")
        return []
    result = []
    for node in root.findall(".//item"):
        title = clean_text(node.findtext("title", ""))
        link = (node.findtext("link", "") or "").strip()
        rss_description_html = node.findtext("description", "") or ""
        description = clean_text(rss_description_html)
        pub_date = node.findtext("pubDate", "") or ""
        source = node.find("source")
        source_name = clean_text(source.text or "") if source is not None else ""
        source_url = source.attrib.get("url", "").strip() if source is not None else ""
        if not title or not link:
            continue
        parser = ArticleParser()
        try:
            parser.feed(rss_description_html)
        except Exception:
            pass
        result.append({
            "id": story_id(link, title), "location": location, "title": title,
            "description": description[:1800], "link": link, "pub_date": pub_date,
            "source_name": source_name, "source_url": source_url,
            "rss_images": parser.image_urls,
        })
    return result


def get_all_news():
    found = []
    for location, query in FEEDS:
        found.extend(fetch_feed(location, query))
    unique = []
    for item in found:
        if not any(item["location"] == old["location"] and same_story(item["title"], [old["title"]]) for old in unique):
            unique.append(item)
    def date_key(item):
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(item["pub_date"])
            return dt.timestamp()
        except Exception:
            return 0
    return sorted(unique, key=date_key, reverse=True)


def is_local_story(item, location):
    text = (item["title"] + " " + item["description"]).lower().replace("ё", "е")
    if location == "kupino":
        return "купино" in text or "купинский район" in text
    if location == "novosibirsk":
        return "новосибирск" in text or "новосибирской области" in text
    return False


def is_incident(item):
    text = (item["title"] + " " + item["description"]).lower()
    return any(word in text for word in INCIDENT_WORDS)


# -------------------------
# Weather
# -------------------------
def weather_name(code):
    code = int(code)
    if code == 0: return "ясно ☀️"
    if code == 1: return "преимущественно ясно 🌤"
    if code == 2: return "переменная облачность 🌤"
    if code == 3: return "облачно ☁️"
    if code in (45, 48): return "туман 🌫"
    if code in (51, 53, 55, 56, 57): return "морось 🌧"
    if code in (61, 63, 65, 66, 67): return "дождь 🌧"
    if code in (71, 73, 75, 77): return "снег ❄️"
    if code in (80, 81, 82): return "ливень 🌧"
    if code in (85, 86): return "снегопад ❄️"
    if code in (95, 96, 99): return "гроза ⛈"
    return "переменная погода"


def signed(value):
    value = int(round(float(value)))
    return f"+{value}" if value > 0 else str(value)


RU_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
RU_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)


def format_local_date(value):
    return f"{value.day} {RU_MONTHS[value.month - 1]}, {RU_WEEKDAYS[value.weekday()]}"


def weather_plain_name(code):
    """Short weather wording matching the channel's established weather post."""
    code = int(code)
    if code == 0: return "ясно"
    if code == 1: return "преимущественно ясно"
    if code == 2: return "переменная облачность"
    if code == 3: return "пасмурно"
    if code in (45, 48): return "туман"
    if code in (51, 53, 55, 56, 57): return "морось 🌧"
    if code in (61, 63, 65, 66, 67): return "дождь 🌧"
    if code in (71, 73, 75, 77): return "снег ❄️"
    if code in (80, 81, 82): return "ливень 🌧"
    if code in (85, 86): return "снегопад ❄️"
    if code in (95, 96, 99): return "гроза ⛈"
    return "переменная погода"


def find_hour_index(hourly_times, wanted_prefix):
    for i, value in enumerate(hourly_times):
        if value.startswith(wanted_prefix):
            return i
    return None


def get_weather():
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={KUPINO_LAT}&longitude={KUPINO_LON}"
        "&current=temperature_2m,apparent_temperature,wind_speed_10m,weather_code,relative_humidity_2m,surface_pressure"
        "&hourly=temperature_2m,weather_code,precipitation_probability"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,sunrise,sunset"
        "&timezone=Asia%2FNovosibirsk&forecast_days=3"
    )
    raw, _, _ = request(url)
    data = json.loads(raw.decode("utf-8"))
    current, daily, hourly = data["current"], data["daily"], data["hourly"]

    today = now_local()
    today_text = format_local_date(today)
    today_iso = today.date().isoformat()
    tomorrow_iso = (today.date() + timedelta(days=1)).isoformat()
    night_index = find_hour_index(hourly.get("time", []), tomorrow_iso + "T03:00")
    night_temp = daily["temperature_2m_min"][1]
    night_code = daily["weather_code"][1]
    if night_index is not None:
        if night_index < len(hourly.get("temperature_2m", [])):
            night_temp = hourly["temperature_2m"][night_index]
        if night_index < len(hourly.get("weather_code", [])):
            night_code = hourly["weather_code"][night_index]

    pressure_mmhg = round(float(current["surface_pressure"]) * 0.750061683)
    sunrise = str(daily["sunrise"][0]).split("T")[-1][:5]
    sunset = str(daily["sunset"][0]).split("T")[-1][:5]
    probability = daily.get("precipitation_probability_max", [None])[0]
    probability_text = f"{int(probability)}%" if probability is not None else "нет данных"
    humidity = current.get("relative_humidity_2m")
    humidity_text = f"{round(float(humidity))}%" if humidity is not None else "нет данных"

    current_condition = weather_plain_name(current["weather_code"])
    current_condition = current_condition[:1].upper() + current_condition[1:]
    day_condition = weather_plain_name(daily["weather_code"][0])
    night_condition = weather_plain_name(night_code)
    return (
        "Доброе утро, Купино!\n"
        f"Сегодня, {today_text}\n\n"
        "☀️ Погода сейчас\n"
        f"Температура: {signed(current['temperature_2m'])}°C (ощущается как {signed(current['apparent_temperature'])}°C)\n"
        f"{current_condition}, ветер {round(current['wind_speed_10m'])} м/с.\n\n"
        "📊 Детали на день:\n"
        f"• Днём: от {signed(daily['temperature_2m_min'][0])}°C до {signed(daily['temperature_2m_max'][0])}°C, {day_condition}\n"
        f"• Ночью: до {signed(night_temp)}°C, {night_condition}\n"
        f"• Вероятность осадков: {probability_text}\n"
        f"• Влажность: {humidity_text} | Давление: {pressure_mmhg} мм рт. ст.\n"
        f"• 🌅 Восход: {sunrise} | 🌇 Закат: {sunset}"
    )


# -------------------------
# Resolve and validate article
# -------------------------
def resolve_article(item):
    """Resolve Google News redirect. Never substitute a publisher homepage for an article."""
    original = item["link"]
    try:
        _, final_url, content_type = request(original, timeout=20)
        if final_url.startswith("https://") and not is_google_host(final_url):
            return final_url
        # Sometimes Google News serves a landing page with a direct title link.
        if "html" in content_type.lower():
            raw, _, _ = request(final_url, timeout=15)
            parser = ArticleParser()
            parser.feed(raw.decode("utf-8", errors="ignore"))
            candidates = []
            for href, anchor_text in parser.anchors:
                absolute = urllib.parse.urljoin(final_url, html.unescape(href))
                if absolute.startswith("https://") and not is_google_host(absolute):
                    score = difflib.SequenceMatcher(None, normalize_title(item["title"]), normalize_title(anchor_text)).ratio()
                    if score >= 0.60:
                        candidates.append((score, absolute))
            if candidates:
                return sorted(candidates, reverse=True)[0][1]
    except Exception as exc:
        print(f"Article URL resolution failed: {exc}")
    # Keep the Google News item link rather than falsely linking the publisher's homepage.
    return original


def fetch_article(item, article_url):
    if is_google_host(article_url):
        return {"valid": False, "url": article_url, "text": "", "images": [], "reason": "Не удалось подтвердить прямую ссылку на статью"}
    try:
        raw, final_url, content_type = request(article_url, timeout=20)
        if is_google_host(final_url) or "html" not in content_type.lower():
            return {"valid": False, "url": article_url, "text": "", "images": [], "reason": "Страница не является доступной статьёй"}
        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="ignore"))
        page_title = parser.meta_title or ""
        if not titles_match(item["title"], page_title):
            return {"valid": False, "url": final_url, "text": "", "images": [], "reason": f"Заголовок страницы не совпал с RSS: {page_title[:180]}"}
        canonical = urllib.parse.urljoin(final_url, parser.canonical or final_url)
        if canonical.startswith("https://") and not is_google_host(canonical):
            final_url = canonical
        paragraphs = parser.article_paragraphs
        article_text = "\n".join(paragraphs)
        # If there is no article/main content, only a relevant meta description is allowed.
        if len(article_text) < 180 and parser.description and relevance_score(item["title"], parser.description) >= 0.25:
            article_text = parser.description
        if len(article_text) < 80 or relevance_score(item["title"], article_text) < 0.25:
            return {"valid": False, "url": final_url, "text": "", "images": [], "reason": "Текст статьи не прошёл проверку соответствия заголовку"}
        return {"valid": True, "url": final_url, "text": article_text[:12000], "images": parser.image_urls, "reason": ""}
    except Exception as exc:
        return {"valid": False, "url": article_url, "text": "", "images": [], "reason": f"Ошибка загрузки статьи: {exc}"}


# -------------------------
# AI summary: OpenAI first, Gemini fallback
# -------------------------
def parse_ai_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def summary_prompt(title, source_text):
    return (
        "Ты редактор регионального новостного Telegram-канала. Пиши естественно, по-человечески и ясно. "
        "Работай ТОЛЬКО с приведённым текстом. Не добавляй внешние знания, догадки, причины, числа, имена или обстоятельства. "
        "Сначала оцени, относится ли текст к заголовку. Если нет — relevant=false и summary пустая строка. "
        "Если относится — сделай подробный, но компактный пересказ в 3–5 содержательных предложениях без повторения заголовка. "
        "Раздели текст на 2 абзаца пустой строкой, если в источнике хватает фактов. "
        "Сохраняй точное время/дату события, число погибших и пострадавших, место, последствия и статус информации только если они прямо указаны. "
        "Не путай время публикации со временем события. Не включай меню сайта, контакты, рекламу и подписи редакции. "
        "Верни ТОЛЬКО JSON вида {\"relevant\": true, \"summary\": \"...\"}.\n\n"
        f"Заголовок новости: {title}\n\nТекст проверенной статьи:\n{source_text[:9000]}"
    )


def call_openai(prompt):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан")
    payload = json.dumps({"model": "gpt-4.1-mini", "input": prompt, "max_output_tokens": 500}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=35) as response:
        data = json.loads(response.read().decode("utf-8"))
    texts = []
    for output in data.get("output", []):
        if output.get("type") == "message":
            for part in output.get("content", []):
                if part.get("type") == "output_text" and part.get("text"):
                    texts.append(part["text"])
    result = parse_ai_json("\n".join(texts))
    if not result:
        raise RuntimeError("OpenAI вернул ответ не в JSON")
    return result


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан")
    # Current official Gemini Interactions REST API; stateless request.
    payload = json.dumps({"model": "gemini-3.8-flash", "input": prompt, "store": False}).encode()
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=payload,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=35) as response:
        data = json.loads(response.read().decode("utf-8"))
    texts = []
    for step in data.get("steps", []):
        if step.get("type") == "model_output":
            for part in step.get("content", []):
                if part.get("type") == "text" and part.get("text"):
                    texts.append(part["text"])
    result = parse_ai_json("\n".join(texts))
    if not result:
        raise RuntimeError("Gemini вернул ответ не в JSON")
    return result


def make_summary(item, article):
    source_text = article.get("text", "")
    if not article.get("valid") or not source_text:
        return "", False
    prompt = summary_prompt(display_title(item), source_text)
    errors = []
    for provider, func in (("OpenAI", call_openai), ("Gemini", call_gemini)):
        try:
            result = func(prompt)
            if result.get("relevant") is not True:
                raise RuntimeError("ИИ определил, что текст не соответствует заголовку")
            summary = clean_summary(result.get("summary", ""))
            if len(summary) < 35:
                raise RuntimeError("Слишком короткий пересказ")
            # Reject obvious boilerplate or a summary that drifts from the article topic.
            if looks_like_boilerplate(summary) or relevance_score(item["title"], source_text) < 0.25:
                raise RuntimeError("Пересказ не прошёл проверку")
            print(f"Summary provider: {provider}")
            return summary[:1200], True
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            print(errors[-1])
    # Both models are down: use only the already-validated source text, not a fabricated summary.
    notify_owner("OpenAI и Gemini не смогли сделать пересказ для новости:\n" + item["title"] + "\n" + "\n".join(errors))
    # If both AI providers are unavailable, do not dump raw page text into the channel.
    # The verified headline and source link can still be published without a summary.
    return "", True


# -------------------------
# Photo and Telegram
# -------------------------
def get_real_photo(article_url, image_urls):
    if Image is None or is_google_host(article_url):
        return None
    seen = set()
    for candidate in image_urls[:20]:
        url = urllib.parse.urljoin(article_url, html.unescape(candidate))
        if url in seen:
            continue
        seen.add(url)
        if not url.startswith("https://") or is_google_host(url):
            continue
        if any(word in url.lower() for word in ("favicon", "logo", "avatar", "icon", "sprite", "placeholder", "default-image")):
            continue
        try:
            raw, final_url, content_type = request(url, timeout=20, headers={"Referer": article_url}, max_bytes=12_000_000)
            if is_google_host(final_url) or not content_type.lower().startswith("image/"):
                continue
            with Image.open(BytesIO(raw)) as im:
                im.verify()
            with Image.open(BytesIO(raw)) as im:
                if im.width < 400 or im.height < 250 or im.width / max(im.height, 1) > 3.5:
                    continue
                im = im.convert("RGB")
                im.thumbnail((1600, 1600))
                out = BytesIO()
                im.save(out, format="JPEG", quality=88, optimize=True)
                if len(out.getvalue()) >= 10_000:
                    return out.getvalue()
        except Exception as exc:
            print(f"Image rejected: {exc}")
    return None


def telegram_api(method, fields=None, photo=None):
    if not PUBLISH:
        print(f"[TEST MODE] {method}: {fields}")
        return {"ok": True, "result": {}}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if photo is None:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields or {}).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        boundary = "----BlogZhekiBoundary7MA4YWxkTrZu0gW"
        chunks = []
        for key, value in (fields or {}).items():
            chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), str(value).encode(), b"\r\n"])
        chunks.extend([f"--{boundary}\r\n".encode(), b'Content-Disposition: form-data; name="photo"; filename="news.jpg"\r\n', b"Content-Type: image/jpeg\r\n\r\n", photo, b"\r\n", f"--{boundary}--\r\n".encode()])
        req = urllib.request.Request(url, data=b"".join(chunks), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=40) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def send_text(text, chat_id=None):
    fields = {"chat_id": chat_id or CHANNEL, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    return telegram_api("sendMessage", fields)


def esc(text):
    return html.escape(str(text or ""), quote=True)


def build_caption(item, label, summary, url, max_length=4096):
    title = esc(display_title(item))
    link = esc(url)
    prefix = f"{label}\n\n📰 <b>{title}</b>"
    summary_part = "\n\n" + esc(summary) if summary else ""
    source = f'\n\n🔗 Источник: <a href="{link}">оригинальная статья</a>'
    caption = prefix + summary_part + source
    if len(caption) > max_length:
        room = max(0, max_length - len(prefix) - len(source) - 6)
        shortened = summary[:room].rsplit(" ", 1)[0] if room > 40 else ""
        caption = prefix + (("\n\n" + esc(shortened)) if shortened else "") + source
    return caption


def publish_story(item, label="📍 Новости"):
    url = resolve_article(item)
    article = fetch_article(item, url)
    if not article.get("valid"):
        reason = article.get("reason", "неизвестно")
        notify_owner(
            "Новость пропущена: не удалось подтвердить соответствие заголовка и текста статьи.\n"
            f"Заголовок: {display_title(item)}\nПричина: {reason}\nСсылка: {item.get('link', url)}"
        )
        print(f"Skipped unverified article: {display_title(item)} — {reason}")
        return False

    url = article["url"]
    summary, _ = make_summary(item, article)
    images = article.get("images", []) + item.get("rss_images", [])
    photo = get_real_photo(url, images)
    # Telegram photo captions are limited to 1024 characters; text messages allow up to 4096.
    caption = build_caption(item, label, summary, url, max_length=1000 if photo else 4000)
    if photo:
        try:
            telegram_api("sendPhoto", {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"}, photo=photo)
            print("Published with article image:", display_title(item))
            return True
        except Exception as exc:
            print(f"Photo send failed, text fallback: {exc}")
            notify_owner(f"Не удалось отправить фото для новости: {display_title(item)}\nОшибка: {exc}")
    send_text(caption)
    print("Published text-only:", display_title(item))
    return True


# -------------------------
# Deduplication and schedule
# -------------------------
def was_posted(item, state):
    return item["id"] in set(state.get("posted", [])) or same_story(item["title"], state.get("posted_titles", []))


def mark_posted(item, state):
    state.setdefault("posted", []).append(item["id"])
    state.setdefault("posted_titles", []).append(item["title"])


def choose_story(news, location, state):
    for item in news:
        if item["location"] == location and is_local_story(item, location) and not was_posted(item, state):
            return item
    return None


def daily_post(state, news):
    today = now_local().date().isoformat()
    if state.get("daily_date") == today:
        return
    try:
        send_text(get_weather())
    except Exception as exc:
        print(f"Weather error: {exc}")
        notify_owner(f"Не удалось получить погоду в Купино: {exc}")
    for location, label in (("kupino", "📍 Купино и Купинский район"), ("novosibirsk", "📍 Новосибирск")):
        item = choose_story(news, location, state)
        if item:
            try:
                published = publish_story(item, label)
                if PUBLISH and published:
                    mark_posted(item, state)
            except Exception as exc:
                print(f"Daily news error: {exc}")
                notify_owner(f"Ошибка публикации новости:\n{item['title']}\n{exc}")
    if PUBLISH:
        state["daily_date"] = today
    save_state(state)


def retraction_confirmed(item):
    url = resolve_article(item)
    if is_google_host(url):
        return False
    try:
        raw, final_url, content_type = request(url, timeout=15)
        if is_google_host(final_url) or "html" not in content_type.lower():
            return False
        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="ignore"))
        text = " ".join(parser.article_paragraphs).lower()
        return any(term in text for term in RETRACTION_WORDS)
    except Exception as exc:
        print(f"Retraction check failed: {exc}")
        return False


def process_incidents(state, news):
    pending = state.get("pending", [])
    pending_ids = {x["id"] for x in pending}
    now = time.time()
    for item in news:
        if was_posted(item, state) or item["id"] in pending_ids:
            continue
        if is_local_story(item, item["location"]) and is_incident(item):
            item["detected_at"] = now
            item["publish_after"] = now + 900 + (int(item["id"][:4], 16) % 301)
            pending.append(item)
            pending_ids.add(item["id"])
    remaining = []
    for item in pending:
        if was_posted(item, state):
            continue
        if now < float(item.get("publish_after", now + 900)):
            remaining.append(item)
            continue
        try:
            if retraction_confirmed(item):
                mark_posted(item, state)
                notify_owner("Происшествие не опубликовано: источник содержит опровержение.\n" + item["title"])
                continue
            published = publish_story(item, "🚨 Происшествие")
            if PUBLISH and published:
                mark_posted(item, state)
        except Exception as exc:
            print(f"Incident publication failed: {exc}")
            notify_owner(f"Не удалось опубликовать происшествие:\n{item['title']}\n{exc}")
            remaining.append(item)
    state["pending"] = remaining
    save_state(state)


def main():
    state = load_state()
    try:
        news = get_all_news()
    except Exception as exc:
        news = []
        print(f"Could not fetch news: {exc}")
        notify_owner(f"Не удалось загрузить новостные ленты: {exc}")
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if now_local().hour >= 9 or manual:
        daily_post(state, news)
    process_incidents(state, news)
    save_state(state)


if __name__ == "__main__":
    main()

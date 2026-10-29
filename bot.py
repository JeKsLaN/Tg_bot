import os
import re
import json
import time
import html
import hashlib
import random
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
# Как часто писать «новых новостей нет» (в часах). 0 = не писать вообще.
try:
    NO_NEWS_NOTICE_HOURS = float(os.getenv("NO_NEWS_NOTICE_HOURS", "3") or 3)
except ValueError:
    NO_NEWS_NOTICE_HOURS = 3.0
# Сколько новостей страны/мира публиковать в сутки (можно поменять переменной MAX_NATIONAL_PER_DAY).
try:
    MAX_NATIONAL_PER_DAY = int(os.getenv("MAX_NATIONAL_PER_DAY", "3") or 3)
except ValueError:
    MAX_NATIONAL_PER_DAY = 3
STATE_FILE = "bot_state.json"
SOURCE_STATS = {}  # сколько новостей отдал каждый источник в этом запуске
LAST_RESOLVE_ERROR = ""  # почему не открылась последняя статья
LOCAL_TZ = timezone(timedelta(hours=7))
KUPINO_LAT, KUPINO_LON = 54.366, 77.298
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/130.0 Safari/537.36"
)
GOOGLE_HOSTS = ("news.google.com", "google.com", "gstatic.com", "googleusercontent.com")

# Only direct local/official publishers. RSS is tried first; if unavailable,
# the bot reads article links from the publisher's own news page.
SOURCES = [
    {
        "location": "kupino",
        "name": "Городская газета Купино",
        "page": "https://kupino-gazeta.ru/",
        "feeds": ["https://kupino-gazeta.ru/feed/"],
        "domains": ["kupino-gazeta.ru"],
    },
    {
        "location": "kupino",
        "name": "Маяк Кулунды",
        "page": "https://mkkupino.ru/",
        "feeds": ["https://mkkupino.ru/feed/"],
        "domains": ["mkkupino.ru"],
    },
    {
        "location": "novosibirsk",
        "name": "Новосибирские новости",
        "page": "https://nsknews.info/",
        "feeds": ["https://nsknews.info/rss/", "https://nsknews.info/feed/"],
        "domains": ["nsknews.info"],
    },
    {
        "location": "russia",
        "name": "Интерфакс",
        "page": "https://www.interfax.ru/news/",
        "feeds": ["https://www.interfax.ru/rss.asp"],
        "domains": ["interfax.ru"],
    },
]
# Источники, которые уже были в боте раньше: их текущие публикации считаются «уже виденными».
LEGACY_SOURCES = ["Городская газета Купино", "Маяк Кулунды", "Новосибирские новости", "Официальный сайт Новосибирска"]

# Для новостей Новосибирска берём только происшествия и всё, что связано с Купино/областью.
LOCAL_KEYWORDS = ("купин", "новосибирской области", "новосибирская область")
# Для новостей страны и мира берём только важные «институциональные» темы (законы, запреты, экономика, связь).
NATIONAL_KEYWORDS = (
    "госдум", "совет федерации", "правительств", "закон", "запрет", "запрещ", "ограничен",
    "роскомнадзор", "блокиров", "мессенджер", "телеграм", "telegram", "whatsapp", "ватсап",
    "интернет", "мобильн", "связь", "налог", "тариф", "пенси", "ключев", "инфляц",
    "курс доллара", "курс евро", "бензин", "минфин", "центробанк", "банк россии",
)
# Заголовки с этими словами бот не публикует.
BLOCKED_TITLE_WORDS = ("афиша",)
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


def fmt_time(ts):
    """Время по Купино в формате ЧЧ:ММ."""
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%H:%M")


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


def notify_owner(message, icon="⚠️"):
    """Личное уведомление владельцу. Работает и в тестовом режиме (force=True)."""
    if not OWNER_CHAT_ID:
        print("OWNER_CHAT_ID is not configured; alert: " + message[:500])
        return
    try:
        telegram_api("sendMessage", {
            "chat_id": OWNER_CHAT_ID,
            "text": f"{icon} Блог|Жеки — уведомление\n\n" + message[:3500],
            "disable_web_page_preview": "true",
        }, force=True)
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
    state.setdefault("seen", [])
    state.setdefault("seen_titles", [])
    state.setdefault("source_baseline_done", False)
    state.setdefault("last_no_news_notice", 0)
    state.setdefault("last_empty_notice", 0)
    state.setdefault("last_dead_notice", 0)
    state.setdefault("national_day", "")
    state.setdefault("national_count", 0)
    if "baselined_sources" not in state:
        # старое состояние: считаем уже известные источники «прочитанными»
        state["baselined_sources"] = list(LEGACY_SOURCES) if state.get("source_baseline_done") else []
    return state


def save_state(state):
    state["posted"] = state.get("posted", [])[-1500:]
    state["posted_titles"] = state.get("posted_titles", [])[-1000:]
    state["pending"] = state.get("pending", [])[-200:]
    state["seen"] = state.get("seen", [])[-2500:]
    state["seen_titles"] = state.get("seen_titles", [])[-1800:]
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


def allowed_source_url(url, domains):
    host = host_of(url)
    return bool(host) and any(host == domain or host.endswith("." + domain) for domain in domains)


def make_news_item(source, title, link, description="", pub_date="", image_urls=None):
    title = clean_text(title)
    link = urllib.parse.urljoin(source["page"], html.unescape((link or "").strip()))
    if not title or len(title) < 18 or not link.startswith("https://"):
        return None
    if not allowed_source_url(link, source["domains"]):
        return None
    path = urllib.parse.urlparse(link).path.rstrip("/")
    page_path = urllib.parse.urlparse(source["page"]).path.rstrip("/")
    if path == page_path or path in ("", "/"):
        return None
    blocked = ("/category/", "/tag/", "/author/", "/page/", "/wp-content/", "/wp-json/", "/search/", "/feed/")
    if any(part in path.lower() for part in blocked):
        return None
    low_title = title.lower()
    if low_title in {"новости", "все новости", "читать далее", "подробнее", "далее", "главная"}:
        return None
    return {
        "id": story_id(link, title),
        "location": source["location"],
        "title": title,
        "description": clean_text(description)[:1800],
        "link": link,
        "pub_date": pub_date or "",
        "source_name": source["name"],
        "source_url": source["page"],
        "allowed_domains": source["domains"],
        "rss_images": image_urls or [],
    }


def parse_feed_items(raw, source):
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    nodes = root.findall(".//item")
    # Atom feed support
    if not nodes:
        nodes = root.findall(".//{*}entry")
    results = []
    for node in nodes[:12]:
        title = node.findtext("title", default="") or node.findtext("{*}title", default="")
        link = node.findtext("link", default="") or node.findtext("{*}link", default="")
        if not link:
            link_node = node.find("link") or node.find("{*}link")
            if link_node is not None:
                link = link_node.attrib.get("href", "")
        description = (node.findtext("description", default="") or node.findtext("{*}summary", default="") or node.findtext("{*}content", default=""))
        pub_date = (node.findtext("pubDate", default="") or node.findtext("{*}published", default="") or node.findtext("{*}updated", default=""))
        item = make_news_item(source, title, link, description, pub_date)
        if item:
            results.append(item)
    return results


def fetch_source(source):
    # Try the publisher's own RSS/Atom endpoints, never an aggregator.
    for feed_url in source["feeds"]:
        try:
            raw, final_url, content_type = request(feed_url, timeout=15)
            items = parse_feed_items(raw, source)
            if items:
                print(f"Loaded {len(items)} items from {feed_url}")
                return items
            print(f"No usable RSS items at {feed_url} ({content_type})")
        except Exception as exc:
            print(f"Feed unavailable {feed_url}: {exc}")

    # Fallback: read links directly from the publisher's own news page.
    try:
        raw, final_url, content_type = request(source["page"], timeout=20)
        if not allowed_source_url(final_url, source["domains"]) or "html" not in content_type.lower():
            raise RuntimeError("source page did not return HTML from the expected publisher")
        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="ignore"))
        items = []
        seen_links = set()
        for href, anchor_text in parser.anchors:
            item = make_news_item(source, anchor_text, urllib.parse.urljoin(final_url, html.unescape(href)))
            if not item or item["link"] in seen_links:
                continue
            seen_links.add(item["link"])
            items.append(item)
            if len(items) >= 12:
                break
        print(f"Loaded {len(items)} direct page links from {source['page']}")
        return items
    except Exception as exc:
        print(f"Publisher page unavailable {source['page']}: {exc}")
        return []


def get_all_news():
    found = []
    for source in SOURCES:
        items = fetch_source(source)
        SOURCE_STATS[source["name"]] = len(items)
        found.extend(items)
    unique = []
    for item in found:
        if not any(item["location"] == old["location"] and same_story(item["title"], [old["title"]]) for old in unique):
            unique.append(item)
    def date_key(item):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(item["pub_date"]).timestamp()
        except Exception:
            try:
                return datetime.fromisoformat(item["pub_date"].replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0
    return sorted(unique, key=date_key, reverse=True)

def is_local_story(item, location):
    # The publisher domain is the locality/official-source signal; don't reject
    # legitimate local articles merely because their headline omits the city name.
    if item.get("location") != location:
        return False
    domains = item.get("allowed_domains", [])
    return bool(domains) and allowed_source_url(item.get("link", ""), domains)

def passes_topic_filter(item):
    """Что вообще годится для канала. Купино — всё; Новосибирск — происшествия и область; страна — важные темы."""
    text = (item.get("title", "") + " " + item.get("description", "")).lower().replace("ё", "е")
    location = item.get("location")
    if location == "kupino":
        return True
    if location == "novosibirsk":
        return is_incident(item) or any(k in text for k in LOCAL_KEYWORDS) or bool(re.search(r"\bнсо\b", text))
    if location == "russia":
        return any(k in text for k in NATIONAL_KEYWORDS)
    return True


def is_incident(item):
    text = (item["title"] + " " + item["description"]).lower()
    return any(word in text for word in INCIDENT_WORDS)


def retraction_confirmed(item):
    """True, если в статье есть явное опровержение. При ошибке сети публикацию не блокирует."""
    try:
        raw, _, _ = request(item.get("link", ""), timeout=20)
        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="ignore"))
        text = " ".join([
            item.get("title", ""),
            parser.meta_title or "",
            parser.description or "",
            " ".join(parser.article_paragraphs),
        ]).lower().replace("ё", "е")
        return any(word in text for word in RETRACTION_WORDS)
    except Exception as exc:
        print(f"Retraction check skipped: {exc}")
        return False


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
    """Accept only the original article on the configured publisher domain."""
    global LAST_RESOLVE_ERROR
    LAST_RESOLVE_ERROR = ""
    original = item.get("link", "").strip()
    domains = item.get("allowed_domains", [])
    if not original.startswith("https://") or not allowed_source_url(original, domains):
        LAST_RESOLVE_ERROR = "ссылка не https или ведёт не на сайт источника"
        return ""
    try:
        _, final_url, content_type = request(original, timeout=20)
        if not allowed_source_url(final_url, domains):
            LAST_RESOLVE_ERROR = f"сайт перенаправил на другой адрес: {host_of(final_url)}"
            return ""
        if "html" not in content_type.lower():
            LAST_RESOLVE_ERROR = f"сайт вернул не страницу статьи ({content_type[:30]})"
            return ""
        return final_url
    except Exception as exc:
        LAST_RESOLVE_ERROR = f"{type(exc).__name__}: {exc}"[:160]
        print(f"Article URL check failed: {LAST_RESOLVE_ERROR}")
        return ""

def fetch_article(item, article_url):
    domains = item.get("allowed_domains", [])
    if not article_url or not allowed_source_url(article_url, domains):
        return {"valid": False, "url": article_url, "text": "", "images": [], "reason": "Не удалось подтвердить прямую ссылку на статью из разрешённого источника"}
    try:
        raw, final_url, content_type = request(article_url, timeout=20)
        if not allowed_source_url(final_url, domains) or "html" not in content_type.lower():
            return {"valid": False, "url": article_url, "text": "", "images": [], "reason": "Страница не является статьёй на исходном сайте"}
        parser = ArticleParser()
        parser.feed(raw.decode("utf-8", errors="ignore"))
        page_title = parser.meta_title or ""
        if not titles_match(item["title"], page_title):
            return {"valid": False, "url": final_url, "text": "", "images": [], "reason": f"Заголовок страницы не совпал с RSS: {page_title[:180]}"}
        canonical = urllib.parse.urljoin(final_url, parser.canonical or final_url)
        if canonical.startswith("https://") and allowed_source_url(canonical, domains):
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
        "Пиши нейтрально: без оценок, эмоций и своих выводов, только факты из текста. Не путай время публикации со временем события. Не включай меню сайта, контакты, рекламу и подписи редакции. "
        "Верни ТОЛЬКО JSON вида {\"relevant\": true, \"summary\": \"...\"}.\n\n"
        f"Заголовок новости: {title}\n\nТекст проверенной статьи:\n{source_text[:9000]}"
    )


def call_openai(prompt):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан")
    # 900 токенов: для русского текста 500 может обрезать JSON на середине.
    payload = json.dumps({"model": "gpt-4.1-mini", "input": prompt, "max_output_tokens": 900}).encode()
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
BAD_IMAGE_WORDS = ("favicon", "logo", "avatar", "icon", "sprite", "placeholder", "default-image")


def get_real_photo(article_url, image_urls, reasons=None):
    """Скачивает и проверяет картинку статьи. Причины отказа складывает в reasons."""
    reasons = reasons if reasons is not None else []
    if Image is None:
        reasons.append("не установлена библиотека Pillow")
        return None
    if is_google_host(article_url):
        reasons.append("ссылка статьи ведёт на Google")
        return None
    if not image_urls:
        reasons.append("на странице не найдено картинок")
        return None
    seen = set()
    for candidate in image_urls[:20]:
        url = urllib.parse.urljoin(article_url, html.unescape(candidate))
        if url in seen:
            continue
        seen.add(url)
        if not url.startswith("https://") or is_google_host(url):
            reasons.append("не https: " + url[:70])
            continue
        if any(word in url.lower() for word in BAD_IMAGE_WORDS):
            reasons.append("похоже на логотип/иконку: " + url[:70])
            continue
        try:
            raw, final_url, content_type = request(url, timeout=20, headers={"Referer": article_url}, max_bytes=12_000_000)
            if is_google_host(final_url) or not content_type.lower().startswith("image/"):
                reasons.append(f"это не картинка ({content_type[:25]}): " + url[:60])
                continue
            with Image.open(BytesIO(raw)) as im:
                im.verify()
            with Image.open(BytesIO(raw)) as im:
                if im.width < 400 or im.height < 250 or im.width / max(im.height, 1) > 3.5:
                    reasons.append(f"неподходящий размер {im.width}x{im.height}: " + url[:50])
                    continue
                im = im.convert("RGB")
                im.thumbnail((1600, 1600))
                out = BytesIO()
                im.save(out, format="JPEG", quality=88, optimize=True)
                if len(out.getvalue()) >= 10_000:
                    return out.getvalue()
                reasons.append("слишком лёгкая картинка: " + url[:60])
        except Exception as exc:
            print(f"Image rejected: {exc}")
            reasons.append(f"ошибка {type(exc).__name__}: " + url[:60])
    return None


def get_photo_url(article_url, image_urls):
    """Запасной вариант без Pillow: отдаём Telegram прямую ссылку, он скачает картинку сам."""
    for candidate in image_urls[:20]:
        url = urllib.parse.urljoin(article_url, html.unescape(candidate))
        if not url.startswith("https://") or is_google_host(url):
            continue
        if any(word in url.lower() for word in BAD_IMAGE_WORDS):
            continue
        return url
    return None


def telegram_api(method, fields=None, photo=None, force=False):
    # force=True отправляет сообщение даже в тестовом режиме (нужно для уведомлений владельцу).
    if not PUBLISH and not force:
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


def publish_fallback(item, label, reason):
    """Статья не открылась: публикуем короткую версию (заголовок + описание из ленты + ссылка)."""
    link = item.get("link", "")
    if not link.startswith("https://") or not allowed_source_url(link, item.get("allowed_domains", [])):
        notify_owner("Новость пропущена: ссылка не ведёт на сайт источника.\n" + display_title(item))
        return False
    description = clean_summary(item.get("description", ""))[:500]
    if len(description) < 20:
        description = ""
    send_text(build_caption(item, label, description, link, max_length=4000))
    print("Published short version:", display_title(item))
    notify_owner(
        f"Статья не открылась ({reason}), вышла короткая версия: заголовок и ссылка.\n{display_title(item)}",
        icon="ℹ️",
    )
    return True


def publish_story(item, label="📍 Новости"):
    url = resolve_article(item)
    article = fetch_article(item, url)
    if not article.get("valid"):
        reason = article.get("reason", "неизвестно")
        print(f"Article not verified, publishing short version: {display_title(item)} — {reason}")
        return publish_fallback(item, label, reason)

    url = article["url"]
    summary, summary_ok = make_summary(item, article)
    if not summary_ok or len(clean_text(summary)) < 80:
        return publish_fallback(item, label, "не удалось сделать пересказ")
    images = article.get("images", []) + item.get("rss_images", [])
    reasons = []
    photo = get_real_photo(url, images, reasons)
    photo_url = get_photo_url(url, images)
    # Telegram photo captions are limited to 1024 characters; text messages allow up to 4096.
    caption = build_caption(item, label, summary, url, max_length=1000 if (photo or photo_url) else 4000)
    if photo:
        try:
            telegram_api("sendPhoto", {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"}, photo=photo)
            print("Published with article image:", display_title(item))
            return True
        except Exception as exc:
            print(f"Photo send failed: {exc}")
            reasons.append(f"Telegram не принял файл: {exc}")
    if photo_url:
        try:
            telegram_api("sendPhoto", {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML", "photo": photo_url})
            print("Published with image link:", display_title(item))
            if reasons:
                notify_owner("Фото прикрепил по прямой ссылке, своя проверка не прошла:\n" + "\n".join(reasons[:3]), icon="📷")
            return True
        except Exception as exc:
            print(f"Photo by URL failed: {exc}")
            reasons.append(f"Telegram не принял ссылку на фото: {exc}")
    send_text(caption)
    print("Published text-only:", display_title(item))
    notify_owner(
        "Новость вышла без фото: " + display_title(item) + "\nПричины:\n" + "\n".join(reasons[:5] or ["картинка на странице не найдена"]),
        icon="📷",
    )
    return True


# -------------------------
# Deduplication and schedule
# -------------------------
def was_posted(item, state):
    return item["id"] in set(state.get("posted", [])) or same_story(item["title"], state.get("posted_titles", []))


def was_seen(item, state):
    return item["id"] in set(state.get("seen", [])) or same_story(item["title"], state.get("seen_titles", []))


def mark_seen(item, state):
    state.setdefault("seen", []).append(item["id"])
    state.setdefault("seen_titles", []).append(item["title"])


def mark_posted(item, state):
    state.setdefault("posted", []).append(item["id"])
    state.setdefault("posted_titles", []).append(item["title"])
    mark_seen(item, state)


def daily_weather(state):
    today = now_local().date().isoformat()
    if state.get("daily_date") == today:
        return
    try:
        send_text(get_weather())
        if PUBLISH:
            state["daily_date"] = today
        save_state(state)
    except Exception as exc:
        print(f"Weather error: {exc}")
        notify_owner(f"Не удалось получить погоду в Купино: {exc}")


def news_label(item):
    location = item.get("location")
    if location == "russia":
        return "🗞 Главное по стране"
    if is_incident(item):
        return "🚨 Происшествие · Купино" if location == "kupino" else "🚨 Происшествие · Новосибирск"
    return "📍 Купино и Купинский район" if location == "kupino" else "📍 Новосибирск"


def notify_found(items):
    """Триггер «новость найдена, в обработке»: одно сообщение владельцу за запуск."""
    lines = ["Новость найдена, в обработке."]
    for item in items[:5]:
        when = fmt_time(item.get("publish_after", time.time() + 420))
        lines.append(
            f"\n• {display_title(item)}\n  {item.get('source_name', '')}\n  {item.get('link', '')}\n"
            f"  Выйдет примерно в {when} (по Купино)."
        )
    if len(items) > 5:
        lines.append(f"\nИ ещё {len(items) - 5}.")
    lines.append("\nGitHub может запустить бота с опозданием, тогда пост выйдет чуть позже.")
    notify_owner("\n".join(lines), icon="🔎")


def maybe_notify_dead_sources(state):
    """Говорит, какие именно сайты не отдают новости (не чаще раза в сутки)."""
    dead = [name for name, count in SOURCE_STATS.items() if count == 0]
    if not dead:
        return
    now = time.time()
    if now - float(state.get("last_dead_notice", 0)) < 24 * 3600:
        return
    state["last_dead_notice"] = now
    notify_owner(
        "Эти источники сейчас не отдали ни одной новости (сайт не открывается, закрыт для GitHub "
        "или адрес неверный):\n• " + "\n• ".join(dead),
        icon="❗",
    )


def maybe_notify_empty_sources(state):
    """Если ни один сайт не отдал новостей, говорим об этом (не чаще раза в 3 часа), а не молчим."""
    now = time.time()
    if now - float(state.get("last_empty_notice", 0)) < 3 * 3600:
        return
    state["last_empty_notice"] = now
    notify_owner(
        "Ни один источник не отдал новости. Возможно, адреса сайтов в SOURCES неверные "
        "или сайты не открываются с серверов GitHub.",
        icon="❗",
    )


def process_news_queue(state, news):
    now = time.time()
    pending = state.get("pending", [])
    pending_ids = {item.get("id") for item in pending}

    # On the first successful scan, establish a baseline instead of publishing
    # every old item already visible on the sites at setup time.
    if not state.get("source_baseline_done"):
        if news:
            for item in news:
                mark_seen(item, state)
            state["source_baseline_done"] = True
            state["baselined_sources"] = sorted(n for n, c in SOURCE_STATS.items() if c > 0)
            notify_owner("Источники новостей проверены. Запомнил текущие публикации; новые новости будут попадать в очередь и выходить через 5–8 минут.", icon="✅")
        state["pending"] = pending
        save_state(state)
        return

    # Новый источник: тихо запоминаем его текущие публикации, чтобы не выложить весь архив сразу.
    baselined = set(state.get("baselined_sources", []))
    added_sources = []
    for name, count in SOURCE_STATS.items():
        if count > 0 and name not in baselined:
            for it in news:
                if it.get("source_name") == name:
                    mark_seen(it, state)
            baselined.add(name)
            added_sources.append(name)
    state["baselined_sources"] = sorted(baselined)
    if added_sources:
        notify_owner("Подключён новый источник: " + ", ".join(added_sources) + ". Запомнил его текущие публикации, выходить будут только новые.", icon="➕")

    new_items = []
    for item in news:
        if was_seen(item, state) or was_posted(item, state) or item.get("id") in pending_ids:
            continue
        if not is_local_story(item, item.get("location")):
            continue
        if any(w in item["title"].lower() for w in BLOCKED_TITLE_WORDS):
            mark_seen(item, state)
            print(f"Blocked by title filter: {item['title']}")
            continue
        if not passes_topic_filter(item):
            mark_seen(item, state)
            print(f"Filtered out (not on topic): {item['title']}")
            continue
        if item.get("location") == "russia":
            today = now_local().date().isoformat()
            if state.get("national_day") != today:
                state["national_day"], state["national_count"] = today, 0
            if state["national_count"] >= MAX_NATIONAL_PER_DAY:
                mark_seen(item, state)
                print(f"National daily limit reached, skipped: {item['title']}")
                continue
            state["national_count"] += 1
        item["detected_at"] = now
        item["publish_after"] = now + random.randint(300, 480)  # через 5–8 минут после находки
        pending.append(item)
        pending_ids.add(item["id"])
        mark_seen(item, state)
        new_items.append(item)
        print(f"Queued for publication: {item['title']}")

    if new_items:
        notify_found(new_items)

    remaining = []
    handled = 0
    for item in pending:
        if was_posted(item, state):
            continue
        publish_after = float(item.get("publish_after", now + 300))
        if now < publish_after:
            remaining.append(item)
            continue
        handled += 1
        try:
            if retraction_confirmed(item):
                notify_owner("Новость пропущена: в статье обнаружено опровержение.\n" + display_title(item))
                continue
            published = publish_story(item, news_label(item))
            if published:
                mark_posted(item, state)
                status = "Опубликовано в канале:" if PUBLISH else "Тестовый режим, в канал не отправлено:"
                notify_owner(status + "\n" + display_title(item), icon="✅")
            # publish_story returns False for a failed verification; skip it rather
            # than repeatedly trying to publish a bad or unrelated article.
        except Exception as exc:
            print(f"Queued news publication failed: {exc}")
            notify_owner(f"Не удалось опубликовать новость; повторю проверку через 5 минут:\n{display_title(item)}\n{exc}")
            item["publish_after"] = now + 300
            remaining.append(item)
    state["pending"] = remaining

    if not new_items and not remaining and not handled:
        maybe_notify_no_news(state)
    save_state(state)


def main():
    state = load_state()
    try:
        news = get_all_news()
    except Exception as exc:
        news = []
        print(f"Could not fetch local news: {exc}")
        notify_owner(f"Не удалось загрузить новости с местных сайтов: {exc}")

    if not news:
        maybe_notify_empty_sources(state)
    else:
        maybe_notify_dead_sources(state)

    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if now_local().hour >= 9 or manual:
        daily_weather(state)
    process_news_queue(state, news)
    save_state(state)


if __name__ == "__main__":
    main()

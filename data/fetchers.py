import asyncio
import feedparser
import httpx
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from config import (
    COINGECKO_URL,
    NBU_URL,
    YOUTUBE_API_KEY,
    NEWSAPI_KEY,
    GITHUB_API_URL,
    FRED_URL,
    WORLD_BANK_URL,
    RSS_FEEDS,
    YOUTUBE_MIN_VIEWS,
    YOUTUBE_SEARCH_ORDER,
    VIDEO_MIN_DURATION_SEC,
    VIDEO_MAX_DURATION_SEC,
)
from utils.http_safe import redact_secrets

logger = logging.getLogger(__name__)

# ISO 8601 тривалість з YouTube contentDetails, напр. PT9M54S / P1DT2H.
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso_duration(value: str) -> int:
    """Повертає тривалість у секундах; 0 — якщо розібрати не вдалося."""
    match = _ISO_DURATION_RE.match((value or "").strip())
    if not match:
        return 0
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


class YouTubeQuotaExceeded(Exception):
    """YouTube Data API повернув 429 — квота/rate limit вичерпано."""


# ─────────────────────────────────────────
# СПІЛЬНИЙ HTTP-КЛІЄНТ (пул з'єднань)
# ─────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15, follow_redirects=True)
    return _client


async def close() -> None:
    """Закриває спільний HTTP-клієнт (викликати при зупинці бота)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _raise_for_status(response: httpx.Response) -> None:
    """raise_for_status без секретів у повідомленні (key=/apiKey= у URL)."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        safe_url = redact_secrets(str(error.request.url))
        raise httpx.HTTPStatusError(
            f"HTTP {error.response.status_code} for '{safe_url}'",
            request=error.request,
            response=error.response,
        ) from None


# ─────────────────────────────────────────
# COINGECKO — курси крипти
# ─────────────────────────────────────────

async def fetch_crypto(coins: list = ["bitcoin", "ethereum", "solana"]) -> dict:
    """Повертає ціну і % зміну за 24год для списку монет."""
    ids = ",".join(coins)
    url = f"{COINGECKO_URL}/simple/price"
    params = {
        "ids": ids,
        "vs_currencies": "usd,uah",
        "include_24hr_change": "true",
    }
    r = await _get_client().get(url, params=params)
    _raise_for_status(r)
    return r.json()


async def fetch_trending_crypto() -> list:
    """Топ трендових монет на CoinGecko прямо зараз."""
    url = f"{COINGECKO_URL}/search/trending"
    r = await _get_client().get(url)
    _raise_for_status(r)
    coins = r.json().get("coins", [])
    return [c["item"]["name"] for c in coins[:5]]


# ─────────────────────────────────────────
# НБУ — офіційний курс валют
# ─────────────────────────────────────────

async def fetch_nbu_rates(currencies: list = ["USD", "EUR", "PLN"]) -> dict:
    """Повертає офіційний курс НБУ для вказаних валют."""
    # NBU очікує прапорець `?json` без значення; `?json=` дає 404.
    r = await _get_client().get(f"{NBU_URL}?json")
    _raise_for_status(r)
    all_rates = r.json()
    return {
        item["cc"]: item["rate"]
        for item in all_rates
        if item["cc"] in currencies
    }


# ─────────────────────────────────────────
# FRED API — макроекономіка США
# ─────────────────────────────────────────

async def fetch_fred(series_id: str, api_key: str) -> dict:
    """
    Повертає останнє значення макро-показника.
    Приклади series_id: FEDFUNDS (ставка ФРС), CPIAUCSL (інфляція), UNRATE (безробіття)
    """
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    r = await _get_client().get(FRED_URL, params=params)
    _raise_for_status(r)
    observations = r.json().get("observations", [])
    if observations:
        return {"series_id": series_id, "value": observations[0]["value"], "date": observations[0]["date"]}
    return {}


# ─────────────────────────────────────────
# WORLD BANK — ВВП країн
# ─────────────────────────────────────────

async def fetch_world_bank_gdp(country_code: str = "UA") -> dict:
    """Повертає останній ВВП країни."""
    url = f"{WORLD_BANK_URL}/country/{country_code}/indicator/NY.GDP.MKTP.CD"
    params = {"format": "json", "per_page": 1, "mrv": 1}
    r = await _get_client().get(url, params=params)
    _raise_for_status(r)
    data = r.json()
    if len(data) > 1 and data[1]:
        item = data[1][0]
        return {"country": item["country"]["value"], "value": item["value"], "year": item["date"]}
    return {}


# ─────────────────────────────────────────
# NEWSAPI — breaking news
# ─────────────────────────────────────────

async def fetch_news(query: str = "AI technology finance", language: str = "en", page_size: int = 5) -> list:
    """Повертає свіжі новини з NewsAPI."""
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": NEWSAPI_KEY,
        "from": (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r = await _get_client().get(url, params=params)
    _raise_for_status(r)
    articles = r.json().get("articles", [])
    return [
        {
            "title": a["title"],
            "description": a.get("description", ""),
            "url": a["url"],
            "published": a["publishedAt"],
        }
        for a in articles
    ]


# ─────────────────────────────────────────
# RSS — українські та міжнародні медіа
# ─────────────────────────────────────────

def fetch_rss(feed_url: str, limit: int = 5) -> list:
    """Парсить RSS стрічку і повертає останні N статей."""
    feed = feedparser.parse(feed_url)
    items = []
    for entry in feed.entries[:limit]:
        items.append({
            "title": entry.get("title", ""),
            "summary": entry.get("summary", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
        })
    return items


async def fetch_all_rss(limit_per_feed: int = 3) -> list:
    """Збирає новини з усіх RSS джерел."""
    all_items = []
    for feed_url in RSS_FEEDS:
        try:
            # feedparser блокуючий (мережа + парсинг) — виносимо в потік,
            # щоб не зупиняти event loop на кожній стрічці.
            items = await asyncio.to_thread(fetch_rss, feed_url, limit_per_feed)
            all_items.extend(items)
        except Exception:
            pass
    return all_items


# ─────────────────────────────────────────
# GITHUB TRENDING — трендові репозиторії
# ─────────────────────────────────────────

async def fetch_github_trending(topic: str = "artificial-intelligence") -> list:
    """Повертає топ трендових репозиторіїв за темою за останній тиждень."""
    date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    params = {
        "q": f"topic:{topic} created:>{date_from}",
        "sort": "stars",
        "order": "desc",
        "per_page": 5,
    }
    headers = {"Accept": "application/vnd.github+json"}
    r = await _get_client().get(GITHUB_API_URL, params=params, headers=headers)
    _raise_for_status(r)
    items = r.json().get("items", [])
    return [
        {
            "name": repo["full_name"],
            "description": repo.get("description", ""),
            "stars": repo["stargazers_count"],
            "url": repo["html_url"],
            "language": repo.get("language", ""),
        }
        for repo in items
    ]


# ─────────────────────────────────────────
# YOUTUBE — пошук відео
# ─────────────────────────────────────────

async def fetch_youtube_videos(
    query: str,
    max_results: int = 10,
    published_after_hours: int = 48,
    min_views: int | None = None,
) -> list:
    """
    Шукає свіжі відео на YouTube за релевантністю.
    Фільтрує по переглядах (min_views, за замовч. YOUTUBE_MIN_VIEWS) і по
    тривалості (VIDEO_MIN_DURATION_SEC..VIDEO_MAX_DURATION_SEC), щоб відсіяти
    Shorts і багатогодинні стріми.
    При 429 піднімає YouTubeQuotaExceeded — викликач має зупинити подальші пошуки.
    """
    views_threshold = YOUTUBE_MIN_VIEWS if min_views is None else min_views
    published_after = (
        datetime.now(timezone.utc) - timedelta(hours=published_after_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    search_url = "https://www.googleapis.com/youtube/v3/search"
    search_params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": YOUTUBE_SEARCH_ORDER,
        "publishedAfter": published_after,
        "maxResults": max_results,
        "relevanceLanguage": "en",   # зміщуємо видачу в бік англомовного контенту
        "key": YOUTUBE_API_KEY,
    }

    r = await _get_client().get(search_url, params=search_params)
    if r.status_code == 429:
        raise YouTubeQuotaExceeded("YouTube Search API: 429 Too Many Requests")
    _raise_for_status(r)
    search_data = r.json()

    video_ids = [item["id"]["videoId"] for item in search_data.get("items", [])]
    if not video_ids:
        return []

    # Витягуємо статистику для фільтрації по переглядах
    stats_url = "https://www.googleapis.com/youtube/v3/videos"
    stats_params = {
        "part": "statistics,snippet,contentDetails",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY,
    }
    r = await _get_client().get(stats_url, params=stats_params)
    if r.status_code == 429:
        raise YouTubeQuotaExceeded("YouTube Videos API: 429 Too Many Requests")
    _raise_for_status(r)
    stats_data = r.json()

    videos = []
    for item in stats_data.get("items", []):
        views = int(item["statistics"].get("viewCount", 0))
        if views < views_threshold:
            continue

        duration_sec = parse_iso_duration(
            (item.get("contentDetails") or {}).get("duration", "")
        )
        # 0 = стрім або нерозпізнаний формат; такі для рубрики не годяться.
        if not VIDEO_MIN_DURATION_SEC <= duration_sec <= VIDEO_MAX_DURATION_SEC:
            continue

        snippet = item["snippet"]
        videos.append({
            "video_id": item["id"],
            "title": snippet["title"],
            "channel": snippet["channelTitle"],
            "description": snippet.get("description", ""),
            "views": views,
            "duration_sec": duration_sec,
            "published": snippet["publishedAt"],
            "thumbnail": snippet["thumbnails"]["high"]["url"],
            "url": f"https://youtu.be/{item['id']}",
            # Мова аудіо/опису — щоб відсіювати неангломовні ролики.
            "language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "",
        })

    return sorted(videos, key=lambda x: x["views"], reverse=True)


# ─────────────────────────────────────────
# REDDIT — топ пости
# ─────────────────────────────────────────

async def fetch_reddit(subreddit: str = "technology", limit: int = 5) -> list:
    """Повертає топ постів із subreddit без авторизації."""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json"
    headers = {"User-Agent": "FinProBot/1.0"}
    params = {"limit": limit}
    r = await _get_client().get(url, headers=headers, params=params)
    _raise_for_status(r)
    posts = r.json()["data"]["children"]
    return [
        {
            "title": p["data"]["title"],
            "score": p["data"]["score"],
            "url": f"https://reddit.com{p['data']['permalink']}",
        }
        for p in posts
        if not p["data"].get("stickied")
    ]


# ─────────────────────────────────────────
# WIKIDATA + FLAGCDN — країни та прапори
# ─────────────────────────────────────────
# REST Countries із 2026-го вимагає ключ (v1–v4 вимкнули, v5 — тільки з
# Authorization), тому країни беремо з Wikidata: без ключа, українські назви
# в комплекті, і те саме джерело потрібне для рубрики фактів.

_WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
_WIKI_USER_AGENT = "FinProBot/1.0 (Telegram channel for teens)"

# Q3624078 — суверенна держава; P576 відсіює вже неіснуючі країни.
_COUNTRIES_QUERY = """
SELECT ?iso ?countryLabel ?capitalLabel ?population ?area
       ?currencyLabel ?languageLabel ?continentLabel WHERE {
  ?country wdt:P31 wd:Q3624078 ;
           wdt:P297 ?iso .
  FILTER NOT EXISTS { ?country wdt:P576 ?dissolved }
  OPTIONAL { ?country wdt:P36 ?capital }
  OPTIONAL { ?country wdt:P1082 ?population }
  OPTIONAL { ?country wdt:P2046 ?area }
  OPTIONAL { ?country wdt:P38 ?currency }
  OPTIONAL { ?country wdt:P37 ?language }
  OPTIONAL { ?country wdt:P30 ?continent }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "uk". }
}
"""


def _sparql_value(row: dict, key: str) -> str:
    return ((row.get(key) or {}).get("value") or "").strip()


def _has_cyrillic(text: str) -> bool:
    return any("\u0400" <= char <= "\u04ff" for char in text)


async def fetch_countries() -> list:
    """Країни з Wikidata: українська назва, столиця, населення, валюта.

    Запит повільний (~10 с) і вертає кілька рядків на країну, коли значень
    декілька, тому результат варто кешувати, а не тягнути перед кожним постом.
    """
    response = await _get_client().get(
        _WIKIDATA_SPARQL,
        params={"query": _COUNTRIES_QUERY, "format": "json"},
        headers={"User-Agent": _WIKI_USER_AGENT},
        timeout=60,
    )
    _raise_for_status(response)

    by_code: dict[str, dict] = {}
    for row in (response.json().get("results") or {}).get("bindings") or []:
        code = _sparql_value(row, "iso").lower()
        name = _sparql_value(row, "countryLabel")
        # Без української назви країна нам не підходить: Wikidata в такому разі
        # віддає англійську або взагалі Q-код.
        if len(code) != 2 or not name or not _has_cyrillic(name):
            continue
        if code in by_code:
            continue

        try:
            population = int(float(_sparql_value(row, "population") or 0))
        except ValueError:
            population = 0
        try:
            area = float(_sparql_value(row, "area") or 0)
        except ValueError:
            area = 0.0

        by_code[code] = {
            "code": code,
            "name_uk": name,
            "capital": _sparql_value(row, "capitalLabel"),
            "population": population,
            "area": area,
            "currency": _sparql_value(row, "currencyLabel"),
            "language": _sparql_value(row, "languageLabel"),
            "continent": _sparql_value(row, "continentLabel"),
        }
    return sorted(by_code.values(), key=lambda item: item["name_uk"])


# Факти для однієї країни: сусіди, води, бік руху. Запит дрібний (~2 с), тому
# робимо його на вибрану країну, а не на весь довідник.
_COUNTRY_EXTRAS_QUERY = """
SELECT ?neighbourName ?waterName ?sideName ?typeName ?languageName WHERE {
  ?country wdt:P297 "%s" .
  OPTIONAL {
    ?country wdt:P31 ?type .
    ?type rdfs:label ?typeName .
    FILTER(LANG(?typeName) = "uk")
  }
  OPTIONAL {
    ?country wdt:P37 ?language .
    ?language rdfs:label ?languageName .
    FILTER(LANG(?languageName) = "uk")
  }
  OPTIONAL {
    ?country wdt:P47 ?neighbour .
    ?neighbour rdfs:label ?neighbourName .
    FILTER(LANG(?neighbourName) = "uk")
  }
  OPTIONAL {
    ?country wdt:P206 ?water .
    ?water rdfs:label ?waterName .
    FILTER(LANG(?waterName) = "uk")
  }
  OPTIONAL {
    ?country wdt:P1622 ?side .
    ?side rdfs:label ?sideName .
    FILTER(LANG(?sideName) = "uk")
  }
}
"""


async def fetch_country_extras(country_code: str) -> dict:
    """Сусіди, води та бік руху для країни за ISO-кодом.

    P47 включає й морських сусідів (для Японії це, наприклад, США), тому
    викликаючий код сам вирішує, як їх подавати.
    """
    code = (country_code or "").strip().upper()
    if len(code) != 2:
        return {}

    try:
        response = await _get_client().get(
            _WIKIDATA_SPARQL,
            params={"query": _COUNTRY_EXTRAS_QUERY % code, "format": "json"},
            headers={"User-Agent": _WIKI_USER_AGENT},
            timeout=30,
        )
        _raise_for_status(response)
    except Exception as error:
        logger.warning("[country] Додаткові факти для %s не прийшли: %s", code, error)
        return {}

    neighbours: set[str] = set()
    waters: set[str] = set()
    sides: set[str] = set()
    types: set[str] = set()
    languages: set[str] = set()
    for row in (response.json().get("results") or {}).get("bindings") or []:
        if name := _sparql_value(row, "neighbourName"):
            neighbours.add(name)
        if name := _sparql_value(row, "waterName"):
            waters.add(name)
        if name := _sparql_value(row, "sideName"):
            sides.add(name)
        if name := _sparql_value(row, "typeName"):
            types.add(name.lower())
        if name := _sparql_value(row, "languageName"):
            languages.add(name)

    return {
        "neighbours": sorted(neighbours),
        "waters": sorted(waters),
        "languages": sorted(languages),
        "drives_left": any("ліво" in side for side in sides),
        "is_island": any("острівна" in item for item in types),
        "landlocked": any("виходу до моря" in item for item in types),
    }


async def fetch_flag_png(country_code: str, width: int = 640) -> bytes:
    """PNG прапора з FlagCDN. Порожньо — якщо картинки немає."""
    code = (country_code or "").strip().lower()
    if not code:
        return b""

    url = f"https://flagcdn.com/w{width}/{code}.png"
    try:
        response = await _get_client().get(url)
        # FlagCDN на невідомий код віддає 404, а не картинку.
        _raise_for_status(response)
    except Exception as error:
        logger.warning("[flagcdn] Прапор %s недоступний: %s", code, error)
        return b""
    return response.content


# ─────────────────────────────────────────
# ВІКІПЕДІЯ — короткий опис українською
# ─────────────────────────────────────────

async def fetch_wikipedia_summary(title: str, lang: str = "uk") -> dict:
    """Короткий опис статті: {title, extract, url}. Порожньо — якщо не знайшли."""
    if not (title or "").strip():
        return {}

    url = (
        f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
        f"{quote(title.strip().replace(' ', '_'), safe='')}"
    )
    try:
        response = await _get_client().get(url, headers={"User-Agent": _WIKI_USER_AGENT})
        _raise_for_status(response)
    except Exception as error:
        logger.warning("[wiki] %s: %s", title, error)
        return {}

    data = response.json() or {}
    # Сторінки-роздільники змісту не мають — для нас це те саме, що й нічого.
    if data.get("type") == "disambiguation":
        return {}
    return {
        "title": (data.get("title") or "").strip(),
        "extract": (data.get("extract") or "").strip(),
        "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
    }

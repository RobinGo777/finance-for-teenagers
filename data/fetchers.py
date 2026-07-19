import asyncio
import feedparser
import httpx
from datetime import datetime, timedelta, timezone
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
)

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
    r.raise_for_status()
    return r.json()


async def fetch_trending_crypto() -> list:
    """Топ трендових монет на CoinGecko прямо зараз."""
    url = f"{COINGECKO_URL}/search/trending"
    r = await _get_client().get(url)
    r.raise_for_status()
    coins = r.json().get("coins", [])
    return [c["item"]["name"] for c in coins[:5]]


# ─────────────────────────────────────────
# НБУ — офіційний курс валют
# ─────────────────────────────────────────

async def fetch_nbu_rates(currencies: list = ["USD", "EUR", "PLN"]) -> dict:
    """Повертає офіційний курс НБУ для вказаних валют."""
    # NBU очікує прапорець `?json` без значення; `?json=` дає 404.
    r = await _get_client().get(f"{NBU_URL}?json")
    r.raise_for_status()
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
    r.raise_for_status()
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
    r.raise_for_status()
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
    r.raise_for_status()
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
    r.raise_for_status()
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
    Шукає свіжі короткі відео на YouTube.
    Фільтрує по мінімальній кількості переглядів (min_views, за замовч. YOUTUBE_MIN_VIEWS).
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
        "videoDuration": "short",
        "order": "date",
        "publishedAfter": published_after,
        "maxResults": max_results,
        "relevanceLanguage": "en",   # зміщуємо видачу в бік англомовного контенту
        "key": YOUTUBE_API_KEY,
    }

    r = await _get_client().get(search_url, params=search_params)
    r.raise_for_status()
    search_data = r.json()

    video_ids = [item["id"]["videoId"] for item in search_data.get("items", [])]
    if not video_ids:
        return []

    # Витягуємо статистику для фільтрації по переглядах
    stats_url = "https://www.googleapis.com/youtube/v3/videos"
    stats_params = {
        "part": "statistics,snippet",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY,
    }
    r = await _get_client().get(stats_url, params=stats_params)
    r.raise_for_status()
    stats_data = r.json()

    videos = []
    for item in stats_data.get("items", []):
        views = int(item["statistics"].get("viewCount", 0))
        if views >= views_threshold:
            snippet = item["snippet"]
            videos.append({
                "video_id": item["id"],
                "title": snippet["title"],
                "channel": snippet["channelTitle"],
                "views": views,
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
    r.raise_for_status()
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

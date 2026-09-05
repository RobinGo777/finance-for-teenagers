"""Свіжі / ювілейні випуски банкнот — збір і антиспам-фільтр."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from config import (
    BANKNOTE_BONISTIKA,
    BANKNOTE_BONISTIKA_LIMIT,
    BANKNOTE_LOOKBACK_DAYS,
    BANKNOTE_MAX_CANDIDATES,
    BANKNOTE_MIN_SCORE,
    BANKNOTE_NEWS_QUERY,
    BANKNOTE_RSS_FEEDS,
    NEWSAPI_KEY,
)
from data.fetchers import _get_client, _raise_for_status, fetch_rss
from utils.http_safe import redact_secrets

logger = logging.getLogger(__name__)

# Має бути хоча б один сигнал «банкнота» і один «свіжий випуск».
_BANKNOTE_TERMS = (
    "banknote",
    "bank note",
    "banknotes",
    "paper money",
    "банкнот",
    "банкнота",
    "банкноти",
    "купюр",
    "денежных знаков",
    "денежные знаки",
)

_FRESH_TERMS = (
    "new banknote",
    "new series",
    "newly issued",
    "issues new",
    "issued a new",
    "unveils",
    "unveiled",
    "releases new",
    "released a new",
    "launches new",
    "put into circulation",
    "enters circulation",
    "commemorative banknote",
    "commemorative note",
    "anniversary banknote",
    "jubilee banknote",
    "polymer note",
    "ювілейн",
    "пам'ятн",
    "памятн",
    "нова банкнот",
    "нову банкнот",
    "новий випуск",
    "вводить в обіг",
    "ввела в обіг",
    "презентувала банкнот",
    "презентував банкнот",
    # bonistika.net / російські заголовки
    "новая купюр",
    "новую купюр",
    "новой купюр",
    "новые купюр",
    "новая банкнот",
    "новую банкнот",
    "новой банкнот",
    "новые банкнот",
    "новой серии",
    "новая серия",
    "нового поколения",
    "представлена",
    "представил",
    "презентац",
    "выпустил в обращение",
    "выпущена",
    "выпущены",
    "введена в обращение",
    "введены в обращение",
    "юбилейн",
    "памятн",
    "новый номинал",
    "новый дизайн",
    "модернизован",
    "обновленн",
)

# Відсікаємо аукціони, «скільки коштує рідкісна», каталожний шум.
_SPAM_TERMS = (
    "auction",
    "for sale",
    "sold for",
    "bids",
    "ebay",
    "heritage auctions",
    "stack's",
    "price guide",
    "grading",
    "pmg ",
    "pcgs",
    "most valuable",
    "rarest",
    "worth of",
    "how much is",
    "found in attic",
    "collection for sale",
    "want to buy",
    "wts ",
    "wtb ",
    "error note worth",
    "investment tip",
    "catalog update",
    "catalogue update",
    "numista",
    "colnect",
    "продається",
    "аукціон",
    "скільки коштує",
    "рідкісна банкнота",
    "найдорожч",
    "аукцион",
    "продается",
    "сколько стоит",
    "оценка банкнот",
)

_SPACE_RE = re.compile(r"\s+")

# Стоп-слова для відбитка заголовка (різні статті → та сама банкнота).
_TITLE_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "about", "this", "that",
    "new", "news", "press", "release", "releases", "released", "issues",
    "issued", "issue", "unveils", "unveiled", "unveil", "launches", "launched",
    "puts", "put", "into", "circulation", "enters", "central", "national",
    "bank", "banks", "banknote", "banknotes", "note", "notes", "paper",
    "money", "currency", "series", "design", "features", "feature",
    "announces", "announced", "introduces", "introduced", "presents",
    "нова", "новий", "нова", "нову", "банкнота", "банкноти", "банкноту",
    "банкнот", "випуск", "прес", "реліз", "презентував", "презентувала",
    "вводить", "ввела", "обіг", "про", "для", "що", "як", "та", "або",
})


def _norm(text: str) -> str:
    return _SPACE_RE.sub(" ", (text or "").lower()).strip()


def title_fingerprint(title: str, summary: str = "") -> str:
    """Стабільний відбиток змісту заголовка (не URL)."""
    text = _norm(f"{title} {summary}")
    if not text:
        return ""
    nums = re.findall(r"\d+", text)
    tokens = [
        t for t in re.findall(r"[a-zа-яіїєґ]{3,}", text, flags=re.IGNORECASE)
        if t.lower() not in _TITLE_STOPWORDS
    ]
    # Цифри (номінал/рік) + змістовні слова — сортовані для стабільності.
    parts = sorted({n for n in nums}) + sorted({t.lower() for t in tokens})[:10]
    if not parts:
        return ""
    digest = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"bn:title:{digest}"


def issue_fingerprint(
    country: str = "",
    denomination: str = "",
    issue_type: str = "",
) -> str:
    """Відбиток конкретної банкноти: країна + номінал + тип."""
    c = _norm(country)
    d = _norm(denomination)
    # Лише цифри/літери з номіналу (EUR 20 / 20 євро → 20).
    d_nums = "".join(re.findall(r"[a-zа-яіїєґ0-9]+", d))
    t = _norm(issue_type)
    t_key = ""
    for label, aliases in (
        ("jubilee", ("jubilee", "anniversary", "ювілей", "commemorative", "пам'ят", "памят")),
        ("series", ("series", "сері")),
        ("new", ("новий номінал", "new denomination", "новий")),
    ):
        if any(a in t for a in aliases):
            t_key = label
            break
    if not c and not d_nums:
        return ""
    raw = f"{c}|{d_nums}|{t_key}"
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return f"bn:issue:{digest}"


def title_token_set(title: str) -> set[str]:
    text = _norm(title)
    words = set(re.findall(r"[a-zа-яіїєґ]{3,}", text, flags=re.IGNORECASE))
    nums = set(re.findall(r"\d+", text))
    keep = {t.lower() for t in words if t.lower() not in _TITLE_STOPWORDS}
    return keep | nums


def titles_too_similar(a: str, b: str, threshold: float = 0.55) -> bool:
    """True, якщо заголовки майже про ту саму банкноту."""
    sa, sb = title_token_set(a), title_token_set(b)
    if not sa or not sb:
        return False
    overlap = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return False
    # Спільні цифри номіналу/року — сильний сигнал.
    nums_a = {t for t in sa if t.isdigit()}
    nums_b = {t for t in sb if t.isdigit()}
    if nums_a and nums_a == nums_b and overlap >= 2:
        return True
    return (overlap / union) >= threshold


def _parse_published(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def score_banknote_item(title: str, summary: str = "") -> int:
    """Повертає score ≥0; 0 = відхилити."""
    text = _norm(f"{title} {summary}")
    if not text:
        return 0

    if any(term in text for term in _SPAM_TERMS):
        return 0

    has_banknote = any(term in text for term in _BANKNOTE_TERMS)
    if not has_banknote:
        return 0

    fresh_hits = [term for term in _FRESH_TERMS if term in text]
    if not fresh_hits:
        return 0

    score = 10 + min(len(fresh_hits), 4) * 3

    # Бонус за конкретність (країна/номінал часто в заголовку з цифрами).
    if re.search(r"\b\d+\b", title or ""):
        score += 2
    if any(
        w in text
        for w in (
            "commemorative", "anniversary", "jubilee",
            "ювілейн", "пам'ятн", "юбилейн", "памятн",
        )
    ):
        score += 4
    if any(
        w in text
        for w in (
            "central bank", "national bank", "нбу", "ecb", "bank of",
            "банк канады", "центральный банк", "национальный банк",
            "государственный банк",
        )
    ):
        score += 3

    return score


def _dedupe_key(item: dict) -> str:
    url = (item.get("url") or "").strip()
    if url:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().removeprefix("www.")
        path = (parsed.path or "").rstrip("/")
        if host and path:
            return f"{host}{path}"
    return _norm(item.get("title", ""))[:120]


def filter_banknote_candidates(
    items: list[dict],
    *,
    min_score: int | None = None,
    lookback_days: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Фільтрує, дедупить і сортує кандидатів за score."""
    threshold = BANKNOTE_MIN_SCORE if min_score is None else min_score
    days = BANKNOTE_LOOKBACK_DAYS if lookback_days is None else lookback_days
    max_items = BANKNOTE_MAX_CANDIDATES if limit is None else limit
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))

    best_by_key: dict[str, dict] = {}
    for raw in items:
        title = (raw.get("title") or "").strip()
        summary = (raw.get("summary") or raw.get("description") or "").strip()
        score = score_banknote_item(title, summary)
        if score < threshold:
            continue

        published = _parse_published(raw.get("published"))
        if published and published < cutoff:
            continue

        item = {
            "title": title,
            "summary": summary,
            "url": (raw.get("url") or raw.get("link") or "").strip(),
            "image_url": (raw.get("image_url") or "").strip(),
            "published": raw.get("published") or "",
            "source": (raw.get("source") or "").strip(),
            "score": score,
        }
        key = _dedupe_key(item)
        if not key:
            continue
        prev = best_by_key.get(key)
        if prev is None or item["score"] > prev["score"]:
            best_by_key[key] = item

    ranked = sorted(
        best_by_key.values(),
        key=lambda x: (x["score"], x.get("published") or ""),
        reverse=True,
    )
    return ranked[: max(1, max_items)]


async def _fetch_newsapi_banknotes() -> list[dict]:
    if not NEWSAPI_KEY:
        return []

    since = (
        datetime.now(timezone.utc) - timedelta(days=BANKNOTE_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "q": BANKNOTE_NEWS_QUERY,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 20,
        "apiKey": NEWSAPI_KEY,
        "from": since,
    }
    try:
        r = await _get_client().get("https://newsapi.org/v2/everything", params=params)
        _raise_for_status(r)
        articles = r.json().get("articles", [])
    except Exception as e:
        logger.warning("[banknotes] NewsAPI: %s", redact_secrets(str(e)))
        return []

    out = []
    for a in articles:
        out.append({
            "title": a.get("title") or "",
            "summary": a.get("description") or "",
            "url": a.get("url") or "",
            "image_url": a.get("urlToImage") or "",
            "published": a.get("publishedAt") or "",
            "source": (a.get("source") or {}).get("name") or "NewsAPI",
        })
    return out


async def _fetch_rss_banknotes() -> list[dict]:
    all_items: list[dict] = []
    for feed_url in BANKNOTE_RSS_FEEDS:
        try:
            items = await asyncio.to_thread(fetch_rss, feed_url, 8)
            for item in items:
                all_items.append({
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "url": item.get("link", ""),
                    "image_url": "",
                    "published": item.get("published", ""),
                    "source": "RSS",
                })
        except Exception as e:
            logger.warning("[banknotes] RSS %s: %s", feed_url[:60], e)
    return all_items


async def _fetch_bonistika_banknotes() -> list[dict]:
    from data.bonistika import fetch_bonistika_news

    if not BANKNOTE_BONISTIKA:
        return []
    try:
        return await fetch_bonistika_news(limit=BANKNOTE_BONISTIKA_LIMIT)
    except Exception as error:
        logger.warning("[banknotes] bonistika: %s", error)
        return []


async def fetch_new_banknotes() -> list[dict]:
    """Повертає відфільтровані свіжі/ювілейні випуски (без спаму)."""
    news_items, rss_items, bonistika_items = await asyncio.gather(
        _fetch_newsapi_banknotes(),
        _fetch_rss_banknotes(),
        _fetch_bonistika_banknotes(),
    )
    combined = news_items + rss_items + bonistika_items
    filtered = filter_banknote_candidates(combined)
    logger.info(
        "[banknotes] кандидатів сирих=%s (news=%s rss=%s bonistika=%s), після фільтра=%s",
        len(combined),
        len(news_items),
        len(rss_items),
        len(bonistika_items),
        len(filtered),
    )
    return filtered

"""Парсер новин [bonistika.net](https://www.bonistika.net/) — свіжі випуски банкнот.

RSS немає: читаємо головну (список id) і сторінки `/news/show/<id>`.
Текст російською — українською вже переписує Gemini у generators/banknotes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from urllib.parse import urljoin

from data.fetchers import _get_client

logger = logging.getLogger(__name__)

BONISTIKA_HOME = "https://www.bonistika.net/"
BONISTIKA_ORIGIN = "https://www.bonistika.net"

_USER_AGENT = "FinProBot/1.0 (Telegram channel for teens; banknote news)"

_NEWS_LINK_RE = re.compile(
    r'href=["\'](/news/show/(\d+))["\']',
    flags=re.I,
)
_OG_RE = re.compile(
    r'property=["\']og:(title|description|image|url)["\']\s+content=["\']([^"\']*)["\']',
    flags=re.I,
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", flags=re.I | re.S)
_DATE_RE = re.compile(
    r"<small[^>]*>.*?(\d{4}-\d{2}-\d{2})\s*:\s*</small>",
    flags=re.I | re.S,
)
_CARD_TEXT_RE = re.compile(
    r'<div class="card-text">(.*?)</div>',
    flags=re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _decode_html(content: bytes, content_type: str = "") -> str:
    """Сайт віддає cp1251; інколи в заголовку інше — пробуємо кілька варіантів."""
    hinted = ""
    match = re.search(r"charset=([\w-]+)", content_type or "", flags=re.I)
    if match:
        hinted = match.group(1).strip().lower()
    for encoding in (hinted, "cp1251", "windows-1251", "utf-8"):
        if not encoding:
            continue
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _abs_url(path: str) -> str:
    clean = (path or "").strip()
    if not clean:
        return ""
    if clean.startswith("//"):
        return "https:" + clean
    return urljoin(BONISTIKA_ORIGIN + "/", clean.lstrip("/"))


def _strip_html(chunk: str) -> str:
    text = unescape(_TAG_RE.sub(" ", chunk or ""))
    return _SPACE_RE.sub(" ", text).strip()


def _prefer_full_image(url: str) -> str:
    """`/img_news/120/...` і `/240/...` → повний `/img_news/...`."""
    if not url:
        return ""
    return re.sub(r"/img_news/(?:120|240)/", "/img_news/", url)


def parse_home_news_ids(html: str, *, limit: int = 8) -> list[int]:
    """Унікальні id новин з головної, від новіших до старіших."""
    seen: set[int] = set()
    ordered: list[int] = []
    for _, raw_id in _NEWS_LINK_RE.findall(html or ""):
        news_id = int(raw_id)
        if news_id in seen:
            continue
        seen.add(news_id)
        ordered.append(news_id)
        if len(ordered) >= max(1, limit):
            break
    return ordered


def parse_bonistika_article(html: str, news_id: int) -> dict | None:
    """Витягує заголовок, текст, дату й фото зі сторінки новини."""
    if not html:
        return None

    og: dict[str, str] = {}
    for key, value in _OG_RE.findall(html):
        og[key.lower()] = unescape(value).strip()

    title = og.get("title") or ""
    if not title:
        h1 = _H1_RE.search(html)
        if h1:
            title = _strip_html(h1.group(1))
    if not title:
        return None

    summary = og.get("description") or ""
    if not summary:
        cards = _CARD_TEXT_RE.findall(html)
        if cards:
            summary = _strip_html(cards[0])[:600]

    published = ""
    date_match = _DATE_RE.search(html)
    if date_match:
        published = date_match.group(1)

    image = _prefer_full_image(og.get("image") or "")
    if not image:
        img_match = re.search(
            r'(?:href|src)=["\'](/img_news/(?:120/|240/)?[^"\']+\.(?:jpg|jpeg|png|webp))["\']',
            html,
            flags=re.I,
        )
        if img_match:
            image = _prefer_full_image(_abs_url(img_match.group(1)))

    url = og.get("url") or f"{BONISTIKA_ORIGIN}/news/show/{news_id}"
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    if url.startswith("/"):
        url = _abs_url(url)

    return {
        "title": title,
        "summary": summary,
        "url": url,
        "image_url": image,
        "published": published,
        "source": "bonistika.net",
        "bonistika_id": news_id,
    }


async def _fetch_html(url: str) -> str:
    response = await _get_client().get(
        url,
        headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return _decode_html(response.content, response.headers.get("content-type", ""))


async def fetch_bonistika_news(*, limit: int = 6) -> list[dict]:
    """Останні новини з bonistika.net (головна + картки статей)."""
    cap = max(1, min(int(limit), 12))
    try:
        home_html = await _fetch_html(BONISTIKA_HOME)
    except Exception as error:
        logger.warning("[bonistika] головна: %s", error)
        return []

    news_ids = parse_home_news_ids(home_html, limit=cap)
    if not news_ids:
        logger.info("[bonistika] на головній не знайдено /news/show/…")
        return []

    semaphore = asyncio.Semaphore(3)

    async def _one(news_id: int) -> dict | None:
        async with semaphore:
            try:
                html = await _fetch_html(f"{BONISTIKA_ORIGIN}/news/show/{news_id}")
            except Exception as error:
                logger.warning("[bonistika] стаття %s: %s", news_id, error)
                return None
            return parse_bonistika_article(html, news_id)

    parsed = await asyncio.gather(*[_one(news_id) for news_id in news_ids])
    items = [item for item in parsed if item]
    logger.info(
        "[bonistika] id=%s, статей=%s",
        len(news_ids),
        len(items),
    )
    return items

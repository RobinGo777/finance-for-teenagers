import re

import logging

from generators.gemini import generate_json, pick_persona, build_base_prompt
from data.redis_client import get_used_topics, save_topic, is_published, mark_published
from data.fetchers import fetch_youtube_videos, fetch_news
from config import (
    VISUAL_TEMPLATES,
    TRUSTED_VIDEO_CHANNEL_HINTS,
    VIDEO_CLICKBAIT_TERMS,
    VIDEO_MATCH_STOPWORDS,
    YOUTUBE_MIN_VIEWS,
    VIDEO_PUBLISHED_AFTER_HOURS,
    VIDEO_MIN_VIEWS_FLOOR,
)

logger = logging.getLogger(__name__)

RUBRIC_KEY     = "video"
RUBRIC_NAME    = "#ВідеоТижня"
RUBRIC_HASHTAG = "🎥 #ВідеоТижня"

# Пошукові запити — чередуємо для різноманіття
SEARCH_QUERIES = [
    "AI breakthrough 2026",
    "robot technology amazing 2026",
    "space mission 2026",
    "science discovery incredible",
    "future technology invention",
    "artificial intelligence new",
    "robotics innovation 2026",
    "NASA space exploration 2026",
]

MATCH_STOPWORDS = set(VIDEO_MATCH_STOPWORDS)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-zA-Zа-яА-ЯіїєґІЇЄҐ0-9]+", _normalize_text(text))
    return {t for t in tokens if len(t) > 2 and t not in MATCH_STOPWORDS}


def _has_clickbait(title: str) -> bool:
    t = _normalize_text(title)
    return any(term in t for term in VIDEO_CLICKBAIT_TERMS)


def _is_trusted_channel(channel: str) -> bool:
    c = _normalize_text(channel)
    return any(hint in c for hint in TRUSTED_VIDEO_CHANNEL_HINTS)


def _news_match_score(video_title: str, recent_news_titles: list[str]) -> int:
    """Наскільки відео перегукується зі свіжими новинами (0 = немає збігу).

    Використовується як БОНУС до рейтингу, а не як жорсткий фільтр —
    інакше майже завжди отримували б 0 відео.
    """
    video_tokens = _tokenize(video_title)
    if not video_tokens:
        return 0
    best = 0
    for news_title in recent_news_titles:
        overlap = len(video_tokens.intersection(_tokenize(news_title)))
        best = max(best, overlap)
    return best


async def _collect_videos(min_views: int, hours: int) -> list[dict]:
    """Збирає та дедуплікує відео по всіх пошукових запитах."""
    collected: dict[str, dict] = {}
    for query in SEARCH_QUERIES:
        try:
            videos = await fetch_youtube_videos(
                query=query,
                max_results=5,
                published_after_hours=hours,
                min_views=min_views,
            )
            for v in videos:
                collected[v["video_id"]] = v
        except Exception:
            continue
    return list(collected.values())


async def _filter_fresh(videos: list[dict]) -> list[dict]:
    """Прибирає клікбейт і вже опубліковані відео."""
    result = []
    for v in videos:
        if _has_clickbait(v.get("title", "")):
            continue
        if await is_published(v["video_id"]):
            continue
        result.append(v)
    return result


async def generate_video() -> dict | None:
    """
    Знаходить свіже топове відео на YouTube і генерує коментар.
    Повертає None якщо нічого цікавого не знайдено.
    Публікується у будь-який час коли знайдено (не вночі).

    Фільтри навмисно м'які + прогресивне послаблення: спершу нормальний
    поріг, якщо порожньо — знижуємо перегляди й розширюємо вікно дат.
    Збіг зі свіжими новинами — лише бонус до рейтингу, не фільтр.
    """

    persona     = pick_persona()
    used_topics = await get_used_topics(RUBRIC_KEY)

    # Organic Growth шаблон для відео
    template = next((t for t in VISUAL_TEMPLATES if t["name"] == "Organic Growth"), None)

    recent_news = await fetch_news(
        query="technology OR AI OR robotics OR science",
        language="en",
        page_size=20,
    )
    recent_news_titles = [item.get("title", "") for item in recent_news if item.get("title")]

    # Прогресивне послаблення: (мін. переглядів, вікно годин)
    attempts = [
        (YOUTUBE_MIN_VIEWS, VIDEO_PUBLISHED_AFTER_HOURS),
        (VIDEO_MIN_VIEWS_FLOOR, VIDEO_PUBLISHED_AFTER_HOURS),
        (VIDEO_MIN_VIEWS_FLOOR, VIDEO_PUBLISHED_AFTER_HOURS * 2),
    ]

    new_videos: list[dict] = []
    for min_views, hours in attempts:
        candidates = await _collect_videos(min_views=min_views, hours=hours)
        new_videos = await _filter_fresh(candidates)
        if new_videos:
            logger.info(
                "[video] Знайдено %s відео (min_views=%s, hours=%s)",
                len(new_videos), min_views, hours,
            )
            break

    if not new_videos:
        logger.info("[video] Нічого не знайдено навіть після послаблення фільтрів")
        return None

    # Рейтинг = надійний канал (+3) + релевантність новинам (0..3) + перегляди.
    def _rank(v: dict) -> tuple:
        score = 0
        if _is_trusted_channel(v.get("channel", "")):
            score += 3
        score += min(_news_match_score(v.get("title", ""), recent_news_titles), 3)
        return (score, v["views"])

    top_videos = sorted(new_videos, key=_rank, reverse=True)[:5]

    videos_str = "\n".join(
        f"- ID: {v['video_id']} | {v['title']} | {v['channel']} | {v['views']:,} переглядів"
        for v in top_videos
    )

    task = (
        "Вибери ОДНЕ найцікавіше відео зі списку для підлітків 12-20 років. "
        "Напиши захопливий коментар українською — що відбувається у відео і чому це вражає. "
        "Додай фінансовий або науковий кут якщо можливо."
    )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=f"Доступні відео:\n{videos_str}",
    )

    prompt = base + """
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{
  "video_id": "YouTube ID обраного відео",
  "topic": "тема відео (3-5 слів)",
  "post": "🎥 #ВідеоТижня\\n\\n[emoji] [що відбувається — 1-2 захопливі речення]\\n\\n[чому це важливо або вражає — 1-2 речення]\\n\\n💰 Цікавий факт: [фінансовий або науковий кут]\\n\\n💬 [питання читачам]\\n\\n👇 Дивись відео:"
}
"""

    data = await generate_json(prompt)

    video_id = data.get("video_id", "")

    # Перевіряємо що відео є в нашому списку
    selected = next((v for v in top_videos if v["video_id"] == video_id), top_videos[0])

    post_with_link = data["post"] + f"\nhttps://youtu.be/{selected['video_id']}"

    await save_topic(RUBRIC_KEY, data["topic"])
    await mark_published(selected["video_id"])

    return {
        "rubric": RUBRIC_KEY,
        "topic": data["topic"],
        "post": post_with_link,
        "image_url": selected["thumbnail"],  # YouTube thumbnail
        "video_id": selected["video_id"],
        "persona": persona["name"],
        "template": template["name"] if template else "Organic Growth",
    }

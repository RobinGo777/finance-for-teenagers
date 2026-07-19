import re
import logging
from datetime import date

from generators.gemini import generate_json, pick_persona, build_base_prompt
from data.redis_client import get_used_topics, save_topic, is_published, mark_published
from data.fetchers import fetch_youtube_videos, fetch_news
from config import (
    VISUAL_TEMPLATES,
    TRUSTED_VIDEO_CHANNEL_HINTS,
    VIDEO_CLICKBAIT_TERMS,
    VIDEO_MATCH_STOPWORDS,
    VIDEO_PUBLISHED_AFTER_HOURS,
    VIDEO_MIN_VIEWS_FLOOR,
    VIDEO_ALLOWED_LANGUAGES,
    NEWS_CHANNEL_HINTS,
    VIDEO_ENGAGING_TERMS,
    VIDEO_LOW_VALUE_TERMS,
)

logger = logging.getLogger(__name__)

# Скрипти, які підліток-глядач не зрозуміє (гінді, арабська, CJK, тайська тощо).
# Латиниця й кирилиця — ок. Так відсіюємо, напр., індомовні новинні ролики.
_NON_LATIN_SCRIPT = re.compile(
    r"[\u0600-\u06FF\u0900-\u097F\u0E00-\u0E7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]"
)

RUBRIC_KEY     = "video"
RUBRIC_NAME    = "#ВідеоТижня"
RUBRIC_HASHTAG = "🎥 #ВідеоТижня"

# Щодня беремо по одному запиту з кожної теми. Це дає різноманіття без
# надмірного витрачання квоти YouTube Search API під час перевірок монітора.
SEARCH_QUERY_GROUPS = {
    "ШІ": [
        "new AI tool demo",
        "AI agent demo",
        "AI video generation demo",
    ],
    "Гаджети й технології": [
        "new gadget hands-on demo",
        "future technology prototype demo",
        "new technology invention tested",
    ],
    "Стартапи": [
        "startup product demo launch",
        "Y Combinator startup demo",
        "young founder startup invention",
    ],
    "Наука й інженерія": [
        "science experiment demonstration",
        "engineering invention tested",
        "new scientific discovery explained",
    ],
    "Роботи й космос": [
        "humanoid robot demo",
        "rocket launch footage",
        "space technology demonstration",
    ],
    "Фінанси й фінтех": [
        "fintech app demo explained",
        "money technology explained",
        "personal finance for teenagers explained",
    ],
    "Ігри й цифрова економіка": [
        "video game economy explained",
        "game developer earnings explained",
        "esports business money explained",
    ],
}

MATCH_STOPWORDS = set(VIDEO_MATCH_STOPWORDS)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-zA-Zа-яА-ЯіїєґІЇЄҐ0-9]+", _normalize_text(text))
    return {t for t in tokens if len(t) > 2 and t not in MATCH_STOPWORDS}


def _has_clickbait(title: str) -> bool:
    t = _normalize_text(title)
    return any(term in t for term in VIDEO_CLICKBAIT_TERMS)


def _term_score(text: str, terms: list[str]) -> int:
    normalized = _normalize_text(text)
    return sum(1 for term in terms if term in normalized)


def _queries_for_day(day: date | None = None) -> list[tuple[str, str]]:
    """По одному запиту з кожної теми; вибір змінюється щодня."""
    current = day or date.today()
    seed = current.toordinal()
    return [
        (category, queries[(seed + offset) % len(queries)])
        for offset, (category, queries) in enumerate(SEARCH_QUERY_GROUPS.items())
    ]


def _is_trusted_channel(channel: str) -> bool:
    c = _normalize_text(channel)
    return any(hint in c for hint in TRUSTED_VIDEO_CHANNEL_HINTS)


def _is_news_channel(channel: str) -> bool:
    """Загальний новинний канал (часто балакучі сюжети без реальних кадрів)."""
    c = _normalize_text(channel)
    return any(hint in c for hint in NEWS_CHANNEL_HINTS)


def _title_is_understandable(title: str) -> bool:
    """False, якщо заголовок містить нелатинські/некириличні символи (гінді тощо)."""
    return not _NON_LATIN_SCRIPT.search(title or "")


def _language_ok(language: str) -> bool:
    """Пропускаємо англ/укр або невідому мову; відсіюємо явно чужомовні."""
    if not language:
        return True  # мову не вказано — не відкидаємо
    lang = language.lower().split("-")[0]
    return lang in VIDEO_ALLOWED_LANGUAGES


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
    """Збирає та дедуплікує відео по сьогоднішніх тематичних запитах."""
    collected: dict[str, dict] = {}
    for category, query in _queries_for_day():
        try:
            videos = await fetch_youtube_videos(
                query=query,
                max_results=6,
                published_after_hours=hours,
                min_views=min_views,
            )
            for v in videos:
                existing = collected.get(v["video_id"])
                if existing:
                    categories = set(existing.get("categories", []))
                    categories.add(category)
                    existing["categories"] = sorted(categories)
                else:
                    v["categories"] = [category]
                    collected[v["video_id"]] = v
        except Exception as exc:
            logger.warning("[video] YouTube-пошук '%s' не вдався: %s", query, exc)
            continue
    return list(collected.values())


async def _filter_fresh(videos: list[dict]) -> list[dict]:
    """Прибирає клікбейт, чужомовне та вже опубліковане."""
    result = []
    for v in videos:
        title = v.get("title", "")
        if _has_clickbait(title):
            continue
        if not _title_is_understandable(title):
            continue
        if not _language_ok(v.get("language", "")):
            continue
        searchable = f"{title} {v.get('description', '')}"
        # Новинний канал допускаємо лише коли метадані явно обіцяють реальні
        # кадри/демонстрацію, а не політичний або студійний переказ.
        if _is_news_channel(v.get("channel", "")):
            if _term_score(searchable, VIDEO_ENGAGING_TERMS) == 0:
                continue
            if _term_score(searchable, VIDEO_LOW_VALUE_TERMS) > 0:
                continue
        if await is_published(v["video_id"]):
            continue
        result.append(v)
    return result


def _video_rank(video: dict, recent_news_titles: list[str]) -> tuple[int, int]:
    """Ранжує корисність для підлітка; перегляди лише розв'язують нічию."""
    searchable = f"{video.get('title', '')} {video.get('description', '')}"
    score = min(_term_score(searchable, VIDEO_ENGAGING_TERMS), 3)
    score -= min(_term_score(searchable, VIDEO_LOW_VALUE_TERMS) * 3, 6)
    if _is_trusted_channel(video.get("channel", "")):
        score += 4
    if _is_news_channel(video.get("channel", "")):
        score -= 4
    score += min(_news_match_score(video.get("title", ""), recent_news_titles), 2)
    return score, int(video.get("views", 0))


async def generate_video() -> dict | None:
    """
    Знаходить свіже топове відео на YouTube і генерує коментар.
    Повертає None якщо нічого цікавого не знайдено.
    Публікується у будь-який час коли знайдено (не вночі).

    Один широкий пошук бере кандидатів за останні дні без повторного
    витрачання YouTube-квоти. Якість важливіша за саму кількість переглядів.
    Збіг зі свіжими новинами — лише бонус до рейтингу, не фільтр.
    """

    persona     = pick_persona()
    used_topics = await get_used_topics(RUBRIC_KEY)

    # Organic Growth шаблон для відео
    template = next((t for t in VISUAL_TEMPLATES if t["name"] == "Organic Growth"), None)

    recent_news = await fetch_news(
        query=(
            "technology OR AI OR startup OR gadget OR robotics OR science "
            "OR fintech OR gaming"
        ),
        language="en",
        page_size=20,
    )
    recent_news_titles = [item.get("title", "") for item in recent_news if item.get("title")]

    # Один запит із широким вікном замість повторення всіх пошуків для кожного
    # fallback-рівня. Це економить до 2/3 добової квоти YouTube Search API.
    candidates = await _collect_videos(
        min_views=VIDEO_MIN_VIEWS_FLOOR,
        hours=VIDEO_PUBLISHED_AFTER_HOURS * 2,
    )
    new_videos = await _filter_fresh(candidates)

    if not new_videos:
        logger.info("[video] Не знайдено якісних свіжих відео")
        return None
    logger.info(
        "[video] Знайдено %s кандидатів (min_views=%s, hours=%s)",
        len(new_videos),
        VIDEO_MIN_VIEWS_FLOOR,
        VIDEO_PUBLISHED_AFTER_HOURS * 2,
    )

    top_videos = sorted(
        new_videos,
        key=lambda video: _video_rank(video, recent_news_titles),
        reverse=True,
    )[:7]

    videos_str = "\n".join(
        (
            f"- ID: {v['video_id']} | Категорія: {', '.join(v.get('categories', []))} "
            f"| {v['title']} | {v['channel']} | {v['views']:,} переглядів "
            f"| Опис: {v.get('description', '')[:280]}"
        )
        for v in top_videos
    )

    task = (
        "Це рубрика «вау-відео для підлітка», а не телевізійні новини. "
        "Вибери ОДНЕ найцікавіше відео для аудиторії 12-20 років: про ШІ, "
        "гаджети, стартапи, науку, інженерію, роботів, космос, фінтех, "
        "особисті фінанси або цифрову економіку. Віддавай перевагу короткому "
        "демо, експерименту, тесту, огляду чи простому поясненню, яке "
        "15-річний глядач захотів би додивитися до кінця. Обирай те, що "
        "реально ПОКАЗУЄ предмет або процес, а не балакучий сюжет диктора, "
        "політичну заяву чи кадр зі статичною фотографією. Оцінюй лише за "
        "наданими назвою, каналом та описом — не вигадуй побачених кадрів. "
        "ВАЖЛИВО: якщо жодне відео не є справді цікавим і вартим публікації "
        "(нудне, чужомовне, лише новинний переказ без реальних кадрів) — "
        "поверни video_id: \"\" (порожній рядок), і ми нічого не опублікуємо. "
        "Краще пропустити тиждень, ніж показати слабке відео. "
        "Якщо відео гарне — напиши захопливий коментар українською: що воно "
        "демонструє або пояснює і чому це варте уваги. Додай доречний "
        "фінансовий, технологічний або науковий факт."
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

    video_id = (data.get("video_id") or "").strip()

    # LLM свідомо відмовився — жодне відео не варте публікації.
    if not video_id:
        logger.info("[video] Модель відхилила всі кандидати — пропускаємо публікацію")
        return None

    # Перевіряємо що відео є в нашому списку; якщо ні — не вигадуємо, пропускаємо.
    selected = next((v for v in top_videos if v["video_id"] == video_id), None)
    if selected is None:
        logger.info("[video] Обраний video_id не зі списку — пропускаємо публікацію")
        return None

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

import asyncio
import hashlib
import logging
from datetime import datetime
import pytz

from config import (
    MONITOR_INTERVAL_HOURS,
    MONITOR_QUIET_START,
    MONITOR_QUIET_END,
    MONITOR_MAX_PER_DAY,
    TIMEZONE,
)
from data.redis_client import (
    get as redis_get,
    get_monitor_count_today,
    increment_monitor_count,
    is_published,
    mark_published,
)
from data.fetchers import fetch_all_rss, fetch_news, fetch_github_trending
from generators.video import generate_video
from generators.ai_news import generate_ai_news
from bot.publisher import publish, notify_moderator
from utils.http_safe import safe_error_text

KYIV = pytz.timezone(TIMEZONE)
logger = logging.getLogger(__name__)


def _stable_id(prefix: str, text: str) -> str:
    """Стабільний ідентифікатор для дедуплікації (переживає рестарт процесу).

    Вбудований hash() рандомізований між запусками (PYTHONHASHSEED), тож для
    збереження в Redis потрібен детермінований хеш.
    """
    digest = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


# Мінімальний score щоб вважати новину "breaking"
BREAKING_MIN_KEYWORDS = [
    "breaking", "just in", "urgent", "exclusive",
    "ШІ", "штучний інтелект", "OpenAI", "Google", "Apple",
    "recession", "crypto", "bitcoin", "ukraine",
]


# ─────────────────────────────────────────
# ГОЛОВНИЙ ЦИКЛ МОНІТОРИНГУ
# ─────────────────────────────────────────

async def start_monitor() -> None:
    """
    Безкінечний цикл — перевіряє нові матеріали кожні N годин.
    Не працює вночі (00:00 — 07:00 Київ).
    """
    logger.info("[monitor] Запущено реалтайм моніторинг")

    while True:
        try:
            await asyncio.sleep(MONITOR_INTERVAL_HOURS * 3600)
            await run_monitor_cycle()
        except asyncio.CancelledError:
            logger.info("[monitor] Зупинено")
            break
        except Exception as e:
            logger.exception("[monitor] Помилка циклу: %s", safe_error_text(e))
            await notify_moderator(f"⚠️ Збій циклу моніторингу: {safe_error_text(e)}")
            await asyncio.sleep(60)


async def run_monitor_cycle() -> None:
    """Один цикл перевірки — викликається кожні 2 години."""

    # Перевіряємо тихий час
    if _is_quiet_time():
        return

    # Перевіряємо паузу
    paused = await redis_get("settings:paused")
    if paused:
        return

    # Перевіряємо ліміт постів на день
    count = await get_monitor_count_today()
    if count >= MONITOR_MAX_PER_DAY:
        logger.info("[monitor] Ліміт %s постів досягнуто", MONITOR_MAX_PER_DAY)
        return

    # Перевірки — ПОСЛІДОВНО, а не gather.
    # Інакше три корутини одночасно проходять перевірку ліміту й можуть
    # опублікувати більше, ніж MONITOR_MAX_PER_DAY (гонка).
    for check in (_check_video, _check_breaking_news, _check_github_trending):
        if await get_monitor_count_today() >= MONITOR_MAX_PER_DAY:
            break
        try:
            await check()
        except Exception as e:
            logger.exception(
                "[monitor] Помилка перевірки %s: %s",
                check.__name__,
                safe_error_text(e),
            )


# ─────────────────────────────────────────
# ПЕРЕВІРКИ
# ─────────────────────────────────────────

async def _check_video() -> None:
    """Шукає нове топове відео на YouTube."""
    try:
        post_data = await generate_video()
        if post_data:
            count = await get_monitor_count_today()
            if count < MONITOR_MAX_PER_DAY:
                await publish(post_data)
                await increment_monitor_count()
                logger.info("[monitor] Відео опубліковано: %s", post_data.get("topic"))
    except Exception as e:
        logger.exception("[monitor] Помилка відео: %s", safe_error_text(e))


async def _check_breaking_news() -> None:
    """Перевіряє RSS і NewsAPI на breaking news."""
    try:
        rss_items  = await fetch_all_rss(limit_per_feed=2)
        news_items = await fetch_news(query="AI technology breaking", page_size=3)
        all_items  = rss_items + news_items

        for item in all_items:
            raw_title = item.get("title", "")
            title = raw_title.lower()

            # Перевіряємо чи є ключові слова
            is_breaking = any(kw.lower() in title for kw in BREAKING_MIN_KEYWORDS)
            if not is_breaking:
                continue

            # Перевіряємо чи вже публікували (стабільний хеш — переживає рестарт)
            item_id = _stable_id("news", raw_title)
            if await is_published(item_id):
                continue

            # Публікуємо через генератор #ТехНовини
            count = await get_monitor_count_today()
            if count >= MONITOR_MAX_PER_DAY:
                return

            # Передаємо саме знайдений заголовок як фокус поста
            post_data = await generate_ai_news(focus=raw_title)
            if post_data:
                await publish(post_data)
                await mark_published(item_id)
                await increment_monitor_count()
                logger.info("[monitor] Breaking news: %s", raw_title[:60])
                return  # одна новина за цикл

    except Exception as e:
        logger.exception("[monitor] Помилка breaking news: %s", safe_error_text(e))


async def _check_github_trending() -> None:
    """Перевіряє GitHub Trending на нові вірусні репозиторії."""
    try:
        repos = await fetch_github_trending(topic="artificial-intelligence")

        for repo in repos:
            repo_id = _stable_id("github", repo["name"])
            if await is_published(repo_id):
                continue

            # Тільки якщо багато зірок (вірусний)
            if repo["stars"] < 500:
                continue

            count = await get_monitor_count_today()
            if count >= MONITOR_MAX_PER_DAY:
                return

            # Формуємо фокус про конкретний репозиторій
            focus = (
                f"Трендовий GitHub-проєкт '{repo['name']}' (⭐{repo['stars']}): "
                f"{repo.get('description') or 'опис відсутній'}"
            )

            # Публікуємо як #ТехНовини саме про цей проєкт
            post_data = await generate_ai_news(focus=focus)
            if post_data:
                await publish(post_data)
                await mark_published(repo_id)
                await increment_monitor_count()
                logger.info("[monitor] GitHub trending: %s ⭐%s", repo["name"], repo["stars"])
                return

    except Exception as e:
        logger.exception("[monitor] Помилка GitHub trending: %s", safe_error_text(e))


# ─────────────────────────────────────────
# ДОПОМІЖНІ ФУНКЦІЇ
# ─────────────────────────────────────────

def _is_quiet_time() -> bool:
    """Повертає True якщо зараз тихий час (не публікуємо)."""
    now_hour = datetime.now(KYIV).hour
    return MONITOR_QUIET_START <= now_hour < MONITOR_QUIET_END

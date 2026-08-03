import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
import pytz

from config import (
    BANKNOTE_MAX_PER_CYCLE,
    BANKNOTE_POLL_MINUTES,
    MONITOR_HOURS,
    MONITOR_MAX_PER_DAY,
    TIMEZONE,
)
from data.redis_client import (
    get as redis_get,
    get_monitor_count_today,
    increment_banknote_count,
    increment_monitor_count,
    is_published,
    mark_banknote_seen,
    mark_published,
)
from data.fetchers import fetch_all_rss, fetch_news, fetch_github_trending
from generators.video import generate_video
from generators.ai_news import generate_ai_news
from generators.banknotes import generate_banknotes
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

def _next_slot_datetime(
    now: datetime,
    last_fired: tuple[date, int] | None,
) -> datetime:
    """Наступний слот MONITOR_HOURS; у поточному слоті без last_fired — одразу."""
    hours = sorted(MONITOR_HOURS)
    for day_offset in range(0, 3):
        day = (now + timedelta(days=day_offset)).date()
        for hour in hours:
            key = (day, hour)
            if last_fired == key:
                continue
            slot = now.replace(
                year=day.year,
                month=day.month,
                day=day.day,
                hour=hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            if day_offset == 0 and hour == now.hour:
                return now
            if slot > now:
                return slot
    # fallback: перший слот через 3 дні (не має статись)
    day = (now + timedelta(days=3)).date()
    return now.replace(
        year=day.year, month=day.month, day=day.day,
        hour=hours[0], minute=0, second=0, microsecond=0,
    )


async def start_monitor() -> None:
    """
    Безкінечний цикл — перевіряє матеріали у фіксовані години
    (11:00, 14:00, 17:00, 20:00 Київ).
    """
    logger.info(
        "[monitor] Запущено реалтайм моніторинг (слоти %s Київ)",
        ", ".join(f"{h:02d}:00" for h in sorted(MONITOR_HOURS)),
    )

    last_fired: tuple[date, int] | None = None

    while True:
        try:
            now = datetime.now(KYIV)
            target = _next_slot_datetime(now, last_fired)
            wait = (target - now).total_seconds()
            if wait > 0:
                logger.info(
                    "[monitor] Наступний цикл о %s (через %.0f с)",
                    target.strftime("%H:%M"),
                    wait,
                )
                await asyncio.sleep(wait)

            now = datetime.now(KYIV)
            if now.hour not in MONITOR_HOURS:
                continue

            slot_key = (now.date(), now.hour)
            if last_fired == slot_key:
                await asyncio.sleep(30)
                continue

            await run_monitor_cycle()
            last_fired = slot_key
        except asyncio.CancelledError:
            logger.info("[monitor] Зупинено")
            break
        except Exception as e:
            logger.exception("[monitor] Помилка циклу: %s", safe_error_text(e))
            await notify_moderator(f"⚠️ Збій циклу моніторингу: {safe_error_text(e)}")
            await asyncio.sleep(60)


async def run_monitor_cycle() -> None:
    """Один цикл перевірки — у слотах MONITOR_HOURS."""

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


async def _publish_banknote_alerts() -> int:
    """Шле всі нові банкноти, що пройшли фільтр (до BANKNOTE_MAX_PER_CYCLE).

    Не чіпає MONITOR_MAX_PER_DAY — алерти колекціонера йдуть окремо.
    """
    sent = 0
    max_per_cycle = max(1, BANKNOTE_MAX_PER_CYCLE)

    while sent < max_per_cycle:
        post_data = await generate_banknotes()
        if not post_data:
            break

        await publish(post_data)
        dedupe_ids = list(post_data.get("dedupe_ids") or [])
        item_id = post_data.get("item_id")
        if item_id and item_id not in dedupe_ids:
            dedupe_ids.insert(0, item_id)
        if dedupe_ids:
            await mark_published(*dedupe_ids)
            await mark_banknote_seen(*dedupe_ids)
        await increment_banknote_count()
        sent += 1
        logger.info("[banknotes] Алерт: %s", post_data.get("topic"))

    return sent


async def start_banknote_monitor() -> None:
    """Окремий частіший цикл: нова банкнота → алерт одразу (не чекає слотів 11/14/17/20)."""
    interval = max(5, BANKNOTE_POLL_MINUTES) * 60
    logger.info(
        "[banknotes] Моніторинг кожні %s хв (до %s алертів/цикл)",
        max(5, BANKNOTE_POLL_MINUTES),
        max(1, BANKNOTE_MAX_PER_CYCLE),
    )

    # Перша перевірка одразу після старту бота.
    while True:
        try:
            paused = await redis_get("settings:paused")
            if not paused:
                sent = await _publish_banknote_alerts()
                if sent:
                    logger.info("[banknotes] Надіслано алертів за цикл: %s", sent)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("[banknotes] Моніторинг зупинено")
            break
        except Exception as e:
            logger.exception("[banknotes] Помилка циклу: %s", safe_error_text(e))
            await notify_moderator(f"⚠️ Збій моніторингу банкнот: {safe_error_text(e)}")
            await asyncio.sleep(60)

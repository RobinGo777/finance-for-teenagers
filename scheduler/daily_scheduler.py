import random
import asyncio
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    SCHEDULE,
    SCHEDULE_RANDOM_OFFSET_MIN,
    SCHEDULE_RANDOM_OFFSET_MAX,
    TIMEZONE,
    QUIZ_ANSWER_DELAY_HOURS,
)
from data.redis_client import get as redis_get
from bot.publisher import publish, notify_moderator

# Імпорти всіх генераторів
from generators.ai_news import generate_ai_news
from generators.ai_hack import generate_ai_hack
from generators.video import generate_video
from generators.crypto import generate_crypto
from generators.crime import generate_crime
from generators.careers import generate_careers
from generators.cyber import generate_cyber
from generators.money_hack import generate_money_hack
from generators.quiz import generate_quiz
from generators.cost_of_life import generate_cost_of_life
from generators.side_hustle import generate_side_hustle
from generators.game_economy import generate_game_economy
from generators.subscription_trap import generate_subscription_trap
from generators.money_myth import generate_money_myth

KYIV = pytz.timezone(TIMEZONE)
logger = logging.getLogger(__name__)

# Маппінг назв рубрик → функції генераторів
GENERATORS = {
    "ai_news":       generate_ai_news,
    "ai_hack":       generate_ai_hack,
    "video":         generate_video,
    "crypto":        generate_crypto,
    "crime":         generate_crime,
    "careers":       generate_careers,
    "cyber":         generate_cyber,
    "money_hack":    generate_money_hack,
    "quiz":          generate_quiz,
    "cost_of_life":  generate_cost_of_life,
    "side_hustle":   generate_side_hustle,
    "game_economy":  generate_game_economy,
    "subscription_trap": generate_subscription_trap,
    "money_myth":    generate_money_myth,
}


# ─────────────────────────────────────────
# ПУБЛІКАЦІЯ ОДНІЄЇ РУБРИКИ
# ─────────────────────────────────────────

async def publish_rubric(rubric_key: str) -> None:
    """Генерує і публікує один пост рубрики."""

    # Перевіряємо чи бот не на паузі
    paused = await redis_get("settings:paused")
    if paused:
        return

    generator = GENERATORS.get(rubric_key)
    if not generator:
        return

    try:
        post_data = await generator()
        if post_data:
            await publish(post_data)
        else:
            logger.info("[scheduler] Рубрика %s не дала контенту цього разу", rubric_key)
    except Exception as e:
        logger.exception("[scheduler] Помилка генерації %s: %s", rubric_key, e)
        await notify_moderator(f"⚠️ Збій генерації рубрики «{rubric_key}»: {e}")


# ─────────────────────────────────────────
# ЗАДАЧА З РАНДОМНИМ ЗСУВОМ ЧАСУ
# ─────────────────────────────────────────

async def publish_rubric_with_offset(rubric_key: str, base_hour: int, base_minute: int) -> None:
    """Додає рандомний зсув ±хвилин перед публікацією."""
    offset = random.randint(SCHEDULE_RANDOM_OFFSET_MIN, SCHEDULE_RANDOM_OFFSET_MAX)
    total_minutes = base_hour * 60 + base_minute + offset
    total_minutes = max(0, min(total_minutes, 23 * 60 + 59))

    now_minutes = datetime.now(KYIV).hour * 60 + datetime.now(KYIV).minute
    wait_seconds = max(0, (total_minutes - now_minutes) * 60)

    await asyncio.sleep(wait_seconds)
    await publish_rubric(rubric_key)


# ─────────────────────────────────────────
# НАЛАШТУВАННЯ РОЗКЛАДУ
# ─────────────────────────────────────────

def setup_scheduler() -> AsyncIOScheduler:
    """Створює і налаштовує APScheduler з усіма задачами."""

    scheduler = AsyncIOScheduler(timezone=KYIV)

    # ── ПОНЕДІЛОК 18:00 ──
    for rubric in SCHEDULE["monday"]["rubrics"]:
        scheduler.add_job(
            publish_rubric_with_offset,
            CronTrigger(day_of_week="mon", hour=18, minute=0, timezone=KYIV),
            args=[rubric, 18, 0],
            id=f"monday_{rubric}",
            replace_existing=True,
        )

    # ── ВІВТОРОК 18:30 ──
    for rubric in SCHEDULE["tuesday"]["rubrics"]:
        scheduler.add_job(
            publish_rubric_with_offset,
            CronTrigger(day_of_week="tue", hour=18, minute=30, timezone=KYIV),
            args=[rubric, 18, 30],
            id=f"tuesday_{rubric}",
            replace_existing=True,
        )

    # ── СЕРЕДА 19:00 ──
    for rubric in SCHEDULE["wednesday"]["rubrics"]:
        scheduler.add_job(
            publish_rubric_with_offset,
            CronTrigger(day_of_week="wed", hour=19, minute=0, timezone=KYIV),
            args=[rubric, 19, 0],
            id=f"wednesday_{rubric}",
            replace_existing=True,
        )

    # ── ЧЕТВЕР 18:00 ──
    for rubric in SCHEDULE["thursday"]["rubrics"]:
        scheduler.add_job(
            publish_rubric_with_offset,
            CronTrigger(day_of_week="thu", hour=18, minute=0, timezone=KYIV),
            args=[rubric, 18, 0],
            id=f"thursday_{rubric}",
            replace_existing=True,
        )

    # ── П'ЯТНИЦЯ 17:45 ──
    for rubric in SCHEDULE["friday"]["rubrics"]:
        scheduler.add_job(
            publish_rubric_with_offset,
            CronTrigger(day_of_week="fri", hour=17, minute=45, timezone=KYIV),
            args=[rubric, 17, 45],
            id=f"friday_{rubric}",
            replace_existing=True,
        )

    # ── СУБОТА 12:00 ──
    for rubric in SCHEDULE["saturday"]["rubrics"]:
        scheduler.add_job(
            publish_rubric_with_offset,
            CronTrigger(day_of_week="sat", hour=12, minute=0, timezone=KYIV),
            args=[rubric, 12, 0],
            id=f"saturday_{rubric}",
            replace_existing=True,
        )

    # ── НЕДІЛЯ 19:00 ──
    for rubric in SCHEDULE["sunday"]["rubrics"]:
        scheduler.add_job(
            publish_rubric_with_offset,
            CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=KYIV),
            args=[rubric, 19, 0],
            id=f"sunday_{rubric}",
            replace_existing=True,
        )

    # ── КІБЕРБЕЗПЕКА — 1-й і 3-й вівторок місяця о 14:00 ──
    scheduler.add_job(
        publish_rubric,
        CronTrigger(day_of_week="tue", week="1,3", hour=14, minute=0, timezone=KYIV),
        args=["cyber"],
        id="cyber_biweekly",
        replace_existing=True,
    )

    # ── ВІДПОВІДЬ НА КВІЗ — щодня о 19:10 (перевіряємо чи є pending) ──
    scheduler.add_job(
        check_and_publish_quiz_answers,
        CronTrigger(hour=19, minute=10, timezone=KYIV),
        id="quiz_answers",
        replace_existing=True,
    )

    return scheduler


# ─────────────────────────────────────────
# ВІДПОВІДЬ НА КВІЗ ЧЕРЕЗ 24 ГОД
# ─────────────────────────────────────────

async def check_and_publish_quiz_answers() -> None:
    """
    Публікує відповіді на «дозрілі» квізи (старші за QUIZ_ANSWER_DELAY_HOURS)
    разом зі статистикою голосів. Запускається щодня о 19:10.
    """
    import time
    from data.redis_client import (
        lrange,
        lrem,
        get_quiz_pending,
        get_quiz_results,
    )
    from bot.publisher import publish_quiz_answer

    # Upstash не підтримує KEYS, тому poll_id зберігаємо в окремому списку.
    pending_ids_raw = await lrange("quiz:pending_ids", 0, -1)
    min_age = QUIZ_ANSWER_DELAY_HOURS * 3600

    for poll_id in pending_ids_raw:
        try:
            pending = await get_quiz_pending(poll_id)
            if not pending:
                # Дані зникли (TTL) — прибираємо «висячий» id.
                await lrem("quiz:pending_ids", 1, poll_id)
                continue

            # Ще не дозрів — залишаємо на наступний запуск крону.
            age = time.time() - pending.get("created_at", 0)
            if age < min_age:
                continue

            results = await get_quiz_results(poll_id)
            await publish_quiz_answer(poll_id, results)  # сам очистить pending+голоси
            await lrem("quiz:pending_ids", 1, poll_id)
        except Exception as e:
            logger.exception("[scheduler] Помилка публікації відповіді квізу %s: %s", poll_id, e)
            await notify_moderator(f"⚠️ Збій публікації відповіді квізу: {e}")

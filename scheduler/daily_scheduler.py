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
    CYBER_SCHEDULE_TIME,
    QUIZ_ANSWER_CRON_TIME,
    TIMEZONE,
    QUIZ_ANSWER_DELAY_HOURS,
    GEMINI_SCHEDULE_RETRIES,
    GEMINI_SCHEDULE_RETRY_DELAY_SEC,
)
from data.redis_client import get as redis_get
from bot.publisher import publish, notify_moderator
from generators.gemini import GeminiQuotaExhausted
from utils.http_safe import safe_error_text

# Імпорти всіх генераторів
from generators.ai_news import generate_ai_news
from generators.ai_hack import generate_ai_hack
from generators.video import generate_video
from generators.crypto import generate_crypto
from generators.crime import generate_crime
from generators.careers import generate_careers
from generators.cyber import generate_cyber
from generators.quiz import generate_quiz
from generators.cost_of_life import generate_cost_of_life
from generators.side_hustle import generate_side_hustle
from generators.game_economy import generate_game_economy
from generators.subscription_trap import generate_subscription_trap
from generators.money_myth import generate_money_myth
from generators.behavioral_finance import generate_behavioral_finance
from generators.startup_week import generate_startup_week

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
    "quiz":          generate_quiz,
    "cost_of_life":  generate_cost_of_life,
    "side_hustle":   generate_side_hustle,
    "game_economy":  generate_game_economy,
    "subscription_trap": generate_subscription_trap,
    "money_myth":    generate_money_myth,
    "behavioral_finance": generate_behavioral_finance,
    "startup_week":   generate_startup_week,
}

def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_s, minute_s = value.split(":")
    return int(hour_s), int(minute_s)


def _is_rate_limit_error(error: BaseException) -> bool:
    text = str(error)
    return (
        isinstance(error, GeminiQuotaExhausted)
        or "429" in text
        or "Too Many Requests" in text
        or "RESOURCE_EXHAUSTED" in text
    )


# ─────────────────────────────────────────
# ПУБЛІКАЦІЯ ОДНІЄЇ РУБРИКИ
# ─────────────────────────────────────────

async def publish_rubric(rubric_key: str, *, attempt: int = 1) -> None:
    """Генерує і публікує один пост рубрики.

    При 429 (RPM) відкладає повтор через GEMINI_SCHEDULE_RETRY_DELAY_SEC,
    щоб не втрачати денний слот через короткочасний ліміт.
    """

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
    except GeminiQuotaExhausted as e:
        msg = str(e).lower()
        if "cooldown" in msg and attempt <= GEMINI_SCHEDULE_RETRIES:
            delay = GEMINI_SCHEDULE_RETRY_DELAY_SEC * attempt
            logger.warning(
                "[scheduler] Gemini cooldown на %s — повтор #%s через %s с",
                rubric_key,
                attempt,
                delay,
            )
            await notify_moderator(
                f"⏳ Gemini пауза — «{rubric_key}» повтор через {delay // 60} хв "
                f"(спроба {attempt}/{GEMINI_SCHEDULE_RETRIES})."
            )
            await asyncio.sleep(delay)
            await publish_rubric(rubric_key, attempt=attempt + 1)
            return
        logger.warning("[scheduler] Денний ліміт Gemini для %s: %s", rubric_key, e)
        await notify_moderator(
            f"🛑 Денний ліміт Gemini — рубрика «{rubric_key}» пропущена.\n"
            "Квота скидається ≈10:00 за Києвом (опівніч PT)."
        )
    except Exception as e:
        if _is_rate_limit_error(e) and attempt <= GEMINI_SCHEDULE_RETRIES:
            delay = GEMINI_SCHEDULE_RETRY_DELAY_SEC * attempt
            logger.warning(
                "[scheduler] 429 на %s — повтор #%s через %s с",
                rubric_key,
                attempt,
                delay,
            )
            await notify_moderator(
                f"⏳ Gemini 429 на «{rubric_key}» — повтор через {delay // 60} хв "
                f"(спроба {attempt}/{GEMINI_SCHEDULE_RETRIES})."
            )
            await asyncio.sleep(delay)
            await publish_rubric(rubric_key, attempt=attempt + 1)
            return
        logger.exception(
            "[scheduler] Помилка генерації %s: %s",
            rubric_key,
            safe_error_text(e),
        )
        await notify_moderator(
            f"⚠️ Збій генерації рубрики «{rubric_key}»: {safe_error_text(e)}"
        )


# ─────────────────────────────────────────
# ЗАДАЧА З РАНДОМНИМ ЗСУВОМ ЧАСУ
# ─────────────────────────────────────────

async def publish_rubric_with_offset(
    rubric_key: str,
    base_hour: int,
    base_minute: int,
) -> None:
    """Чекає до базового слота + рандомний зсув, потім публікує."""
    offset = random.randint(SCHEDULE_RANDOM_OFFSET_MIN, SCHEDULE_RANDOM_OFFSET_MAX)
    target_minutes = base_hour * 60 + base_minute + offset
    target_minutes = max(0, min(target_minutes, 23 * 60 + 59))

    now_minutes = datetime.now(KYIV).hour * 60 + datetime.now(KYIV).minute
    wait_seconds = max(0, (target_minutes - now_minutes) * 60)

    await asyncio.sleep(wait_seconds)
    await publish_rubric(rubric_key)


# ─────────────────────────────────────────
# НАЛАШТУВАННЯ РОЗКЛАДУ
# ─────────────────────────────────────────

_DAY_OF_WEEK = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}


def setup_scheduler() -> AsyncIOScheduler:
    """Створює і налаштовує APScheduler з усіма задачами."""

    scheduler = AsyncIOScheduler(timezone=KYIV)

    for day_key, slots in SCHEDULE.items():
        day_of_week = _DAY_OF_WEEK[day_key]
        for slot in slots:
            base_hour, base_minute = _parse_hhmm(slot["time"])
            # Крон стартує в найраніший можливий момент зсуву,
            # щоб від'ємний offset теж міг спрацювати.
            trigger_minutes = base_hour * 60 + base_minute + SCHEDULE_RANDOM_OFFSET_MIN
            trigger_minutes = max(0, min(trigger_minutes, 23 * 60 + 59))
            trigger_hour, trigger_minute = divmod(trigger_minutes, 60)

            scheduler.add_job(
                publish_rubric_with_offset,
                CronTrigger(
                    day_of_week=day_of_week,
                    hour=trigger_hour,
                    minute=trigger_minute,
                    timezone=KYIV,
                ),
                args=[slot["rubric"], base_hour, base_minute],
                id=f"{day_key}_{slot['rubric']}",
                replace_existing=True,
            )

    # ── КІБЕРБЕЗПЕКА — 1-й і 3-й вівторок місяця ──
    cyber_hour, cyber_minute = _parse_hhmm(CYBER_SCHEDULE_TIME)
    cyber_trigger = cyber_hour * 60 + cyber_minute + SCHEDULE_RANDOM_OFFSET_MIN
    cyber_trigger = max(0, min(cyber_trigger, 23 * 60 + 59))
    cyber_th, cyber_tm = divmod(cyber_trigger, 60)
    scheduler.add_job(
        publish_rubric_with_offset,
        CronTrigger(
            day="1-7,15-21",
            day_of_week="tue",
            hour=cyber_th,
            minute=cyber_tm,
            timezone=KYIV,
        ),
        args=["cyber", cyber_hour, cyber_minute],
        id="cyber_biweekly",
        replace_existing=True,
    )

    # ── ВІДПОВІДЬ НА КВІЗ — щодня (перевіряємо чи є pending) ──
    quiz_hour, quiz_minute = _parse_hhmm(QUIZ_ANSWER_CRON_TIME)
    scheduler.add_job(
        check_and_publish_quiz_answers,
        CronTrigger(hour=quiz_hour, minute=quiz_minute, timezone=KYIV),
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
    разом зі статистикою голосів. Запускається щодня (див. QUIZ_ANSWER_CRON_TIME).
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

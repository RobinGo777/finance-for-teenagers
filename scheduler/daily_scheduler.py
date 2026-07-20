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

# Пауза між рубриками одного слоту — щоб не спалити Gemini RPM (free tier).
RUBRIC_STAGGER_SEC = 150


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
    stagger_index: int = 0,
) -> None:
    """Додає рандомний зсув ±хвилин і розносить рубрики одного слоту."""
    offset = random.randint(SCHEDULE_RANDOM_OFFSET_MIN, SCHEDULE_RANDOM_OFFSET_MAX)
    total_minutes = base_hour * 60 + base_minute + offset
    total_minutes = max(0, min(total_minutes, 23 * 60 + 59))

    now_minutes = datetime.now(KYIV).hour * 60 + datetime.now(KYIV).minute
    wait_seconds = max(0, (total_minutes - now_minutes) * 60)
    wait_seconds += stagger_index * RUBRIC_STAGGER_SEC

    await asyncio.sleep(wait_seconds)
    await publish_rubric(rubric_key)


# ─────────────────────────────────────────
# НАЛАШТУВАННЯ РОЗКЛАДУ
# ─────────────────────────────────────────

def setup_scheduler() -> AsyncIOScheduler:
    """Створює і налаштовує APScheduler з усіма задачами."""

    scheduler = AsyncIOScheduler(timezone=KYIV)

    def _add_day_jobs(day_key: str, day_of_week: str, hour: int, minute: int) -> None:
        for index, rubric in enumerate(SCHEDULE[day_key]["rubrics"]):
            scheduler.add_job(
                publish_rubric_with_offset,
                CronTrigger(
                    day_of_week=day_of_week,
                    hour=hour,
                    minute=minute,
                    timezone=KYIV,
                ),
                args=[rubric, hour, minute, index],
                id=f"{day_key}_{rubric}",
                replace_existing=True,
            )

    _add_day_jobs("monday", "mon", 18, 0)
    _add_day_jobs("tuesday", "tue", 18, 30)
    _add_day_jobs("wednesday", "wed", 19, 0)
    _add_day_jobs("thursday", "thu", 18, 0)
    _add_day_jobs("friday", "fri", 17, 45)
    _add_day_jobs("saturday", "sat", 12, 0)
    _add_day_jobs("sunday", "sun", 19, 0)

    # ── КІБЕРБЕЗПЕКА — 1-й і 3-й вівторок місяця о 20:00 ──
    scheduler.add_job(
        publish_rubric,
        CronTrigger(
            day="1-7,15-21",
            day_of_week="tue",
            hour=20,
            minute=0,
            timezone=KYIV,
        ),
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

import asyncio
import json
import base64
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, PollAnswer
from aiogram.filters import Command
from bot.publisher import publish_to_channel, send_test_preview, bot
from data.redis_client import (
    get,
    delete,
    set_autopilot,
    get_autopilot,
    get_quiz_pending,
    save_quiz_vote,
    remove_quiz_vote,
)
from config import MODERATOR_CHAT_ID, SCHEDULE

router = Router()
logger = logging.getLogger(__name__)
_test_task: asyncio.Task | None = None


@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer) -> None:
    """Ловить голоси у квіз-опитуваннях (для особистих/груп із неанонімним poll).

    У каналі опитування анонімні — туди poll_answer не приходить.
    Статистику для каналу беремо через stop_poll при публікації відповіді.
    """
    poll_id = poll_answer.poll_id
    pending = await get_quiz_pending(poll_id)
    if not pending:
        return  # не квіз (напр. #ФінТруКрайм) — статистику не збираємо

    user_id = poll_answer.user.id if poll_answer.user else 0
    if poll_answer.option_ids:
        await save_quiz_vote(poll_id, user_id, poll_answer.option_ids[0])
    else:
        await remove_quiz_vote(poll_id, user_id)


def get_test_rubrics() -> list[str]:
    """Повертає всі активні рубрики один раз і в порядку тижневого розкладу."""
    from scheduler.daily_scheduler import GENERATORS

    ordered: list[str] = []
    for day in SCHEDULE.values():
        for slot in day:
            rubric = slot["rubric"]
            if rubric in GENERATORS and rubric not in ordered:
                ordered.append(rubric)
    for rubric in GENERATORS:
        if rubric not in ordered:
            ordered.append(rubric)
    return ordered


def _is_quota_error(error: Exception) -> bool:
    """Чи це помилка вичерпаної квоти Gemini (429 / RESOURCE_EXHAUSTED)."""
    text = str(error)
    return "429" in text or "Too Many Requests" in text or "RESOURCE_EXHAUSTED" in text


async def _run_test_rubrics(rubrics: list[str]) -> None:
    """Послідовно генерує рубрики та надсилає прев'ю тільки модератору."""
    global _test_task
    from scheduler.daily_scheduler import GENERATORS

    completed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    try:
        for index, rubric in enumerate(rubrics, start=1):
            await bot.send_message(
                chat_id=MODERATOR_CHAT_ID,
                text=f"⏳ Тест {index}/{len(rubrics)}: {rubric}",
            )
            try:
                post_data = await GENERATORS[rubric]()
                if not post_data:
                    skipped.append(rubric)
                    await bot.send_message(
                        chat_id=MODERATOR_CHAT_ID,
                        text=f"⚠️ {rubric}: генератор не знайшов матеріал",
                    )
                else:
                    await send_test_preview(post_data)
                    completed.append(rubric)
            except Exception as error:
                failed.append(rubric)
                logger.exception("Помилка тестової генерації %s: %s", rubric, error)
                await bot.send_message(
                    chat_id=MODERATOR_CHAT_ID,
                    text=f"❌ {rubric}: {type(error).__name__}: {error}",
                )
                # Якщо вичерпано квоту Gemini — нема сенсу молотити решту рубрик.
                if _is_quota_error(error):
                    await bot.send_message(
                        chat_id=MODERATOR_CHAT_ID,
                        text=(
                            "🛑 Досягнуто денний ліміт Gemini (429). Тест зупинено.\n"
                            "Квота скидається опівночі за тихоокеанським часом "
                            "(≈10:00 за Києвом). Спробуй пізніше або тестуй по одній "
                            "рубриці командою /test <рубрика>."
                        ),
                    )
                    break

            # Розтягуємо запити в часі, щоб не впертися в ліміт Gemini (429)
            # на безкоштовному тарифі під час тесту всіх рубрик поспіль.
            if index < len(rubrics):
                await asyncio.sleep(7)
    finally:
        summary = (
            "🏁 Тест рубрик завершено\n\n"
            f"✅ Успішно: {len(completed)}"
            f"\n⚠️ Без матеріалу: {len(skipped)}"
            f"\n❌ Помилки: {len(failed)}"
        )
        if failed:
            summary += "\n\nНе пройшли: " + ", ".join(failed)
        await bot.send_message(chat_id=MODERATOR_CHAT_ID, text=summary)
        _test_task = None


async def _start_test_run(message: Message, rubrics: list[str]) -> None:
    global _test_task
    if _test_task and not _test_task.done():
        await message.answer("⏳ Тест рубрик уже виконується. Дочекайся підсумку.")
        return

    _test_task = asyncio.create_task(_run_test_rubrics(rubrics))
    await message.answer(
        f"🧪 Запускаю тест {len(rubrics)} рубрик по черзі.\n"
        "Прев'ю прийдуть лише сюди — у канал нічого не публікується."
    )

# ─────────────────────────────────────────
# ОБРОБКА КНОПОК МОДЕРАЦІЇ
# ─────────────────────────────────────────

@router.callback_query(F.data.startswith("approve:"))
async def approve_post(callback: CallbackQuery) -> None:
    """✅ Модератор схвалив — публікуємо в канал."""
    post_id = callback.data.replace("approve:", "")

    raw = await get(post_id)
    if not raw:
        await callback.answer("❌ Пост не знайдено або вже застарів")
        return

    post_data = json.loads(raw)
    if post_data.get("image"):
        post_data["image"] = base64.b64decode(post_data["image"])
    await publish_to_channel(post_data)
    await delete(post_id)

    if callback.message.caption:
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n✅ Опубліковано!",
            reply_markup=None,
        )
    else:
        await callback.message.edit_text(
            text=callback.message.text + "\n\n✅ Опубліковано!",
            reply_markup=None,
        )
    await callback.answer("✅ Опубліковано в канал!")


@router.callback_query(F.data.startswith("reject:"))
async def reject_post(callback: CallbackQuery) -> None:
    """❌ Модератор скасував — видаляємо з черги."""
    post_id = callback.data.replace("reject:", "")
    await delete(post_id)

    if callback.message.caption:
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n❌ Скасовано",
            reply_markup=None,
        )
    else:
        await callback.message.edit_text(
            text=callback.message.text + "\n\n❌ Скасовано",
            reply_markup=None,
        )
    await callback.answer("❌ Пост скасовано")


# ─────────────────────────────────────────
# КОМАНДИ УПРАВЛІННЯ БОТОМ
# ─────────────────────────────────────────

@router.message(Command("autopilot"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_autopilot(message: Message) -> None:
    """
    /autopilot on  — вмикає автопілот
    /autopilot off — вимикає автопілот
    """
    parts = message.text.strip().split()
    if len(parts) < 2 or parts[1] not in ("on", "off"):
        current = await get_autopilot()
        status = "✅ увімкнений" if current else "❌ вимкнений"
        await message.answer(
            f"Автопілот зараз: {status}\n\n"
            f"Використання:\n/autopilot on\n/autopilot off"
        )
        return

    enabled = parts[1] == "on"
    await set_autopilot(enabled)

    if enabled:
        await message.answer(
            "✅ Автопілот увімкнений!\n"
            "Бот публікує пости автоматично без твоєї перевірки."
        )
    else:
        await message.answer(
            "❌ Автопілот вимкнений.\n"
            "Кожен пост буде надходити тобі на перевірку."
        )


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    """/myid — працює з будь-якого чату. Показує ID, щоб налаштувати MODERATOR_CHAT_ID."""
    chat_id = message.chat.id
    is_moderator = chat_id == MODERATOR_CHAT_ID
    await message.answer(
        f"🆔 Твій chat.id: <code>{chat_id}</code>\n"
        f"⚙️ MODERATOR_CHAT_ID у конфізі: <code>{MODERATOR_CHAT_ID}</code>\n"
        f"{'✅ Збігається — команди працюють' if is_moderator else '❌ Не збігається! Встав це число у MODERATOR_CHAT_ID і зроби redeploy'}",
        parse_mode="HTML",
    )


@router.message(Command("status"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_status(message: Message) -> None:
    """/status — показує поточний стан бота."""
    from data.redis_client import get_monitor_count_today
    from datetime import datetime
    import pytz

    autopilot = await get_autopilot()
    monitor_count = await get_monitor_count_today()
    kyiv_time = datetime.now(pytz.timezone("Europe/Kyiv")).strftime("%H:%M %d.%m.%Y")

    await message.answer(
        f"📊 Статус бота\n\n"
        f"🕐 Час (Київ): {kyiv_time}\n"
        f"🤖 Автопілот: {'✅ увімкнений' if autopilot else '❌ вимкнений'}\n"
        f"📡 Реалтайм постів сьогодні: {monitor_count}/4\n"
    )


@router.message(Command("pause"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_pause(message: Message) -> None:
    """/pause — тимчасово зупиняє публікації."""
    from data.redis_client import set_value as redis_set
    await redis_set("settings:paused", "1")
    await message.answer("⏸ Бот на паузі. Для продовження: /resume")


@router.message(Command("resume"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_resume(message: Message) -> None:
    """/resume — відновлює публікації після паузи."""
    from data.redis_client import delete
    await delete("settings:paused")
    await message.answer("▶️ Бот відновлено!")


@router.message(Command("test"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_test(message: Message) -> None:
    """/test crypto — генерує безпечне прев'ю однієї рубрики."""
    from scheduler.daily_scheduler import GENERATORS

    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Вкажи рубрику, наприклад: /test crypto\n\n"
            "Доступні:\n" + ", ".join(get_test_rubrics())
        )
        return

    rubric = parts[1].strip().lower()
    if rubric not in GENERATORS:
        await message.answer(
            f"❌ Невідома рубрика: {rubric}\n\n"
            "Доступні:\n" + ", ".join(get_test_rubrics())
        )
        return

    await _start_test_run(message, [rubric])


@router.message(Command("test_all"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_test_all(message: Message) -> None:
    """/test_all — тестує всі активні рубрики без публікації в канал."""
    await _start_test_run(message, get_test_rubrics())


@router.message(Command("gemini_reset"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_gemini_reset(message: Message) -> None:
    """/gemini_reset — знімає глобальну паузу Gemini після 429 / нового ключа."""
    from generators.gemini import clear_global_quota_cooldown
    from data.redis_client import delete

    await clear_global_quota_cooldown()
    await delete("video:gemini_cooldown")
    await message.answer(
        "✅ Паузу Gemini знято.\n"
        "Можеш перевірити: /test cost_of_life"
    )


@router.message(Command("help"), F.chat.id == MODERATOR_CHAT_ID)
async def cmd_help(message: Message) -> None:
    """/help — список команд."""
    await message.answer(
        "🤖 Команди управління ботом\n\n"
        "/autopilot on|off — увімк/вимк автопілот\n"
        "/status — стан бота\n"
        "/pause — пауза публікацій\n"
        "/resume — відновити публікації\n"
        "/gemini_reset — зняти паузу Gemini (після нового ключа)\n"
        "/test crypto — тест однієї рубрики\n"
        "/test_all — тест усіх рубрик лише в особисті\n"
        "/help — ця довідка"
    )

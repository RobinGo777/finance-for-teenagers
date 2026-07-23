import base64
import html
import logging
import re
import time
from aiogram import Bot
from aiogram.types import BufferedInputFile
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, MODERATOR_CHAT_ID
from data.redis_client import (
    get_autopilot,
    save_quiz_pending,
    add_quiz_pending_id,
    clear_quiz_pending,
)

logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Хештеги (#Слово) у публікаціях каналу не використовуємо.
_HASHTAG_RE = re.compile(r"(?<!\w)#[\wА-Яа-яЁёІіЇїЄєҐґ]+", re.UNICODE)


async def notify_moderator(text: str) -> None:
    """Надсилає модератору службове повідомлення (алерт про збій тощо).

    Ковтає власні помилки — алерт ніколи не повинен ламати основний потік.
    """
    try:
        await bot.send_message(
            chat_id=MODERATOR_CHAT_ID,
            text=_prepare_html(text, MESSAGE_LIMIT),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("[publisher] Не вдалося надіслати алерт модератору")

# ─────────────────────────────────────────
# ЛІМІТИ TELEGRAM
# ─────────────────────────────────────────
CAPTION_LIMIT   = 1024   # підпис до фото
MESSAGE_LIMIT   = 4096   # звичайне повідомлення
POLL_QUESTION_LIMIT = 300
POLL_OPTION_LIMIT   = 100


def _strip_hashtags(text: str) -> str:
    """Прибирає #хештеги з тексту поста, зберігаючи emoji і зміст."""
    cleaned = _HASHTAG_RE.sub("", text or "")
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in cleaned.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _prepare_html(text: str, limit: int) -> str:
    """Обрізає текст до ліміту і екранує спецсимволи для parse_mode=HTML.

    Текст від Gemini — звичайний (без розмітки), тож символи <, >, & треба
    екранувати, інакше Telegram відхилить повідомлення з помилкою парсингу.
    Ліміт рахуємо після екранування і ніколи не розриваємо HTML-сутність.
    """
    raw = (text or "").strip()
    escaped = html.escape(raw, quote=False)
    if len(escaped) <= limit:
        return escaped

    suffix = "…"
    parts: list[str] = []
    used = 0
    for char in raw:
        token = html.escape(char, quote=False)
        if used + len(token) + len(suffix) > limit:
            break
        parts.append(token)
        used += len(token)
    return "".join(parts).rstrip() + suffix


def _clean_poll_question(text: str) -> str:
    return (text or "").strip()[:POLL_QUESTION_LIMIT]


def _clean_poll_options(options: list) -> list:
    """Приводить варіанти до вимог Telegram: 2–10 штук, кожен ≤100 символів."""
    cleaned = [str(o).strip()[:POLL_OPTION_LIMIT] for o in (options or []) if str(o).strip()]
    return cleaned[:10]


def _split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Ділить довгий текст на Telegram-повідомлення, намагаючись різати по абзацах."""
    remaining = (text or "").strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def _split_caption(text: str, first_limit: int = 1000) -> tuple[str, list[str]]:
    """Ділить текст на підпис до фото (≤first_limit) і решту як окремі повідомлення.

    Так довгі пости не втрачають кінцівку (CTA/дисклеймер), яку раніше
    обрізав ліміт підпису 1024. first_limit < 1024 — буфер під HTML-екранування.
    """
    chunks = _split_message(text, MESSAGE_LIMIT)
    if not chunks:
        return "", []
    first = chunks[0]
    if len(first) <= first_limit:
        return first, chunks[1:]
    head = _split_message(first, first_limit)
    return head[0], head[1:] + chunks[1:]


async def _send_photo_with_text(chat_id, photo, text: str) -> int:
    """Надсилає фото з підписом; надлишок тексту — окремими повідомленнями."""
    caption, rest = _split_caption(text)
    msg = await bot.send_photo(
        chat_id=chat_id,
        photo=photo,
        caption=_prepare_html(caption, CAPTION_LIMIT),
        parse_mode="HTML",
    )
    for chunk in rest:
        await bot.send_message(
            chat_id=chat_id,
            text=_prepare_html(chunk, MESSAGE_LIMIT),
            parse_mode="HTML",
        )
    return msg.message_id


def _test_preview_text(post_data: dict) -> str:
    """Формує повний текст тестового прев'ю для звичайного поста або квізу."""
    if post_data.get("rubric") != "quiz":
        text = _strip_hashtags(post_data.get("post", "") or "(генератор не повернув текст поста)")
        poll_options = _clean_poll_options(post_data.get("poll_options", []))
        if poll_options:
            text += "\n\nТестове опитування:\n" + "\n".join(
                f"{index + 1}. {option}"
                for index, option in enumerate(poll_options)
            )
        return text

    options = _clean_poll_options(post_data.get("options", []))
    correct_index = post_data.get("correct_index", 0)
    answer = (
        options[correct_index]
        if isinstance(correct_index, int) and 0 <= correct_index < len(options)
        else "невідомо"
    )
    return (
        f"Питання: {post_data.get('question', '')}\n\n"
        + "\n".join(f"{index + 1}. {option}" for index, option in enumerate(options))
        + f"\n\n✅ Правильна відповідь: {answer}\n\n"
        + _strip_hashtags(post_data.get("lamp_post", ""))
    ).strip()


def _moderation_body(post_data: dict) -> str:
    """Текст для картки модерації: для квізу показує питання + варіанти."""
    if post_data.get("rubric") == "quiz":
        options = _clean_poll_options(post_data.get("options", []))
        correct_index = post_data.get("correct_index", 0)
        answer = (
            options[correct_index]
            if isinstance(correct_index, int) and 0 <= correct_index < len(options)
            else "невідомо"
        )
        lines = [f"❓ {post_data.get('question', '')}", ""]
        lines += [f"{i + 1}. {opt}" for i, opt in enumerate(options)]
        lines += ["", f"✅ Правильна: {answer}"]
        return "\n".join(lines).strip()

    text = _strip_hashtags(post_data.get("post", "") or "(генератор не повернув текст поста)")
    poll_options = _clean_poll_options(post_data.get("poll_options", []))
    if poll_options:
        text += "\n\nОпитування:\n" + "\n".join(
            f"{i + 1}. {opt}" for i, opt in enumerate(poll_options)
        )
    return text


async def send_test_preview(post_data: dict) -> None:
    """
    Надсилає безпечне тестове прев'ю лише модератору.

    Нічого не зберігає в Redis і не створює кнопки публікації, тому тест
    неможливо випадково відправити в канал.
    """
    rubric = post_data.get("rubric", "unknown")
    header = (
        "🧪 ТЕСТОВЕ ПРЕВ'Ю\n"
        f"Рубрика: {rubric}\n"
        f"Тема: {post_data.get('topic', '—')}\n"
        f"Персона: {post_data.get('persona', '—')}\n"
        f"Шаблон: {post_data.get('template', '—')}"
    )

    image = post_data.get("image")
    image_url = post_data.get("image_url")
    if image:
        await bot.send_photo(
            chat_id=MODERATOR_CHAT_ID,
            photo=BufferedInputFile(image, filename=f"test-{rubric}.png"),
            caption=header,
        )
    elif image_url:
        await bot.send_photo(
            chat_id=MODERATOR_CHAT_ID,
            photo=image_url,
            caption=header,
        )
    else:
        await bot.send_message(chat_id=MODERATOR_CHAT_ID, text=header)

    for chunk in _split_message(_test_preview_text(post_data)):
        await bot.send_message(chat_id=MODERATOR_CHAT_ID, text=chunk)

    if rubric == "quiz":
        question = _clean_poll_question(post_data.get("question", ""))
        options = _clean_poll_options(post_data.get("options", []))
        if question and len(options) >= 2:
            await bot.send_poll(
                chat_id=MODERATOR_CHAT_ID,
                question=f"🧪 {question}"[:POLL_QUESTION_LIMIT],
                options=options,
                is_anonymous=True,
            )
    elif post_data.get("poll_options"):
        options = _clean_poll_options(post_data.get("poll_options", []))
        if len(options) >= 2:
            await bot.send_poll(
                chat_id=MODERATOR_CHAT_ID,
                question="🧪 Який факт — брехня шахрая?",
                options=options,
                is_anonymous=True,
            )

# ─────────────────────────────────────────
# ГОЛОВНА ФУНКЦІЯ ПУБЛІКАЦІЇ
# ─────────────────────────────────────────

async def publish(post_data: dict) -> None:
    """
    Головна функція. Перевіряє autopilot:
    - ON  → публікує одразу в канал
    - OFF → відправляє тобі на модерацію
    """
    if not post_data:
        return

    autopilot = await get_autopilot()

    if autopilot:
        await publish_to_channel(post_data)
    else:
        await send_to_moderator(post_data)


# ─────────────────────────────────────────
# ПУБЛІКАЦІЯ В КАНАЛ
# ─────────────────────────────────────────

async def publish_to_channel(post_data: dict) -> int | None:
    """
    Публікує пост в Telegram канал.
    Повертає message_id опублікованого поста.
    """
    rubric   = post_data.get("rubric", "")
    text     = _strip_hashtags(post_data.get("post", ""))
    image    = post_data.get("image")          # bytes (Pillow)
    image_url = post_data.get("image_url")     # str (YouTube thumbnail)

    # ── Квіз → Telegram Poll ──
    if rubric == "quiz":
        return await _publish_quiz(post_data)

    # ── Пост з опитуванням (#ФінТруКрайм) ──
    if post_data.get("poll_options"):
        return await _publish_with_poll(post_data)

    # ── Пост з картинкою (Pillow bytes) ──
    if image:
        photo = BufferedInputFile(image, filename="post.png")
        return await _send_photo_with_text(TELEGRAM_CHANNEL_ID, photo, text)

    # ── Пост з YouTube thumbnail ──
    if image_url:
        return await _send_photo_with_text(TELEGRAM_CHANNEL_ID, image_url, text)

    # ── Текстовий пост ──
    msg = await bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=_prepare_html(text, MESSAGE_LIMIT),
        parse_mode="HTML",
    )
    return msg.message_id


async def _publish_quiz(post_data: dict) -> int | None:
    """Публікує квіз як Telegram Poll + зберігає дані для відповіді через 24 год."""

    question = _clean_poll_question(post_data.get("question", ""))
    options  = _clean_poll_options(post_data.get("options", []))

    # Telegram вимагає щонайменше 2 варіанти
    if len(options) < 2:
        return None

    # Спочатку картинка
    image = post_data.get("image")
    if image:
        photo = BufferedInputFile(image, filename="quiz.png")
        await bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_ID,
            photo=photo,
            caption=_prepare_html(f"🧠 ФінКвіз\n\n{post_data.get('question', '')}", CAPTION_LIMIT),
            parse_mode="HTML",
        )

    # Потім опитування.
    # У каналах Telegram дозволяє лише анонімні опитування.
    # Голоси збираємо через stop_poll при публікації відповіді (~20 год).
    msg = await bot.send_poll(
        chat_id=TELEGRAM_CHANNEL_ID,
        question=question,
        options=options,
        is_anonymous=True,
        allows_multiple_answers=False,
    )

    # Зберігаємо в Redis для відповіді через ~24 год.
    # created_at потрібен, щоб крон публікував відповідь лише коли квіз «дозрів».
    await save_quiz_pending(msg.poll.id, {
        "correct_index": post_data.get("correct_index", 0),
        "lamp_post": post_data.get("lamp_post", ""),
        "message_id": msg.message_id,
        "created_at": time.time(),
    })
    await add_quiz_pending_id(msg.poll.id)

    return msg.message_id


async def _publish_with_poll(post_data: dict) -> int | None:
    """Публікує пост з картинкою і окремим опитуванням (#ФінТруКрайм)."""

    image = post_data.get("image")
    text  = post_data.get("post", "")

    options = _clean_poll_options(post_data.get("poll_options", []))
    if len(options) < 2:
        # Без валідного опитування публікуємо просто як пост з картинкою
        if image:
            photo = BufferedInputFile(image, filename="post.png")
            return await _send_photo_with_text(TELEGRAM_CHANNEL_ID, photo, text)
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=_prepare_html(text, MESSAGE_LIMIT),
            parse_mode="HTML",
        )
        return msg.message_id

    # Пост з картинкою
    if image:
        photo = BufferedInputFile(image, filename="post.png")
        await _send_photo_with_text(TELEGRAM_CHANNEL_ID, photo, text)

    # Опитування під постом (у каналі — лише анонімне).
    msg = await bot.send_poll(
        chat_id=TELEGRAM_CHANNEL_ID,
        question=_clean_poll_question("Який факт — брехня шахрая?"),
        options=options,
        is_anonymous=True,
    )

    return msg.message_id


async def publish_quiz_answer(poll_id: str, poll_results: dict) -> None:
    """Публікує 💡 відповідь на квіз через 24 год."""
    from generators.quiz import generate_quiz_answer
    from data.redis_client import get_quiz_pending

    # Анонімні опитування в каналі не дають poll_answer — беремо підсумки через stop_poll.
    results = dict(poll_results or {})
    pending = await get_quiz_pending(poll_id)
    if pending and pending.get("message_id"):
        try:
            stopped = await bot.stop_poll(
                chat_id=TELEGRAM_CHANNEL_ID,
                message_id=int(pending["message_id"]),
            )
            results = {
                str(i): int(opt.voter_count)
                for i, opt in enumerate(stopped.options)
            }
        except Exception as e:
            logger.warning(
                "[publisher] stop_poll для квізу %s не вдався: %s",
                poll_id,
                e,
            )

    lamp_post = await generate_quiz_answer(poll_id, results)
    if lamp_post:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=_prepare_html(_strip_hashtags(lamp_post), MESSAGE_LIMIT),
            parse_mode="HTML",
        )
        await clear_quiz_pending(poll_id)


# ─────────────────────────────────────────
# МОДЕРАЦІЯ (human-in-loop)
# ─────────────────────────────────────────

async def send_to_moderator(post_data: dict) -> None:
    """
    Надсилає пост тобі в особисті для перевірки.
    Три кнопки: ✅ Опублікувати / ✏️ Редагувати / ❌ Скасувати
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from data.redis_client import set_value as redis_set
    import json
    import time

    rubric  = post_data.get("rubric", "unknown")
    text    = post_data.get("post", "")
    image   = post_data.get("image")
    persona = post_data.get("persona", "")
    tmpl    = post_data.get("template", "")

    # Зберігаємо пост в Redis на 24 год
    post_id = f"pending:{rubric}:{int(time.time())}"
    image_b64 = base64.b64encode(image).decode("ascii") if image else None
    await redis_set(post_id, json.dumps({
        **post_data,
        "image": image_b64,
    }), ex=86400)

    # Клавіатура
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"approve:{post_id}"),
        InlineKeyboardButton(text="❌ Скасувати",    callback_data=f"reject:{post_id}"),
    ]])

    # Для квізу поля "post" немає — показуємо питання, варіанти й правильну
    # відповідь, щоб модератор бачив, що саме публікується.
    body = _moderation_body(post_data)
    caption = (
        f"📋 Новий пост на модерацію\n\n"
        f"Рубрика: {rubric}\n"
        f"Персона: {persona}\n"
        f"Шаблон: {tmpl}\n\n"
        f"─────────────────\n"
        f"{body[:800]}{'...' if len(body) > 800 else ''}"
    )

    if image:
        photo = BufferedInputFile(image, filename="preview.png")
        await bot.send_photo(
            chat_id=MODERATOR_CHAT_ID,
            photo=photo,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        await bot.send_message(
            chat_id=MODERATOR_CHAT_ID,
            text=caption,
            reply_markup=keyboard,
        )

    # Прев'ю самого опитування, щоб модератор бачив інтерактив (анонімно,
    # щоб не плутати зі справжніми голосами в каналі).
    if rubric == "quiz":
        q = _clean_poll_question(post_data.get("question", ""))
        opts = _clean_poll_options(post_data.get("options", []))
        if q and len(opts) >= 2:
            await bot.send_poll(
                chat_id=MODERATOR_CHAT_ID,
                question=f"👀 Прев'ю: {q}"[:POLL_QUESTION_LIMIT],
                options=opts,
                is_anonymous=True,
            )
    elif post_data.get("poll_options"):
        opts = _clean_poll_options(post_data.get("poll_options", []))
        if len(opts) >= 2:
            await bot.send_poll(
                chat_id=MODERATOR_CHAT_ID,
                question="👀 Прев'ю: Який факт — брехня шахрая?",
                options=opts,
                is_anonymous=True,
            )

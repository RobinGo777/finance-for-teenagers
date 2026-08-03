"""Рубрика #НоваБанкнота — лише свіжі / ювілейні випуски, опис українською."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from data.banknotes import (
    fetch_new_banknotes,
    issue_fingerprint,
    title_fingerprint,
    titles_too_similar,
)
from data.redis_client import (
    can_use_optional_gemini,
    get_banknote_count_today,
    get_used_topics,
    is_banknote_seen,
    is_published,
    mark_banknote_seen,
    mark_published,
    record_optional_gemini_use,
    save_topic,
)
from generators.gemini import generate_json, is_quota_paused, GeminiQuotaExhausted
from images.generator import generate_post_image_async
from config import (
    BANKNOTE_MAX_PER_DAY,
    BANKNOTE_USE_SEARCH,
    GEMINI_OPTIONAL_MAX_PER_DAY,
    VISUAL_TEMPLATES,
)
from utils.http_safe import safe_error_text

logger = logging.getLogger(__name__)

RUBRIC_KEY = "banknotes"
RUBRIC_NAME = "#НоваБанкнота"
RUBRIC_HASHTAG = "💵 НоваБанкнота"


def _item_id(item: dict) -> str:
    raw = (item.get("url") or item.get("title") or "").strip().lower()
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return f"banknote:{digest}"


def _collect_ids(item: dict, data: dict | None = None) -> list[str]:
    """Усі ключі дедупу для однієї банкноти (URL + заголовок + випуск)."""
    ids = [_item_id(item)]
    title_fp = title_fingerprint(item.get("title", ""), item.get("summary", ""))
    if title_fp:
        ids.append(title_fp)
    if data:
        issue_fp = issue_fingerprint(
            data.get("country", ""),
            data.get("denomination", ""),
            data.get("issue_type", ""),
        )
        if issue_fp:
            ids.append(issue_fp)
        topic = (data.get("topic") or "").strip()
        if topic:
            topic_fp = title_fingerprint(topic)
            if topic_fp:
                ids.append(topic_fp)
    # Унікальні, зі збереженням порядку.
    seen: set[str] = set()
    out: list[str] = []
    for value in ids:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


async def _already_sent(item: dict, used_topics: list[str]) -> bool:
    """True, якщо цю банкноту вже відправляли (URL / відбиток / схожий заголовок)."""
    for key in _collect_ids(item):
        if await is_published(key) or await is_banknote_seen(key):
            return True

    title = item.get("title") or ""
    for prev in used_topics:
        if prev and titles_too_similar(title, prev):
            return True
    return False


async def _pick_fresh_item() -> dict | None:
    """Перший непропублікований кандидат з достатнім score."""
    candidates = await fetch_new_banknotes()
    used = await get_used_topics(RUBRIC_KEY)

    for item in candidates:
        if await _already_sent(item, used):
            logger.info(
                "[banknotes] пропуск дубліката: %s",
                (item.get("title") or "")[:60],
            )
            continue
        return item
    return None


async def generate_banknotes() -> dict | None:
    """
    Готує пост про нову/ювілейну банкноту українською.
    Повертає None, якщо свіжих випусків немає (щоб не спамити).
    """
    if BANKNOTE_MAX_PER_DAY > 0:
        if await get_banknote_count_today() >= BANKNOTE_MAX_PER_DAY:
            logger.info("[banknotes] денний ліміт %s досягнуто", BANKNOTE_MAX_PER_DAY)
            return None

    if await is_quota_paused():
        logger.info("[banknotes] Глобальна пауза Gemini — пропуск")
        return None

    if not await can_use_optional_gemini(GEMINI_OPTIONAL_MAX_PER_DAY):
        logger.info(
            "[banknotes] Опційний бюджет Gemini вичерпано (%s/день) — лишаємо квоту на розклад",
            GEMINI_OPTIONAL_MAX_PER_DAY,
        )
        return None

    item = await _pick_fresh_item()
    if not item:
        logger.info("[banknotes] немає свіжих кандидатів")
        return None

    source_block = (
        f"Заголовок: {item['title']}\n"
        f"Коротко: {item.get('summary') or '—'}\n"
        f"Джерело: {item.get('source') or '—'}\n"
        f"Посилання: {item.get('url') or '—'}\n"
        f"Дата: {item.get('published') or '—'}"
    )

    search_hint = (
        "Можеш уточнити факти через пошук, але не вигадуй номінал, країну чи дату."
        if BANKNOTE_USE_SEARCH
        else "Не вигадуй фактів: пиши лише те, що є в даних нижче."
    )

    prompt = f"""Ти пишеш коротке повідомлення українською для колекціонера банкнот (боністика).

Завдання: описати ЛИШЕ цей свіжий або ювілейний випуск банкноти за даними нижче.
{search_hint}

ДАНІ:
{source_block}

Правила:
- мова: українська;
- лише нові серії, нові номінали або ювілейні/пам'ятні випуски;
- не пиши про аукціони, ціни на вторинному ринку, «скільки коштує рідкісна»;
- без кліше й реклами; тон діловий, коротко;
- якщо це НЕ новий/ювілейний випуск (а каталог, продаж, огляд старої банкноти) — поверни skip=true.

ФОРМАТ ВІДПОВІДІ (тільки JSON):
{{
  "skip": false,
  "topic": "країна + номінал + тип випуску (до 8 слів)",
  "title": "заголовок для картинки (макс 8 слів українською)",
  "country": "країна англійською або українською (стабільна назва)",
  "denomination": "номінал з валютою, напр. 20 EUR",
  "issue_type": "нова серія | ювілейна | пам'ятна | новий номінал",
  "post": "{RUBRIC_HASHTAG} | {datetime.now().strftime('%d.%m.%Y')}\\n\\n[emoji] [1 речення-зачіпка]\\n\\n🌍 Країна: ...\\n💵 Номінал: ...\\n🏷 Тип: ...\\n📅 Що відомо про випуск: 1-2 речення\\n🎨 Дизайн / особливості: 1-2 речення (якщо відомо)\\n\\n🔗 [посилання на джерело якщо є]\\n\\n💬 Чи варто брати в колекцію?",
  "body_preview": "1 коротке речення для картинки без емодзі"
}}
"""

    try:
        data = await generate_json(prompt, use_search=BANKNOTE_USE_SEARCH)
    except GeminiQuotaExhausted as exc:
        await record_optional_gemini_use()
        logger.warning("[banknotes] Gemini квота: %s", safe_error_text(exc))
        return None
    except Exception as exc:
        await record_optional_gemini_use()
        logger.warning("[banknotes] Gemini помилка: %s", safe_error_text(exc))
        return None

    await record_optional_gemini_use()
    dedupe_ids = _collect_ids(item, data)

    if data.get("skip"):
        # Позначаємо, щоб не крутити той самий шум знову.
        await mark_published(*dedupe_ids)
        await mark_banknote_seen(*dedupe_ids)
        logger.info("[banknotes] Gemini відхилив як не-свіжий: %s", item["title"][:60])
        return None

    # Після Gemini ще раз: країна+номінал могли збігтися з уже надісланою банкнотою.
    for key in dedupe_ids:
        if await is_published(key) or await is_banknote_seen(key):
            logger.info(
                "[banknotes] дублікат після розбору Gemini (%s): %s",
                key,
                item["title"][:50],
            )
            await mark_published(*dedupe_ids)
            await mark_banknote_seen(*dedupe_ids)
            return None

    topic = (data.get("topic") or item["title"])[:80]
    used = await get_used_topics(RUBRIC_KEY)
    for prev in used:
        if prev and titles_too_similar(topic, prev):
            logger.info("[banknotes] схожа тема вже була: %s ≈ %s", topic, prev)
            await mark_published(*dedupe_ids)
            await mark_banknote_seen(*dedupe_ids)
            return None

    post = data.get("post") or ""
    if item.get("url") and item["url"] not in post:
        post = post.rstrip() + f"\n\n🔗 {item['url']}"

    template = next(
        (t for t in VISUAL_TEMPLATES if t["name"] == "Cool Data"),
        VISUAL_TEMPLATES[0],
    )

    image_url = (item.get("image_url") or "").strip()
    image_bytes = None
    if not image_url:
        image_bytes = await generate_post_image_async(
            title=data.get("title", RUBRIC_NAME),
            body=data.get("body_preview", ""),
            rubric=RUBRIC_HASHTAG,
            persona_name="Колекціонер",
            template=template,
        )

    await save_topic(RUBRIC_KEY, topic)

    result = {
        "rubric": RUBRIC_KEY,
        "topic": topic,
        "post": post,
        "persona": "Колекціонер",
        "template": template["name"],
        "source_url": item.get("url") or "",
        "item_id": dedupe_ids[0],
        "dedupe_ids": dedupe_ids,
    }
    if image_url:
        result["image_url"] = image_url
    else:
        result["image"] = image_bytes
    return result

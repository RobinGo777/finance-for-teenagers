import json
from datetime import datetime

import httpx
import pytz
from config import UPSTASH_REDIS_URL, UPSTASH_REDIS_TOKEN, MAX_USED_TOPICS_IN_PROMPT, TIMEZONE

# ─────────────────────────────────────────
# Базовий клієнт Upstash Redis (REST API)
# ─────────────────────────────────────────

HEADERS = {
    "Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}",
    "Content-Type": "application/json",
}

# Спільний HTTP-клієнт (пул з'єднань) — створюється лениво.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def _request(command: list) -> dict:
    """Виконує Redis команду через Upstash REST API."""
    client = _get_client()
    response = await client.post(
        UPSTASH_REDIS_URL,
        headers=HEADERS,
        json=command,
    )
    response.raise_for_status()
    return response.json()


async def close() -> None:
    """Закриває спільний HTTP-клієнт (викликати при зупинці бота)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ─────────────────────────────────────────
# БАЗОВІ ОПЕРАЦІЇ
# ─────────────────────────────────────────

async def get(key: str) -> str | None:
    result = await _request(["GET", key])
    return result.get("result")


async def set_value(key: str, value: str, ex: int = None) -> bool:
    """Зберігає значення. ex — TTL в секундах (необов'язково)."""
    cmd = ["SET", key, value]
    if ex:
        cmd += ["EX", ex]
    result = await _request(cmd)
    return result.get("result") == "OK"


async def delete(key: str) -> bool:
    result = await _request(["DEL", key])
    return result.get("result", 0) > 0


async def incr(key: str) -> int:
    result = await _request(["INCR", key])
    return result.get("result", 0)


# ─────────────────────────────────────────
# SET ОПЕРАЦІЇ (для унікальних тем)
# ─────────────────────────────────────────

async def sadd(key: str, *values: str) -> int:
    """Додає значення в Set."""
    result = await _request(["SADD", key, *values])
    return result.get("result", 0)


async def smembers(key: str) -> set:
    """Повертає всі елементи Set."""
    result = await _request(["SMEMBERS", key])
    return set(result.get("result", []))


async def sismember(key: str, value: str) -> bool:
    """Перевіряє чи є значення в Set."""
    result = await _request(["SISMEMBER", key, value])
    return result.get("result", 0) == 1


# ─────────────────────────────────────────
# LIST ОПЕРАЦІЇ (для черги постів)
# ─────────────────────────────────────────

async def lpush(key: str, value: str) -> int:
    result = await _request(["LPUSH", key, value])
    return result.get("result", 0)


async def rpop(key: str) -> str | None:
    result = await _request(["RPOP", key])
    return result.get("result")


async def lrange(key: str, start: int = 0, end: int = -1) -> list:
    result = await _request(["LRANGE", key, start, end])
    return result.get("result", [])


async def lrem(key: str, count: int, value: str) -> int:
    result = await _request(["LREM", key, count, value])
    return result.get("result", 0)


async def ltrim(key: str, start: int, end: int) -> bool:
    result = await _request(["LTRIM", key, start, end])
    return result.get("result") == "OK"


# ─────────────────────────────────────────
# HASH ОПЕРАЦІЇ (для збереження даних квізу)
# ─────────────────────────────────────────

async def hset(key: str, field: str, value: str) -> int:
    result = await _request(["HSET", key, field, value])
    return result.get("result", 0)


async def hget(key: str, field: str) -> str | None:
    result = await _request(["HGET", key, field])
    return result.get("result")


async def hgetall(key: str) -> dict:
    result = await _request(["HGETALL", key])
    raw = result.get("result", [])
    # Upstash повертає плоский список [key, val, key, val...]
    return dict(zip(raw[::2], raw[1::2])) if raw else {}


async def hdel(key: str, field: str) -> int:
    result = await _request(["HDEL", key, field])
    return result.get("result", 0)


# ─────────────────────────────────────────
# EXPIRE
# ─────────────────────────────────────────

async def expire(key: str, seconds: int) -> bool:
    result = await _request(["EXPIRE", key, seconds])
    return result.get("result", 0) == 1


# ─────────────────────────────────────────
# ДОПОМІЖНІ ФУНКЦІЇ ДЛЯ БОТА
# ─────────────────────────────────────────

async def get_used_topics(rubric_key: str) -> list:
    """Повертає останні використані теми рубрики (макс MAX_USED_TOPICS_IN_PROMPT).

    Зберігаємо як обмежений список (найновіші — першими), щоб набір не ріс
    безмежно і в промпт потрапляли саме свіжі теми, а не випадкові.
    """
    topics = await lrange(f"{rubric_key}:used_topics", 0, MAX_USED_TOPICS_IN_PROMPT - 1)
    # Прибираємо можливі дублікати, зберігаючи порядок.
    seen: set = set()
    unique = []
    for t in topics:
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


async def save_topic(rubric_key: str, topic: str) -> None:
    """Зберігає використану тему рубрики в обмежений список (з обрізанням)."""
    if not topic:
        return
    key = f"{rubric_key}:used_topics"
    await lpush(key, topic)
    # Тримаємо трохи більше, ніж потрібно для промпту (буфер під дублікати).
    await ltrim(key, 0, MAX_USED_TOPICS_IN_PROMPT * 2 - 1)


async def get_autopilot() -> bool:
    """Перевіряє чи увімкнений автопілот."""
    value = await get("settings:autopilot")
    return value == "on"


async def set_autopilot(enabled: bool) -> None:
    """Вмикає або вимикає автопілот."""
    await set_value("settings:autopilot", "on" if enabled else "off")


async def get_last_template() -> list:
    """Повертає останні 2 використаних шаблони (щоб не повторювати)."""
    value = await get("settings:last_templates")
    return json.loads(value) if value else []


async def save_last_template(template_name: str) -> None:
    """Зберігає останні 2 шаблони."""
    last = await get_last_template()
    last.append(template_name)
    await set_value("settings:last_templates", json.dumps(last[-2:]))


def _monitor_count_key() -> str:
    """Ключ лічильника з датою за Києвом — авто-скидання рівно опівночі.

    Раніше ключ був спільний з TTL 86400, тож лічильник обнулявся через 24 год
    від першого поста, а не о півночі. Датовий ключ вирішує це.
    """
    today = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
    return f"monitor:daily_count:{today}"


async def get_monitor_count_today() -> int:
    """Скільки реалтайм постів опубліковано сьогодні (за київською датою)."""
    value = await get(_monitor_count_key())
    return int(value) if value else 0


async def increment_monitor_count() -> None:
    """+1 до лічильника реалтайм постів. Ключ прив'язаний до дати."""
    key = _monitor_count_key()
    await incr(key)
    await expire(key, 172800)  # 2 доби — щоб ключ сам прибрався


async def is_published(item_id: str) -> bool:
    """Перевіряє чи вже публікували цей пост/відео."""
    return await sismember("monitor:published_ids", item_id)


async def mark_published(item_id: str) -> None:
    """Позначає пост як опублікований."""
    await sadd("monitor:published_ids", item_id)


async def save_quiz_pending(poll_id: str, data: dict) -> None:
    """Зберігає дані квізу для відповіді через 24 год."""
    await set_value(f"quiz:pending:{poll_id}", json.dumps(data), ex=172800)  # 48 год


async def add_quiz_pending_id(poll_id: str) -> None:
    """Додає poll_id у список pending квізів."""
    await lpush("quiz:pending_ids", poll_id)


async def get_quiz_pending(poll_id: str) -> dict | None:
    """Витягує дані квізу за poll_id."""
    value = await get(f"quiz:pending:{poll_id}")
    return json.loads(value) if value else None


async def clear_quiz_pending(poll_id: str) -> None:
    """Видаляє pending-дані, голоси та poll_id зі списку."""
    await delete(f"quiz:pending:{poll_id}")
    await delete(f"quiz:votes:{poll_id}")
    await lrem("quiz:pending_ids", 0, poll_id)


# ─────────────────────────────────────────
# ГОЛОСИ КВІЗУ (для статистики «X% відповіли правильно»)
# ─────────────────────────────────────────
# Зберігаємо останній вибір кожного користувача: user_id -> option_index.
# Так коректно враховуємо зміну голосу і рахуємо підсумок при відповіді.

async def save_quiz_vote(poll_id: str, user_id: int, option_index: int) -> None:
    await hset(f"quiz:votes:{poll_id}", str(user_id), str(option_index))
    await expire(f"quiz:votes:{poll_id}", 172800)  # 2 доби


async def remove_quiz_vote(poll_id: str, user_id: int) -> None:
    """Користувач відкликав голос."""
    await hdel(f"quiz:votes:{poll_id}", str(user_id))


async def get_quiz_results(poll_id: str) -> dict:
    """Повертає підсумок голосів: {option_index(str): кількість}."""
    raw = await hgetall(f"quiz:votes:{poll_id}")
    results: dict = {}
    for option_index in raw.values():
        results[option_index] = results.get(option_index, 0) + 1
    return results


async def clear_weekly_topics() -> None:
    """Очищає список тем тижня (запускається в неділю для дайджесту)."""
    await delete("weekly:posts")


async def add_weekly_topic(topic: str) -> None:
    """Додає тему до списку тижня для дайджесту."""
    await lpush("weekly:posts", topic)


async def get_weekly_topics() -> list:
    """Повертає всі теми тижня."""
    return await lrange("weekly:posts")


# ─────────────────────────────────────────
# СТОКОВІ ФОТО (дедуплікація)
# ─────────────────────────────────────────

async def is_photo_used(photo_id: str) -> bool:
    """Чи вже використовували це фото нещодавно."""
    if not photo_id:
        return False
    return bool(await get(f"photos:used:{photo_id}"))


async def mark_photo_used(photo_id: str, days: int = 14) -> None:
    """Позначає фото як використане (TTL у днях)."""
    if not photo_id:
        return
    await set_value(f"photos:used:{photo_id}", "1", ex=max(1, days) * 86400)


async def filter_unused_photo_ids(photo_ids: list[str]) -> set[str]:
    """Повертає підмножину id, які вже в дедуп-кеші."""
    used: set[str] = set()
    for photo_id in photo_ids:
        if photo_id and await is_photo_used(photo_id):
            used.add(photo_id)
    return used

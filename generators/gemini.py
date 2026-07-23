import asyncio
import json
import logging
import random
import time
import httpx
from config import (
    GEMINI_API_KEY,
    GEMINI_MODELS,
    GEMINI_MIN_INTERVAL_SEC,
    GEMINI_MAX_RETRY_WAIT_SEC,
    GEMINI_GLOBAL_COOLDOWN_SEC,
    PERSONAS,
    VISUAL_TEMPLATES,
)
from data.redis_client import get as redis_get, set_value as redis_set

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# БАЗОВИЙ КЛІЄНТ GEMINI API
# ─────────────────────────────────────────

# Кількість спроб для кожної моделі перед переходом до наступної.
MODEL_RETRIES = 3
JSON_MODEL_RETRIES = 2  # 503/мережа — 2 спроби, без зайвого каскаду
RETRY_BASE_DELAY = 4  # секунди (експоненційний backoff)

_GEMINI_GLOBAL_COOLDOWN_KEY = "gemini:quota_cooldown"

# Спільний HTTP-клієнт (пул з'єднань) — створюється лениво.
_client: httpx.AsyncClient | None = None
# Один запит за раз + пауза між викликами — щоб не спалити RPM free tier.
_request_lock = asyncio.Lock()
_cooldown_until = 0.0
_last_request_at = 0.0


class GeminiQuotaExhausted(Exception):
    """Денний ліміт / тривалий 429 — сенсу одразу ретраїти немає."""


async def is_quota_paused() -> bool:
    """True якщо глобальна пауза Gemini активна (Redis)."""
    return bool(await redis_get(_GEMINI_GLOBAL_COOLDOWN_KEY))


async def clear_global_quota_cooldown() -> None:
    """Знімає глобальну паузу Gemini (модератор / після оновлення ключа)."""
    from data.redis_client import delete
    await delete(_GEMINI_GLOBAL_COOLDOWN_KEY)
    logger.info("[gemini] Глобальну паузу Gemini знято")


async def set_global_quota_cooldown(seconds: int | None = None) -> None:
    """Ставить глобальну паузу Gemini після вичерпання квоти."""
    ttl = seconds if seconds is not None else GEMINI_GLOBAL_COOLDOWN_SEC
    await redis_set(_GEMINI_GLOBAL_COOLDOWN_KEY, "1", ex=max(60, ttl))
    logger.warning("[gemini] Глобальна пауза Gemini на %s с", ttl)


async def _ensure_not_paused() -> None:
    if await is_quota_paused():
        raise GeminiQuotaExhausted("Gemini global cooldown active")


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # Gemini інколи думає 30–60+ с; короткий read timeout дає порожній ReadTimeout.
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=90.0, write=30.0, pool=30.0)
        )
    return _client


def _format_exc(error: Exception | None) -> str:
    """Читабельний текст помилки (ReadTimeout часто має порожній str())."""
    if error is None:
        return "невідома помилка"
    if isinstance(error, httpx.TimeoutException):
        return f"таймаут очікування відповіді Gemini ({type(error).__name__})"
    text = str(error).strip()
    return text or type(error).__name__


async def close() -> None:
    """Закриває спільний HTTP-клієнт (викликати при зупинці бота)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _set_cooldown(seconds: float) -> None:
    global _cooldown_until
    _cooldown_until = max(_cooldown_until, time.monotonic() + max(0.0, seconds))


async def _wait_for_rate_slot() -> None:
    """Чекає глобальний cooldown + мінімальний інтервал між запитами."""
    global _last_request_at
    while True:
        now = time.monotonic()
        wait_cooldown = _cooldown_until - now
        wait_gap = GEMINI_MIN_INTERVAL_SEC - (now - _last_request_at)
        wait = max(wait_cooldown, wait_gap, 0.0)
        if wait <= 0:
            _last_request_at = time.monotonic()
            return
        logger.info("Gemini rate-limit: чекаємо %.1f с", wait)
        await asyncio.sleep(wait)


def _model_url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )


def _build_payload(prompt: str, use_search: bool, json_mode: bool = False) -> dict:
    gen_config: dict = {
        "temperature": 0.85,
        "maxOutputTokens": 4096 if json_mode else 8192,
    }
    # thinkingConfig лише для текстових постів. У JSON-режимі (короткі відповіді)
    # часто дає 403/порожній вивід на flash-моделях безкоштовного тарифу.
    if not json_mode:
        gen_config["thinkingConfig"] = {"thinkingBudget": 2048}

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    # ВАЖЛИВО: google_search несумісний зі структурованим JSON-виводом — у такій
    # комбінації Gemini стабільно повертає порожню відповідь. Тому інструмент
    # пошуку вмикаємо лише для звичайної текстової генерації, не для JSON.
    # Свіжі дані рубрики і так додаються у промпт через fetchers.
    if use_search and not json_mode:
        # Актуальний інструмент для Gemini 2.0+; google_search_retrieval застарів.
        payload["tools"] = [{"google_search": {}}]
    return payload


def _payload_without_thinking(payload: dict) -> dict:
    """Копія payload без thinkingConfig — для моделей, що не знають цього поля."""
    gen = {k: v for k, v in payload.get("generationConfig", {}).items() if k != "thinkingConfig"}
    return {**payload, "generationConfig": gen}


async def _request_model(model: str, payload: dict) -> str:
    """Запит до моделі зі стійкістю до непідтримуваного thinkingConfig.

    Якщо модель не знає поля thinkingConfig і повертає 400/403 — повторюємо
    той самий запит без нього.
    """
    try:
        return await _do_request(model, payload)
    except httpx.HTTPStatusError as error:
        has_thinking = "thinkingConfig" in payload.get("generationConfig", {})
        if error.response.status_code in (400, 403) and has_thinking:
            logger.warning(
                "%s відхилила thinkingConfig (%s) — повтор без нього",
                model,
                error.response.status_code,
            )
            return await _do_request(model, _payload_without_thinking(payload))
        raise


def _is_fatal_auth_error(error: Exception) -> bool:
    """True лише для зламаного API-ключа — тоді fallback між моделями безглуздий.

    403 на конкретну модель (немає доступу / модель недоступна, напр.
    gemini-3.5-flash) — НЕ фатальна: пробуємо наступну з GEMINI_MODELS.
    """
    if not isinstance(error, httpx.HTTPStatusError):
        return False
    status = error.response.status_code
    if status == 401:
        return True
    if status != 403:
        return False
    try:
        err = error.response.json().get("error", {})
        text = f"{err.get('status', '')} {err.get('message', '')}".lower()
    except Exception:
        return False
    return any(
        marker in text
        for marker in (
            "api key not valid",
            "api_key_invalid",
            "invalid api key",
            "api key expired",
        )
    )


async def _do_request(model: str, payload: dict) -> str:
    """Виконує один запит до конкретної моделі та дістає весь текст відповіді."""
    async with _request_lock:
        await _wait_for_rate_slot()
        response = await _get_client().post(
            _model_url(model),
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json=payload,
        )
        if response.status_code == 429:
            wait = _retry_after_seconds_from_response(response) or RETRY_BASE_DELAY
            _set_cooldown(min(wait, GEMINI_MAX_RETRY_WAIT_SEC))
        elif response.status_code == 503:
            # Перевантаження Google — коротка пауза перед наступною спробою.
            wait = _retry_after_seconds_from_response(response) or 8.0
            _set_cooldown(min(wait, 30.0))
        response.raise_for_status()
        data = response.json()

    # Промпт цілком заблоковано (safety / recitation) — кандидатів немає.
    candidates = data.get("candidates") or []
    if not candidates:
        block = (data.get("promptFeedback") or {}).get("blockReason", "невідомо")
        raise ValueError(f"{model}: запит заблоковано (blockReason={block})")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        # Порожня відповідь буває при safety-фільтрі або коли thinking-токени
        # (Gemini 3.x) з'їли весь бюджет виводу (finishReason=MAX_TOKENS).
        finish = candidate.get("finishReason", "невідомо")
        raise ValueError(f"{model}: порожня відповідь (finishReason={finish})")
    return text


def _should_retry(error: Exception) -> bool:
    """Чи має сенс повторити ту саму модель перед fallback."""
    if isinstance(error, GeminiQuotaExhausted):
        return False
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or status >= 500
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError))


def _error_body_text(error: Exception | None) -> str:
    if not isinstance(error, httpx.HTTPStatusError):
        return ""
    try:
        err = error.response.json().get("error", {})
        return f"{err.get('status', '')} {err.get('message', '')}".lower()
    except Exception:
        return str(error).lower()


def _is_daily_quota_exhausted(error: Exception | None) -> bool:
    """True якщо 429 схожий на денний ліміт (RPD), а не короткочасний RPM."""
    if not isinstance(error, httpx.HTTPStatusError):
        return False
    if error.response.status_code != 429:
        return False
    text = _error_body_text(error)
    # Явні денні маркери в тілі / quotaId.
    day_markers = ("per_day", "perday", "per day", "requestsperday", "rpd")
    minute_markers = ("per_minute", "perminute", "per minute", "requestsperminute", "rpm")
    if any(m in text.replace("_", "").replace("-", "") for m in (
        "generatedrequestsperday",
        "generatecontentfreetierrequestsperday",
    )) or any(m in text for m in day_markers):
        if any(m in text for m in minute_markers):
            return False
        return True
    wait = _retry_after_seconds(error)
    # Дуже довгий Retry-After ≈ денний ліміт (години), не RPM.
    return wait is not None and wait > GEMINI_MAX_RETRY_WAIT_SEC


def _retry_after_seconds_from_response(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        details = response.json().get("error", {}).get("details", [])
        for item in details:
            delay = item.get("retryDelay", "")
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])
    except Exception:
        pass
    return None


def _retry_after_seconds(error: Exception | None) -> float | None:
    """Дістає рекомендовану паузу з відповіді 429 (Retry-After або RetryInfo)."""
    if not isinstance(error, httpx.HTTPStatusError):
        return None
    return _retry_after_seconds_from_response(error.response)


async def _sleep_before_retry(attempt: int, error: Exception | None = None) -> None:
    # Поважаємо Retry-After від Gemini (для 429), інакше експоненційний backoff.
    wait = _retry_after_seconds(error)
    if wait is None:
        wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
    wait = min(wait, GEMINI_MAX_RETRY_WAIT_SEC)
    _set_cooldown(wait)
    logger.info("Gemini backoff: %.1f с (спроба %s)", wait, attempt)
    await asyncio.sleep(wait)


async def generate(prompt: str, use_search: bool = False) -> str:
    """
    Генерує текст із автоматичним fallback між моделями.

    Кожна модель отримує до MODEL_RETRIES спроб для тимчасових помилок.
    При недоступності, quota limit або несумісності API береться наступна.
    """
    await _ensure_not_paused()
    payload = _build_payload(prompt, use_search=use_search)
    last_exc: Exception | None = None
    daily_quota_hits = 0

    for model in GEMINI_MODELS:
        for attempt in range(1, MODEL_RETRIES + 1):
            try:
                text = await _request_model(model, payload)
                logger.info("Gemini відповідь згенеровано моделлю %s", model)
                return text
            except httpx.HTTPStatusError as error:
                if _is_fatal_auth_error(error):
                    raise
                last_exc = error
                if _is_daily_quota_exhausted(error):
                    daily_quota_hits += 1
                    if daily_quota_hits >= 2:
                        await set_global_quota_cooldown()
                        raise GeminiQuotaExhausted(
                            f"Денний ліміт Gemini вичерпано (останній: {model})"
                        ) from error
                    break
                if error.response.status_code in (403, 404):
                    logger.warning(
                        "Gemini %s недоступна (%s) — перемикаємось на резервну",
                        model,
                        error.response.status_code,
                    )
                    break
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
                last_exc = error

            logger.warning(
                "Gemini %s, спроба %s/%s: %s",
                model,
                attempt,
                MODEL_RETRIES,
                last_exc,
            )
            if attempt < MODEL_RETRIES and _should_retry(last_exc):
                await _sleep_before_retry(attempt, last_exc)
                continue
            break

        # Перед наступною моделлю теж почекаємо, якщо остання помилка — 429.
        if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.status_code == 429:
            await _sleep_before_retry(1, last_exc)
        logger.warning("Перемикання Gemini з %s на резервну модель", model)

    raise ValueError(
        f"Усі Gemini-моделі недоступні: {', '.join(GEMINI_MODELS)}. "
        f"Причина: {_format_exc(last_exc)}"
    ) from last_exc


def _extract_json(raw: str) -> str:
    """Очищає markdown-обгортку і виділяє ПЕРШИЙ збалансований JSON-об'єкт.

    Модель інколи додає текст до/після JSON або зайву закривну дужку в кінці.
    Тому шукаємо першу `{` і її парну `}`, ігноруючи дужки всередині рядків.
    """
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = clean.find("{")
    if start == -1:
        return clean

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(clean)):
        ch = clean[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return clean[start : i + 1]

    # Дужки не збалансувались — беремо до останньої `}` як запасний варіант.
    end = clean.rfind("}")
    return clean[start : end + 1] if end > start else clean[start:]


async def generate_json(prompt: str, use_search: bool = False) -> dict:
    """
    Генерує JSON із fallback між моделями.

    Невалідний JSON також вважається збоєм моделі: після повторної спроби
    генерація переходить до наступної моделі зі списку.
    """
    await _ensure_not_paused()
    payload = _build_payload(prompt, use_search=use_search, json_mode=True)
    last_exc: Exception | None = None
    last_raw = ""
    daily_quota_hits = 0

    for model in GEMINI_MODELS:
        for attempt in range(1, JSON_MODEL_RETRIES + 1):
            try:
                last_raw = await _request_model(model, payload)
                result = json.loads(_extract_json(last_raw))
                logger.info("Gemini JSON згенеровано моделлю %s", model)
                return result
            except httpx.HTTPStatusError as error:
                if _is_fatal_auth_error(error):
                    raise
                last_exc = error
                if _is_daily_quota_exhausted(error):
                    daily_quota_hits += 1
                    if daily_quota_hits >= 2:
                        await set_global_quota_cooldown()
                        raise GeminiQuotaExhausted(
                            f"Денний ліміт Gemini вичерпано (останній: {model})"
                        ) from error
                    break
                if error.response.status_code in (403, 404):
                    logger.warning(
                        "Gemini JSON %s недоступна (%s) — перемикаємось на резервну",
                        model,
                        error.response.status_code,
                    )
                    break
            except json.JSONDecodeError as error:
                last_exc = error
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
                last_exc = error

            logger.warning(
                "Gemini JSON %s, спроба %s/%s: %s",
                model,
                attempt,
                JSON_MODEL_RETRIES,
                last_exc,
            )
            if attempt < JSON_MODEL_RETRIES and (
                isinstance(last_exc, json.JSONDecodeError) or _should_retry(last_exc)
            ):
                await _sleep_before_retry(attempt, last_exc)
                continue
            break

        if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.status_code == 429:
            await _sleep_before_retry(1, last_exc)
        logger.warning("JSON fallback: перемикання з моделі %s", model)

    raise ValueError(
        f"Усі Gemini-моделі повернули помилку або невалідний JSON. "
        f"Причина: {_format_exc(last_exc)}. Остання відповідь:\n{last_raw[:400]}"
    ) from last_exc


# ─────────────────────────────────────────
# ВИБІР ПЕРСОНИ
# ─────────────────────────────────────────

def pick_persona() -> dict:
    """Випадково обирає одну з 4 персон."""
    return random.choice(PERSONAS)


# ─────────────────────────────────────────
# ВИБІР ВІЗУАЛЬНОГО ШАБЛОНУ
# ─────────────────────────────────────────

async def pick_template() -> dict:
    """
    Випадково обирає шаблон, але не той що був останні 2 рази.
    Зберігає вибір в Redis.
    """
    from data.redis_client import get_last_template, save_last_template

    last = await get_last_template()
    available = [t for t in VISUAL_TEMPLATES if t["name"] not in last]

    # якщо всі були (малоймовірно) — беремо будь-який крім останнього
    if not available:
        available = [t for t in VISUAL_TEMPLATES if t["name"] != last[-1]]

    template = random.choice(available)
    await save_last_template(template["name"])
    return template


# ─────────────────────────────────────────
# БАЗОВИЙ БУДІВНИК ПРОМПТУ
# ─────────────────────────────────────────

def build_base_prompt(
    rubric_name: str,
    rubric_hashtag: str,
    task: str,
    used_topics: list,
    persona: dict,
    extra_data: str = "",
) -> str:
    """
    Збирає базовий промпт з персоною, рубрикою і використаними темами.
    Кожен генератор рубрики викликає цю функцію і додає свій ФОРМАТ.
    """
    used_str = ", ".join(used_topics) if used_topics else "немає"

    return f"""Ти — {persona['name']}, {persona['role']}.
Канал "ФінПро для дітей" — Telegram-канал для підлітків України 12–20 років.
Твій стиль: {persona['style']}
Тип емодзі: {persona['emoji_style']}
Заклик до дії: {persona['cta_style']}

Рубрика: {rubric_name} ({rubric_hashtag})
Завдання: {task}

Теми що вже були (НЕ повторювати): {used_str}
{f'Додаткові дані:{chr(10)}{extra_data}' if extra_data else ''}
Вимоги до мови:
- Пиши як жива людина в Telegram, не як ШІ-асистент і не як новина з пресрелізу
- Українська розмовна; без канцеляриту і «розумних» конструкцій
- Мішай короткі й середні речення; уникай однакових шаблонних абзаців
- Можна максимум 1 розмовний хід («до речі», «слухай») — не на початку кожного абзацу і не в кожному пості однаково
- Без фраз-маркерів ШІ: «важливо зазначити», «у сучасному світі», «варто підкреслити», «давайте розберемо», «підсумовуючи»
- Без моралізаторства «дорослі краще знають»; емпатія — ок, повчання — ні
- Без хештегів (#...) у тексті посту — лише emoji та назва рубрики без #
- Довжина посту: максимум 150 слів
- Аудиторія: підліток 14 років має зрозуміти без словника
- Відповідай ТІЛЬКИ валідним JSON, без зайвого тексту і без ```
"""

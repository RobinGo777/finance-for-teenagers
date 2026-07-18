import asyncio
import json
import logging
import random
import httpx
from config import (
    GEMINI_API_KEY,
    GEMINI_MODELS,
    PERSONAS,
    VISUAL_TEMPLATES,
)
from data.redis_client import get_last_template, save_last_template

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# БАЗОВИЙ КЛІЄНТ GEMINI API
# ─────────────────────────────────────────

# Кількість спроб для кожної моделі перед переходом до наступної.
MODEL_RETRIES = 2
RETRY_BASE_DELAY = 2  # секунди (експоненційний backoff)

# Спільний HTTP-клієнт (пул з'єднань) — створюється лениво.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30)
    return _client


async def close() -> None:
    """Закриває спільний HTTP-клієнт (викликати при зупинці бота)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _model_url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )


def _build_payload(prompt: str, use_search: bool, json_mode: bool = False) -> dict:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            # Gemini 2.5/3.x рахують thinking-токени у межах maxOutputTokens.
            # Даємо великий бюджет, щоб після «роздумів» лишалось місце на текст,
            # інакше відповідь приходить порожня (finishReason=MAX_TOKENS).
            "maxOutputTokens": 8192,
            # Обмежуємо самі роздуми, щоб гарантовано лишити місце на вивід.
            "thinkingConfig": {"thinkingBudget": 2048},
        },
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

    Якщо модель не знає поля thinkingConfig і повертає 400 — повторюємо той
    самий запит без нього (тоді працює лише збільшений maxOutputTokens).
    """
    try:
        return await _do_request(model, payload)
    except httpx.HTTPStatusError as error:
        has_thinking = "thinkingConfig" in payload.get("generationConfig", {})
        if error.response.status_code == 400 and has_thinking:
            logger.warning("%s не підтримує thinkingConfig — повтор без нього", model)
            return await _do_request(model, _payload_without_thinking(payload))
        raise


async def _do_request(model: str, payload: dict) -> str:
    """Виконує один запит до конкретної моделі та дістає весь текст відповіді."""
    response = await _get_client().post(
        _model_url(model),
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=payload,
    )

    # Інший model fallback не виправить невірний/заблокований API-ключ.
    if response.status_code in (401, 403):
        response.raise_for_status()

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
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or status >= 500
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError))


def _retry_after_seconds(error: Exception | None) -> float | None:
    """Дістає рекомендовану паузу з відповіді 429 (Retry-After або RetryInfo)."""
    if not isinstance(error, httpx.HTTPStatusError):
        return None
    header = error.response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    # Gemini кладе паузу в тіло: error.details[].retryDelay = "37s"
    try:
        details = error.response.json().get("error", {}).get("details", [])
        for item in details:
            delay = item.get("retryDelay", "")
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])
    except Exception:
        pass
    return None


async def _sleep_before_retry(attempt: int, error: Exception | None = None) -> None:
    # Поважаємо Retry-After від Gemini (для 429), інакше експоненційний backoff.
    wait = _retry_after_seconds(error)
    if wait is None:
        wait = RETRY_BASE_DELAY * attempt
    await asyncio.sleep(min(wait, 60))


async def generate(prompt: str, use_search: bool = False) -> str:
    """
    Генерує текст із автоматичним fallback між моделями.

    Кожна модель отримує до MODEL_RETRIES спроб для тимчасових помилок.
    При недоступності, quota limit або несумісності API береться наступна.
    """
    payload = _build_payload(prompt, use_search=use_search)
    last_exc: Exception | None = None

    for model in GEMINI_MODELS:
        for attempt in range(1, MODEL_RETRIES + 1):
            try:
                text = await _request_model(model, payload)
                logger.info("Gemini відповідь згенеровано моделлю %s", model)
                return text
            except httpx.HTTPStatusError as error:
                if error.response.status_code in (401, 403):
                    raise
                last_exc = error
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

        logger.warning("Перемикання Gemini з %s на резервну модель", model)

    raise ValueError(
        f"Усі Gemini-моделі недоступні: {', '.join(GEMINI_MODELS)}"
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
    payload = _build_payload(prompt, use_search=use_search, json_mode=True)
    last_exc: Exception | None = None
    last_raw = ""

    for model in GEMINI_MODELS:
        for attempt in range(1, MODEL_RETRIES + 1):
            try:
                last_raw = await _request_model(model, payload)
                result = json.loads(_extract_json(last_raw))
                logger.info("Gemini JSON згенеровано моделлю %s", model)
                return result
            except httpx.HTTPStatusError as error:
                if error.response.status_code in (401, 403):
                    raise
                last_exc = error
            except json.JSONDecodeError as error:
                last_exc = error
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
                last_exc = error

            logger.warning(
                "Gemini JSON %s, спроба %s/%s: %s",
                model,
                attempt,
                MODEL_RETRIES,
                last_exc,
            )
            if attempt < MODEL_RETRIES:
                await _sleep_before_retry(attempt, last_exc)

        logger.warning("JSON fallback: перемикання з моделі %s", model)

    raise ValueError(
        f"Усі Gemini-моделі повернули помилку або невалідний JSON. "
        f"Причина: {last_exc}. Остання відповідь:\n{last_raw[:400]}"
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
Вимоги:
- Мова: українська, розмовна, без канцеляриту
- Довжина посту: максимум 150 слів
- Аудиторія: підліток 14 років має зрозуміти без словника
- Відповідай ТІЛЬКИ валідним JSON, без зайвого тексту і без ```
"""

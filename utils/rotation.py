"""Чергування рубрик у межах одного слота розкладу.

Слот у `SCHEDULE` вказує один ключ, а `ROTATIONS` каже, які рубрики цей ключ
насправді чергує. Лічильник живе в Redis, тому після перезапуску бота черга
продовжується, а не починається з початку.
"""

from __future__ import annotations

import logging

from config import ROTATIONS
from data.redis_client import incr

logger = logging.getLogger(__name__)


def variants_for(slot_key: str) -> list[str]:
    """Рубрики, які чергуються в слоті. Порожньо — слот без ротації."""
    variants = [str(v) for v in ROTATIONS.get(slot_key, []) if v]
    # Один варіант — це те саме, що й відсутність ротації.
    return variants if len(variants) > 1 else []


async def next_rubric(slot_key: str) -> str:
    """Наступна рубрика слота. Без ротації повертає сам ключ."""
    variants = variants_for(slot_key)
    if not variants:
        return slot_key

    try:
        counter = await incr(f"rotation:{slot_key}")
    except Exception as error:
        # Redis лежить — не втрачаємо слот, публікуємо перший варіант.
        logger.warning("[rotation] %s: лічильник недоступний (%s)", slot_key, error)
        return variants[0]

    # INCR повертає 1 на першому виклику, тож перший пост — variants[0].
    return variants[(int(counter) - 1) % len(variants)]

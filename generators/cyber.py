from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY     = "cyber"
RUBRIC_NAME    = "#КіберБезпека"
RUBRIC_HASHTAG = "🔐 КіберБезпека"

CYBER_TOPICS = [
    "паролі і менеджери паролів", "фішинг", "VPN",
    "двофакторна автентифікація", "крипто-гаманці",
    "дипфейки", "соціальна інженерія", "приватність в соцмережах",
    "публічний Wi-Fi", "шкідливі програми", "безпека месенджерів",
    "захист банківських даних", "анонімність в інтернеті",
]


async def generate_cyber() -> dict:
    """
    Генерує практичний пост про кібербезпеку (2 рази на місяць).
    Тільки конкретні дії — без води.
    """

    persona     = pick_persona()
    template    = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    # Завжди Dark Space — технологічна атмосфера
    from config import VISUAL_TEMPLATES
    template = next((t for t in VISUAL_TEMPLATES if t["name"] == "Dark Space"), template)

    available   = [t for t in CYBER_TOPICS if t not in used_topics]
    topics_hint = ", ".join(available[:6]) if available else "нова актуальна тема"

    task = (
        "Напиши практичний міні-гайд з кібербезпеки для підлітка. "
        "Тільки конкретні дії — без води і страшилок. "
        f"Обери тему з: {topics_hint}."
    )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
    )

    prompt = base + """
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{
  "topic": "назва теми (3-5 слів)",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🔐 КіберБезпека\\n\\n⚠️ [загроза — 1 речення]\\n\\n✅ Що робити:\\n1. [дія]\\n2. [дія]\\n3. [дія]\\n\\n🚨 Червоний прапорець: [ознака що тебе атакують]\\n\\n🛠️ Інструмент: [безкоштовний сервіс для захисту]\\n\\n💬 [питання читачам]",
  "body_preview": "1-2 речення для картинки (без емодзі)"
}
"""

    data = await generate_json(prompt)

    image_bytes = await generate_post_image_async(
        title=data.get("title", RUBRIC_NAME),
        body=data.get("body_preview", ""),
        rubric=RUBRIC_HASHTAG,
        persona_name=persona["name"],
        template=template,
    )

    await save_topic(RUBRIC_KEY, data["topic"])
    await add_weekly_topic(data["topic"])

    return {
        "rubric": RUBRIC_KEY,
        "topic": data["topic"],
        "post": data["post"],
        "image": image_bytes,
        "persona": persona["name"],
        "template": template["name"],
    }

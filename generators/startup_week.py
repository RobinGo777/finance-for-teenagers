from data.fetchers import fetch_news
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from images.generator import generate_post_image_async

RUBRIC_KEY = "startup_week"
RUBRIC_NAME = "#СтартапТижня"
RUBRIC_HASHTAG = "🚀 СтартапТижня"

STARTUP_AREAS = [
    "освітні технології",
    "штучний інтелект",
    "кліматичні технології",
    "фінтех",
    "медичні технології",
    "робототехніка",
    "ігри та creator economy",
    "космічні технології",
    "доступність для людей з інвалідністю",
    "українські стартапи",
]


async def generate_startup_week() -> dict:
    """Розбирає реальний стартап як продукт і бізнес, а не як рекламу."""
    persona = pick_persona()
    template = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)
    available = [area for area in STARTUP_AREAS if area not in used_topics]
    areas_hint = ", ".join(available[:7]) if available else "інша корисна технологічна сфера"

    try:
        news = await fetch_news(
            query="startup launch OR startup funding OR new technology product",
            language="en",
            page_size=8,
        )
    except Exception:
        news = []
    headlines = [
        f"- {item.get('title', '')}: {item.get('description', '')}"
        for item in news
        if item.get("title")
    ]
    news_context = "\n".join(headlines) if headlines else "Свіжих заголовків немає."

    task = (
        "Обери один РЕАЛЬНИЙ стартап або новий технологічний продукт зі свіжих "
        "даних. Поясни проблему, рішення, хто платить і головний ризик. Не "
        "перетворюй текст на рекламу, не вигадуй оцінку компанії чи інвестиції. "
        "Якщо свіжі дані не підтверджують конкретний стартап, обери відомий "
        f"перевірений приклад. Бажані сфери: {areas_hint}."
    )
    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=f"Свіжі стартап-заголовки:\n{news_context}",
    )
    prompt = base + """
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{
  "topic": "назва стартапу або продукту",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🚀 СтартапТижня\\n\\n💡 [назва]: [що створили одним реченням]\\n\\n🎯 Проблема: [що болить у користувача]\\n🛠️ Рішення: [як працює продукт]\\n💰 Хто платить: [бізнес-модель простою мовою]\\n⚠️ Ризик: [чесне обмеження або конкурент]\\n\\n🧪 Як перевірити ідею: [маленький урок для підлітка]\\n\\n💬 [питання родині]",
  "body_preview": "одне конкретне речення про продукт без емодзі"
}
"""
    data = await generate_json(prompt)
    image = await generate_post_image_async(
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
        "image": image,
        "persona": persona["name"],
        "template": template["name"],
    }

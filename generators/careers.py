from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY     = "careers"
RUBRIC_NAME    = "#ПрофесіїМайбутнього"
RUBRIC_HASHTAG = "🗺️ ПрофесіїМайбутнього"

CAREER_TOPICS = [
    "AI-інженер", "промпт-інженер", "дата-аналітик", "кібербезпека",
    "UX/UI дизайнер", "блокчейн-розробник", "біоінформатик",
    "менеджер з автоматизації", "створювач цифрового контенту",
    "спеціаліст з кліматичних технологій", "інженер робототехніки",
    "AR/VR розробник", "квантовий програміст", "менеджер ШІ-продуктів",
    "етичний хакер", "фінтех-аналітик", "Space-інженер",
    "продюсер цифрового контенту", "бренд-стратег", "спортивний менеджер",
    "3D-художник", "геймдизайнер", "саунд-дизайнер", "медичний ілюстратор",
    "фахівець з реабілітаційних технологій", "урбаніст", "food-tech технолог",
    "менеджер креативних проєктів", "фахівець з міжнародної логістики",
]


async def generate_careers() -> dict:
    """
    Генерує пост про професію майбутнього.
    Фокус: що вчити вже зараз, реальна зарплата, приклади компаній.
    """

    persona     = pick_persona()
    template    = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    available = [t for t in CAREER_TOPICS if t not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "нова актуальна професія"

    task = (
        "Опиши ОДНУ сучасну професію для підлітка 12-20 років. Чергуй технічні, "
        "креативні, підприємницькі, наукові та соціально корисні напрями. "
        "Фокус: що вчити вже зараз, реальна зарплата в Україні та світі, "
        "приклади компаній де працюють. Зарплату подавай як орієнтовний діапазон "
        "із зазначенням рівня досвіду, а не як гарантовану суму. "
        f"Обери з: {topics_hint}."
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
  "topic": "назва професії",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🗺️ ПрофесіїМайбутнього\\n\\n🚀 [Назва професії]\\n\\n💬 [чому ця професія існує — 1 речення]\\n\\n💰 Зарплата: [Україна] / [світ]\\n🛠️ Що вчити зараз:\\n• [навичка 1]\\n• [навичка 2]\\n• [навичка 3]\\n🏢 Де працюють: [3 компанії]\\n📚 Старт: [1 безкоштовний ресурс]\\n\\n🤔 [питання для роздумів]",
  "body_preview": "1-2 речення для картинки (без емодзі)"
}
"""

    data = await generate_json(prompt, use_search=True)

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

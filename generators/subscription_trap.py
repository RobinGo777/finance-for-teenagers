from generators.gemini import generate_json, pick_persona, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async
from config import VISUAL_TEMPLATES

RUBRIC_KEY = "subscription_trap"
RUBRIC_NAME = "#ПідпискаПастка"
RUBRIC_HASHTAG = "🪤 #ПідпискаПастка"

TRAP_TOPICS = [
    "безкоштовний пробний період з автосписанням",
    "підписки Apple та Google Play",
    "донати у мобільних іграх",
    "розстрочка без переплати",
    "buy now pay later",
    "непомітні комісії банківських карток",
    "динамічні ціни у доставці та таксі",
    "темні патерни кнопки скасування",
    "фейкова знижка перед розпродажем",
    "платна хмара після закінчення місця",
]


async def generate_subscription_trap() -> dict:
    """Розбирає одну повсякденну пастку регулярних платежів або прихованої ціни."""
    persona = pick_persona()
    used_topics = await get_used_topics(RUBRIC_KEY)
    template = next(
        (item for item in VISUAL_TEMPLATES if item["name"] == "Warm Alert"),
        VISUAL_TEMPLATES[0],
    )

    available = [topic for topic in TRAP_TOPICS if topic not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "нова пастка у цифровій оплаті"
    task = (
        "Розбери одну платіжну або маркетингову пастку, з якою підліток реально "
        "стикається в застосунках, іграх чи магазинах. Покажи невелику суму за місяць "
        "і її вартість за рік. Дай точний шлях перевірки або скасування без прив'язки "
        "до конкретної версії меню. Не звинувачуй користувача. "
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
  "topic": "назва пастки",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🪤 #ПідпискаПастка\\n\\n😬 [ситуація одним реченням]\\n\\n💸 [сума]/місяць = [сума]/рік\\n\\n🔍 Де пастка: [як працює механіка]\\n\\n✅ Що перевірити зараз:\\n1. [крок]\\n2. [крок]\\n3. [крок]\\n\\n💬 [питання читачам]",
  "body_preview": "скільки непомітна оплата коштує за рік без емодзі"
}
"""

    data = await generate_json(prompt, use_search=True)
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

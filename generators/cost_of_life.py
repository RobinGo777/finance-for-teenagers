from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from data.fetchers import fetch_nbu_rates
from images.generator import generate_post_image_async

RUBRIC_KEY = "cost_of_life"
RUBRIC_NAME = "#СкількиКоштує"
RUBRIC_HASHTAG = "🧾 #СкількиКоштує"

COST_TOPICS = [
    "місяць життя студента у Києві",
    "зібрати ігровий ПК",
    "перший смартфон без переплати",
    "утримувати домашнього улюбленця",
    "поїздка на музичний фестиваль",
    "навчання на онлайн-курсі",
    "місяць кави та перекусів",
    "підписки на музику, кіно та ігри",
    "перший велосипед або самокат",
    "переїзд до іншого міста на навчання",
]


async def generate_cost_of_life() -> dict:
    """Рахує реальну ціну бажання або повсякденного сценарію підлітка."""
    persona = pick_persona()
    template = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)
    rates = await fetch_nbu_rates(["USD", "EUR"])

    available = [topic for topic in COST_TOPICS if topic not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "новий актуальний сценарій"
    extra = (
        f"Офіційні курси НБУ: USD {rates.get('USD', 'N/A')} грн, "
        f"EUR {rates.get('EUR', 'N/A')} грн. "
        "Для цін використовуй свіжий веб-пошук і вказуй орієнтовний діапазон."
    )

    task = (
        "Порахуй, скільки реально коштує одна ціль або звичка підлітка в Україні. "
        "Покажи 3-4 складові ціни, дешевший розумний варіант і скільки часу треба "
        "відкладати з невеликого щомісячного бюджету. Не вигадуй точні ціни: "
        f"давай перевірювані діапазони. Обери тему з: {topics_hint}."
    )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=extra,
    )

    prompt = base + """
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{
  "topic": "коротка назва розрахунку",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🧾 #СкількиКоштує\\n\\n🎯 [ціль або сценарій]\\n\\n💸 Орієнтовно: [діапазон у гривнях]\\n• [складова 1]: [ціна]\\n• [складова 2]: [ціна]\\n• [складова 3]: [ціна]\\n\\n🧠 Розумніший варіант: [як зекономити без небезпечних схем]\\n\\n⏳ Якщо відкладати [сума]/місяць: [реальний строк]\\n\\n💬 [питання читачам]",
  "body_preview": "короткий підсумок ціни для картинки без емодзі"
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

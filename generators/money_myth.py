from generators.gemini import generate_json, pick_persona, build_base_prompt
from data.redis_client import (
    get_used_topics,
    save_topic,
    get_weekly_topics,
    clear_weekly_topics,
)
from images.generator import generate_post_image_async
from config import VISUAL_TEMPLATES

RUBRIC_KEY = "money_myth"
RUBRIC_NAME = "#МіфПроГроші"
RUBRIC_HASHTAG = "🧨 #МіфПроГроші"

MONEY_MYTHS = [
    "крипта завжди дає швидкі гроші",
    "щоб інвестувати, треба бути багатим",
    "дешевше означає вигідніше",
    "усі успішні люди встали о п'ятій ранку",
    "дорогий курс гарантує високу зарплату",
    "кредитні гроші — це додатковий дохід",
    "пасивний дохід не потребує роботи",
    "власний бізнес завжди кращий за роботу",
    "економити можна лише на великих покупках",
    "популярний фінансовий блогер точно експерт",
]


async def generate_money_myth() -> dict:
    """Перевіряє вірусний фінансовий міф фактами та завершує контентний тиждень."""
    persona = pick_persona()
    used_topics = await get_used_topics(RUBRIC_KEY)
    weekly_topics = await get_weekly_topics()
    template = next(
        (item for item in VISUAL_TEMPLATES if item["name"] == "Newspaper"),
        VISUAL_TEMPLATES[0],
    )

    available = [topic for topic in MONEY_MYTHS if topic not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "новий вірусний міф про гроші"
    week_context = ", ".join(weekly_topics[:15]) if weekly_topics else "немає"
    task = (
        "Перевір один популярний фінансовий міф, який може побачити підліток у TikTok, "
        "YouTube або Telegram. Винеси чіткий вердикт: правда, напівправда або міф. "
        "Наведи один перевірюваний факт або простий розрахунок і поясни, коли твердження "
        f"все ж може бути частково корисним. Обери з: {topics_hint}."
    )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=f"Теми каналу цього тижня (можна використати як контекст): {week_context}",
    )
    prompt = base + """
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{
  "topic": "коротка назва міфу",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🧨 #МіфПроГроші\\n\\n🗣️ Міф: «[твердження]»\\n\\n⚖️ Вердикт: [МІФ / НАПІВПРАВДА / ПРАВДА]\\n\\n🔎 Фактчек: [факт або простий розрахунок]\\n\\n🤏 Де є частка правди: [коротке уточнення]\\n\\n🧠 Запам'ятай: [одне практичне правило]\\n\\n💬 [питання читачам]",
  "body_preview": "міф і короткий вердикт без емодзі"
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
    await clear_weekly_topics()
    return {
        "rubric": RUBRIC_KEY,
        "topic": data["topic"],
        "post": data["post"],
        "image": image,
        "persona": persona["name"],
        "template": template["name"],
    }

from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY = "behavioral_finance"
RUBRIC_NAME = "#ГрошіВГолові"
RUBRIC_HASHTAG = "🧠 #ГрошіВГолові"

BEHAVIOR_TOPICS = [
    "імпульсивна покупка",
    "FOMO і страх пропустити вигоду",
    "ефект знижки та перекресленої ціни",
    "безконтактна оплата і відчуття витрат",
    "соціальне порівняння в Instagram і TikTok",
    "покупки для швидкого дофаміну",
    "ефект безкоштовного пробного періоду",
    "ментальний облік кишенькових грошей",
    "чому дрібні витрати здаються непомітними",
    "якір ціни та дорогий варіант для порівняння",
    "ефект володіння речами",
    "тиск друзів під час покупок",
]


async def generate_behavioral_finance() -> dict:
    """Пояснює психологію рішень про гроші без сорому й моралізаторства."""
    persona = pick_persona()
    template = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)
    available = [topic for topic in BEHAVIOR_TOPICS if topic not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "інший фінансовий когнітивний ефект"

    task = (
        "Поясни один психологічний механізм, через який підліток витрачає "
        "гроші не так, як планував. Не звинувачуй читача й не став діагнозів. "
        "Дай життєвий приклад, простий мініексперимент для самоперевірки та "
        f"одне практичне правило захисту. Обери тему з: {topics_hint}."
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
  "topic": "назва психологічного ефекту",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🧠 #ГрошіВГолові\\n\\n🎯 [життєва ситуація, яку легко впізнати]\\n\\n🧩 Що відбувається: [просте пояснення механізму]\\n\\n🔬 Перевір себе: [безпечний мініексперимент або питання]\\n\\n🛡️ Правило: [одна конкретна дія перед покупкою]\\n\\n💬 [питання родині]",
  "body_preview": "одна коротка думка для картинки без емодзі"
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

from generators.gemini import generate_json, pick_persona, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async
from config import VISUAL_TEMPLATES

RUBRIC_KEY = "game_economy"
RUBRIC_NAME = "#ГеймЕкономіка"
RUBRIC_HASHTAG = "🎮 ГеймЕкономіка"

GAME_TOPICS = [
    "чому безкоштовні ігри заробляють мільярди",
    "скіни у Counter-Strike як цифровий ринок",
    "Robux і внутрішня валюта Roblox",
    "донати та battle pass",
    "як Steam заробляє на комісії",
    "економіка Minecraft-серверів",
    "чому лутбокси схожі на азартні ігри",
    "зарплати кіберспортсменів і реальні шанси",
    "як заробляють творці модів",
    "ринок мобільних ігор",
]


async def generate_game_economy() -> dict:
    """Пояснює фінансові механіки ігор через знайомі підліткам приклади."""
    persona = pick_persona()
    used_topics = await get_used_topics(RUBRIC_KEY)
    template = next(
        (item for item in VISUAL_TEMPLATES if item["name"] == "Game Mode"),
        VISUAL_TEMPLATES[0],
    )

    available = [topic for topic in GAME_TOPICS if topic not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "нова тема ігрової економіки"
    task = (
        "Поясни одну реальну фінансову механіку зі світу відеоігор. Покажи, хто "
        "заробляє, за що платить гравець і який психологічний прийом використовує "
        "гра. Відділяй перевірені способи заробітку від рідкісних історій успіху. "
        "Не рекламуй азартні механіки або торгівлю скінами як інвестицію. "
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
  "topic": "назва механіки",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🎮 ГеймЕкономіка\\n\\n🕹️ [знайома ситуація з гри]\\n\\n💰 Як тут рухаються гроші: [просте пояснення]\\n\\n🏢 Хто заробляє: [компанія, творець або платформа]\\n🧠 На що тебе ловлять: [механіка уваги або покупки]\\n\\n🛡️ Розумний хід: [практичне правило для бюджету]\\n\\n💬 [питання читачам]",
  "body_preview": "короткий факт про економіку гри без емодзі"
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

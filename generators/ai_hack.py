from datetime import datetime
from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY     = "ai_hack"
RUBRIC_NAME    = "#ШІ_Лайфхак"
RUBRIC_HASHTAG = "🤖💡 #ШІ_Лайфхак"


async def generate_ai_hack() -> dict:
    """
    Генерує пост для рубрики #ШІ_Лайфхак.
    Практичний лайфхак з готовим промптом який підліток може скопіювати.
    """

    persona     = pick_persona()
    template    = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    task = (
        "Придумай ОДИН практичний лайфхак як ШІ допомагає підлітку економити час або гроші. "
        "Обов'язково дай готовий промпт який можна скопіювати і одразу спробувати. "
        "Інструменти: ChatGPT, Gemini, Claude, Perplexity, Midjourney, Gamma — що підходить до теми."
    )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
    )

    prompt = base + f"""
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{{
  "topic": "коротка назва теми (3-5 слів)",
  "title": "заголовок для картинки (макс 8 слів)",
  "tool": "назва ШІ інструменту",
  "post": "{RUBRIC_HASHTAG}\\n\\n[суть лайфхаку 2-3 речення]\\n\\n📋 Промпт:\\n\\"[готовий промпт який можна скопіювати]\\"\\n\\n⚡ Результат: [що отримаєш за 30 секунд]\\n\\n💬 [питання читачам]",
  "body_preview": "1-2 речення для картинки (без емодзі)"
}}
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

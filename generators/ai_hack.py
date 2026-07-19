from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.fetchers import fetch_news
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY     = "ai_hack"
RUBRIC_NAME    = "#ШІ_Лайфхак"
RUBRIC_HASHTAG = "🤖💡 #ШІ_Лайфхак"

AI_HACK_TOPICS = [
    "пояснити складну тему простими словами",
    "створити план підготовки до контрольної",
    "перевірити власний текст без списування",
    "потренувати іноземну мову",
    "скласти бюджет покупки",
    "порівняти кілька варіантів товару",
    "перетворити конспект на картки для повторення",
    "підготувати запитання до співбесіди",
    "придумати структуру презентації",
    "розібрати помилку в коді",
    "спланувати особистий навчальний проєкт",
    "перевірити правдивість твердження за джерелами",
]


async def generate_ai_hack() -> dict:
    """
    Генерує пост для рубрики #ШІ_Лайфхак.
    Практичний лайфхак з готовим промптом який підліток може скопіювати.
    """

    persona     = pick_persona()
    template    = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    available = [topic for topic in AI_HACK_TOPICS if topic not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "інший практичний сценарій"
    try:
        news = await fetch_news(
            query="ChatGPT OR Gemini OR Claude AI new feature",
            language="en",
            page_size=5,
        )
    except Exception:
        news = []
    updates = "\n".join(
        f"- {item.get('title', '')}" for item in news if item.get("title")
    )

    task = (
        "Створи ОДИН практичний і чесний сценарій використання ШІ для підлітка. "
        "ШІ має допомагати вчитися, планувати або перевіряти власну роботу, а не "
        "робити домашнє завдання замість людини. Дай готовий промпт, який можна "
        "скопіювати. Не згадуй конкретну нову функцію, якщо її немає у свіжих "
        f"заголовках. Обери сценарій з: {topics_hint}."
    )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=f"Свіжі заголовки про ШІ-інструменти:\n{updates or 'немає даних'}",
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

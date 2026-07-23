from datetime import datetime
from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from data.fetchers import fetch_all_rss, fetch_news
from images.generator import generate_post_image_async

RUBRIC_KEY     = "ai_news"
RUBRIC_NAME    = "#ТехНовини"
RUBRIC_HASHTAG = "🚀 ТехНовини"


async def generate_ai_news(focus: str | None = None) -> dict:
    """
    Генерує пост для рубрики #ТехНовини (технології + ШІ, гаджети, космос, наука).
    Використовує свіжі RSS + NewsAPI + Gemini web search.

    focus — необов'язковий конкретний матеріал (заголовок новини, назва репо
    тощо), на якому треба зосередити пост. Використовується реалтайм-монітором,
    щоб пост був саме про знайдений привід, а не про випадкову тему.

    Повертає dict з текстом поста, картинкою і метаданими.
    """

    # 1. Збираємо свіжі дані (не лише ШІ, а й техно-світ загалом)
    rss_items  = await fetch_all_rss(limit_per_feed=3)
    news_items = await fetch_news(
        query="technology OR AI OR gadgets OR space OR science OR robotics",
        page_size=5,
    )

    # Формуємо короткий список заголовків для промпту
    rss_titles  = [i["title"] for i in rss_items if i.get("title")][:6]
    news_titles = [i["title"] for i in news_items if i.get("title")][:4]
    all_titles  = rss_titles + news_titles

    news_data = "\n".join(f"- {t}" for t in all_titles) if all_titles else "немає даних"
    if focus:
        news_data = f"ГОЛОВНИЙ ПРИВІД (пиши саме про це):\n- {focus}\n\nІнші новини:\n{news_data}"

    # 2. Вибираємо персону і шаблон
    persona  = pick_persona()
    template = await pick_template()

    # 3. Отримуємо використані теми
    used_topics = await get_used_topics(RUBRIC_KEY)

    # 4. Будуємо промпт
    if focus:
        task = (
            "Напиши пост саме про 'ГОЛОВНИЙ ПРИВІД' з наданих даних. "
            "Обов'язково поясни що це означає конкретно для підлітка або студента. "
            "Можеш уточнити деталі через пошук."
        )
    else:
        task = (
            "Знайди найцікавішу свіжу новину зі світу технологій — це може бути "
            "штучний інтелект, гаджети, космос, робототехніка, наука чи великі "
            "IT-компанії. Напиши захопливий пост. Обов'язково поясни, що це "
            "означає конкретно для підлітка або студента. Можеш взяти одну з "
            "наданих новин або знайти свіжішу через пошук."
        )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=f"Свіжі новини з RSS/NewsAPI:\n{news_data}",
    )

    prompt = base + f"""
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{{
  "topic": "коротка назва теми (3-5 слів)",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "{RUBRIC_HASHTAG} | {datetime.now().strftime('%d.%m.%Y')}\\n\\n[текст посту]\\n\\n💬 [питання читачам]",
  "body_preview": "1-2 речення для картинки (без емодзі)"
}}
"""

    # 5. Генеруємо через Gemini з увімкненим web search
    data = await generate_json(prompt, use_search=True)

    # 6. Генеруємо картинку
    image_bytes = await generate_post_image_async(
        title=data.get("title", RUBRIC_NAME),
        body=data.get("body_preview", ""),
        rubric=RUBRIC_HASHTAG,
        persona_name=persona["name"],
        template=template,
    )

    # 7. Зберігаємо тему в Redis
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

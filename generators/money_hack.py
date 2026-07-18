from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY     = "money_hack"
RUBRIC_NAME    = "#ЛайфхакГрошей"
RUBRIC_HASHTAG = "💡 #ЛайфхакГрошей"

MONEY_TOPICS = [
    "cashback з карток", "знижки і промокоди",
    "як заробити в 14-17 років легально",
    "resell — купи дешевше продай дорожче",
    "монетизація ШІ навичок", "фріланс для початківців",
    "монетизація ігрових навичок",
    "продаж цифрових товарів (стікери, пресети, шаблони)",
    "студентські знижки які мало хто знає",
    "як економити на підписках", "заробіток на YouTube Shorts",
    "Telegram канал як бізнес", "продаж фото на стоках",
    "участь в опитуваннях за гроші",
    "affiliate маркетинг для підлітків",
]


async def generate_money_hack() -> dict:
    """
    Генерує практичний лайфхак про заробіток або економію для підлітків України.
    Тільки реальні способи — без схем і обману.
    """

    persona     = pick_persona()
    template    = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    # Golden Flash — ідеально для лайфхаків
    from config import VISUAL_TEMPLATES
    template = next((t for t in VISUAL_TEMPLATES if t["name"] == "Golden Flash"), template)

    available   = [t for t in MONEY_TOPICS if t not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "новий спосіб заробітку"

    task = (
        "Розкажи про реальний спосіб заробити або зекономити гроші для підлітка в Україні. "
        "Тільки легальні методи без обману. Конкретні кроки як почати вже сьогодні. "
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
  "topic": "назва теми (3-5 слів)",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "💡 #ЛайфхакГрошей\\n\\n🔥 [зачіпка — скільки можна заробити або зекономити]\\n\\n[пояснення 2-3 речення з конкретикою]\\n\\n⚡ Старт за 3 кроки:\\n1. [крок]\\n2. [крок]\\n3. [крок]\\n\\n💰 Реальний результат: [скільки і за який час]\\n\\n💬 [питання читачам]",
  "body_preview": "1-2 речення для картинки (без емодзі)"
}
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

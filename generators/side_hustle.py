from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY = "side_hustle"
RUBRIC_NAME = "#ПершіГроші"
RUBRIC_HASHTAG = "🛠️ #ПершіГроші"

SIDE_HUSTLES = [
    "монтаж коротких відео",
    "дизайн обкладинок і презентацій",
    "репетиторство для молодших школярів",
    "ведення соцмереж малого бізнесу",
    "створення Telegram-ботів без коду",
    "фото та відео для локальних закладів",
    "продаж власних цифрових шаблонів",
    "налаштування техніки для знайомих",
    "догляд за тваринами",
    "переклад і субтитри",
    "перепродаж уживаних речей",
    "створення стікерів, пресетів і цифрових шаблонів",
    "монтаж YouTube Shorts для локального бізнесу",
    "створення простих AI-автоматизацій",
    "продаж власних фото на фотостоках",
    "партнерський маркетинг без спаму",
]


async def generate_side_hustle() -> dict:
    """Дає реалістичний план першого підробітку без обіцянок легких грошей."""
    persona = pick_persona()
    template = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    available = [topic for topic in SIDE_HUSTLES if topic not in used_topics]
    topics_hint = ", ".join(available[:8]) if available else "новий легальний підробіток"
    task = (
        "Розбери один легальний спосіб отримати перші власні гроші для підлітка "
        "в Україні. Не обіцяй гарантований дохід. Покажи потрібну навичку, "
        "перший безпечний спосіб знайти клієнта або покупця, стартову ціну як "
        "орієнтовний діапазон та типову помилку. "
        "Для неповнолітніх згадай участь батьків, якщо потрібні договори або платежі. "
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
  "topic": "назва підробітку",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🛠️ #ПершіГроші\\n\\n💼 [підробіток]: що ти продаєш клієнту\\n\\n💰 Реалістичний старт: [діапазон і за що саме]\\n\\n🚀 Перший клієнт за 3 кроки:\\n1. [створи приклад роботи]\\n2. [де безпечно запропонувати]\\n3. [як домовитися про оплату]\\n\\n⚠️ Не роби так: [типова помилка або ризик]\\n\\n💬 [питання читачам]",
  "body_preview": "одне конкретне речення про підробіток без емодзі"
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

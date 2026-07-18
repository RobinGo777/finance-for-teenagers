from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from data.fetchers import fetch_crypto, fetch_trending_crypto
from images.generator import generate_post_image_async

RUBRIC_KEY     = "crypto"
RUBRIC_NAME    = "#КриптоБезХайпу"
RUBRIC_HASHTAG = "₿ #КриптоБезХайпу"

# Теми для ротації (Gemini обирає з тих що ще не були)
CRYPTO_TOPICS = [
    "як люди реально заробляли на ранньому Bitcoin",
    "прибуток і survivor bias у крипті",
    "інвестування проти трейдингу",
    "стейкінг: звідки береться дохід",
    "робота в Web3 без купівлі монет",
    "майнінг: дохід, електрика і обладнання",
    "airdrop: винагорода чи полювання за гаманцем",
    "DeFi-відсотки та ризик смарт-контракту",
    "stablecoin і ризик втрати прив'язки",
    "Bitcoin та Ethereum простими словами",
    "Layer 2 і дешевші перекази",
    "криптобіржа проти власного гаманця",
    "Scam-alert і гарантований прибуток",
    "фішинг у крипті та приватний ключ",
    "NFT: що лишилося після хайпу",
    "як блокчейн використовують поза трейдингом",
]


async def generate_crypto() -> dict:
    """
    Генерує практичний пост про крипту: технології, способи заробітку та ризики.
    Не знецінює реальні історії успіху, але показує повну картину без обіцянок.
    """

    # 1. Дані
    crypto_data      = await fetch_crypto()
    trending         = await fetch_trending_crypto()
    used_topics      = await get_used_topics(RUBRIC_KEY)

    persona  = pick_persona()
    template = await pick_template()

    # Залишок тем які ще не були
    available_topics = [t for t in CRYPTO_TOPICS if t not in used_topics]
    topics_hint = ", ".join(available_topics[:10]) if available_topics else "будь-яка нова тема"

    btc = crypto_data.get("bitcoin", {})
    extra = (
        f"BTC: ${btc.get('usd', 'N/A')} ({btc.get('usd_24h_change', 0):.1f}% за 24год)\n"
        f"Трендові монети зараз: {', '.join(trending)}"
    )

    task = (
        "Розбери ОДНУ тему зі світу крипти за принципом «як це працює, як тут могли "
        "заробити люди і який ризик вони взяли». Не заперечуй реальні великі прибутки: "
        "поясни, за рахунок чого вони виникли, який був горизонт часу та чому результат "
        "не гарантований для нового учасника. Розрізняй інвестування, активний трейдинг, "
        "стейкінг і роботу в індустрії. Наводь лише перевірювані цифри або діапазони. "
        "Не давай персональної інвестиційної поради, сигналів, прогнозу ціни чи заклику "
        "купувати. Для неповнолітніх згадай вікові обмеження бірж і участь батьків. "
        f"Обери тему з цих (або схожу): {topics_hint}. "
        "Якщо тема Scam-alert — додай 3 ознаки шахрайства і що робити, якщо потрапив."
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
  "topic": "назва поняття",
  "title": "заголовок для картинки (макс 8 слів)",
  "is_scam_alert": false,
  "post": "₿ #КриптоБезХайпу\\n\\n[emoji] [тема і коротка зачіпка]\\n\\n⚙️ Як це працює: [2-3 прості речення]\\n\\n💰 Як тут заробляли: [механіка доходу, строк і перевірюваний приклад без обіцянок]\\n\\n⚠️ За що платять ризиком: [головний ризик і що можна втратити]\\n\\n🧭 Безпечний спосіб вивчити: [демо, тестова мережа, курс або спостереження без купівлі]\\n\\n[якщо scam: 🚨 Ознаки шахрайства:\\n1. ...\\n2. ...\\n3. ...]\\n\\n💬 [питання читачам]",
  "body_preview": "1-2 речення для картинки (без емодзі)"
}
"""

    data = await generate_json(prompt, use_search=True)

    # Для Scam-alert використовуємо Warm Alert шаблон
    if data.get("is_scam_alert"):
        from config import VISUAL_TEMPLATES
        template = next((t for t in VISUAL_TEMPLATES if t["name"] == "Warm Alert"), template)

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

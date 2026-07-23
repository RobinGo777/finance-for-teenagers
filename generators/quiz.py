from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_quiz_image_async

RUBRIC_KEY     = "quiz"
RUBRIC_NAME    = "#ФінКвіз"
RUBRIC_HASHTAG = "🧠 ФінКвіз"

QUIZ_TOPICS = [
    "бюджет і планування",
    "банківська картка та рахунок",
    "відсотки й складний відсоток",
    "інфляція та ціни",
    "податки простими словами",
    "кредит і борг",
    "шахрайство та безпечні платежі",
    "психологія покупок",
    "заощадження на ціль",
    "інвестиційний ризик",
    "криптовалюти без хайпу",
    "цифрові товари й підписки",
    "заробіток і перша робота",
    "фінансові факти з історії",
]


async def generate_quiz() -> dict:
    """
    Генерує фінансовий квіз з 4 варіантами відповіді.
    Публікується як Telegram Poll.
    Через 24 год publisher.py публікує 💡 пост-відповідь.
    """

    persona     = pick_persona()
    template    = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    # Game Mode — ідеально для квізу
    from config import VISUAL_TEMPLATES
    template = next((t for t in VISUAL_TEMPLATES if t["name"] == "Game Mode"), template)

    available = [topic for topic in QUIZ_TOPICS if topic not in used_topics]
    topics_hint = ", ".join(available[:9]) if available else "інша тема фінансової грамотності"
    task = (
        "Створи один фінансовий квіз з 4 варіантами відповіді. "
        "Питання має бути цікавим — з wow-фактом або легкою провокацією. "
        "Правильна відповідь не повинна бути очевидною, але має бути однозначною "
        "і фактично перевірюваною. Не використовуй питання з мінливою поточною "
        f"ціною без дати. Обери тему з: {topics_hint}."
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
  "topic": "коротка назва теми (2-4 слова)",
  "question": "питання (макс 100 символів)",
  "options": ["варіант А", "варіант Б", "варіант В", "варіант Г"],
  "correct_index": 1,
  "lamp_post": "💡 Відповідь на ФінКвіз\\n\\nПравильна відповідь: [варіант] ✅\\n\\n[пояснення 2-3 речення]\\n\\n🤯 Wow-факт: [цікавий факт по темі]"
}
"""

    data = await generate_json(prompt)

    image_bytes = await generate_quiz_image_async(
        question=data.get("question", ""),
        template=template,
    )

    await save_topic(RUBRIC_KEY, data["topic"])
    await add_weekly_topic(data["topic"])

    return {
        "rubric": RUBRIC_KEY,
        "topic": data["topic"],
        "question": data["question"],
        "options": data["options"],
        "correct_index": data["correct_index"],
        "lamp_post": data["lamp_post"],
        "image": image_bytes,
        "persona": persona["name"],
        "template": template["name"],
    }


async def generate_quiz_answer(poll_id: str, poll_results: dict) -> str:
    """
    Генерує 💡 пост-відповідь через 24 год після квізу.
    poll_results — статистика відповідей з Telegram.
    """
    from data.redis_client import get_quiz_pending

    pending = await get_quiz_pending(poll_id)
    if not pending:
        return ""

    lamp_post = pending.get("lamp_post", "")

    # Додаємо статистику якщо є
    if poll_results:
        total = sum(poll_results.values())
        correct_idx = pending.get("correct_index", 0)
        correct_votes = poll_results.get(str(correct_idx), 0)
        percent = round(correct_votes / total * 100) if total else 0
        lamp_post += f"\n\n📊 Правильно відповіли: {percent}% з вас ({correct_votes}/{total})"

    return lamp_post

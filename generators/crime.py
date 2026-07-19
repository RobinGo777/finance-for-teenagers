from generators.gemini import generate_json, pick_persona, pick_template, build_base_prompt
from data.fetchers import fetch_news
from data.redis_client import get_used_topics, save_topic, add_weekly_topic
from images.generator import generate_post_image_async

RUBRIC_KEY     = "crime"
RUBRIC_NAME    = "#ФінТруКрайм"
RUBRIC_HASHTAG = "⚖️ #ФінТруКрайм"


async def generate_crime() -> dict:
    """
    Генерує пост про реальну фінансову аферу у стилі детективу.
    Інтерактив: 3 факти — один брехня шахрая.
    """

    persona     = pick_persona()
    template    = await pick_template()
    used_topics = await get_used_topics(RUBRIC_KEY)

    # Scam-alert завжди на Warm Alert шаблоні
    from config import VISUAL_TEMPLATES
    template = next((t for t in VISUAL_TEMPLATES if t["name"] == "Warm Alert"), template)

    try:
        news = await fetch_news(
            query="financial fraud OR scam OR Ponzi scheme OR cyber fraud",
            language="en",
            page_size=8,
        )
    except Exception:
        news = []
    cases = "\n".join(
        f"- {item.get('title', '')}: {item.get('description', '')}"
        for item in news
        if item.get("title")
    )
    task = (
        "Вибери реальну й перевірювану фінансову аферу або скандал (світову "
        "або українську), бажано зі свіжих заголовків. Не вигадуй імен, дат, "
        "сум чи судових рішень; якщо точна цифра не підтверджена — не вказуй її. "
        "Розкажи як детектив — інтригуючо, але з практичним висновком для підлітка. "
        "Додай інтерактив: 3 факти, один з яких — брехня шахрая."
    )

    base = build_base_prompt(
        rubric_name=RUBRIC_NAME,
        rubric_hashtag=RUBRIC_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=f"Свіжі заголовки про шахрайство:\n{cases or 'немає даних'}",
    )

    prompt = base + """
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{
  "topic": "назва афери (3-5 слів)",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "⚖️ #ФінТруКрайм\\n\\n🎬 [інтригуюча зачіпка 1-2 речення]\\n\\n📋 Що сталося:\\n[суть за 3-4 речення з реальними цифрами]\\n\\n🕵️ Ось 3 факти. Один — брехня шахрая. Який?\\n• [факт 1]\\n• [факт 2]\\n• [факт 3]\\n\\n✅ Як розпізнати таке в житті:\\n[1-2 практичні ознаки]",
  "poll_options": ["Факт 1", "Факт 2", "Факт 3"],
  "correct_index": 1,
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
        "poll_options": data.get("poll_options", []),
        "correct_index": data.get("correct_index", 0),
        "persona": persona["name"],
        "template": template["name"],
    }

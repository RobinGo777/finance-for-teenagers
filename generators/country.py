"""Рубрика про країни у двох режимах.

`country_guess` — прапор і опитування «що це за країна», **без Gemini**.
`country_details` — розбір країни: цифри з Wikidata, абзац із Вікіпедії UA,
Gemini лише переказує ці факти людською мовою.

Дані беремо з Wikidata (українські назви вже в комплекті) і FlagCDN — обидва
без ключів. Довідник кешуємо, бо SPARQL-запит триває десятки секунд.
"""

from __future__ import annotations

import logging
import random

from config import (
    COUNTRY_CACHE_TTL_DAYS,
    COUNTRY_MIN_POPULATION,
    COUNTRY_POLL_OPTIONS,
    COUNTRY_SAME_CONTINENT_ODDS,
    COUNTRY_SEEN_TTL_DAYS,
    VISUAL_TEMPLATES,
)
from data.fetchers import (
    fetch_countries,
    fetch_country_extras,
    fetch_flag_png,
    fetch_wikipedia_summary,
)
from data.redis_client import (
    get_countries_cache,
    get_used_topics,
    is_country_used,
    mark_country_used,
    save_countries_cache,
    save_topic,
)
from generators.gemini import build_base_prompt, generate_json, pick_persona, pick_template
from images.generator import generate_flag_image_async

logger = logging.getLogger(__name__)

GUESS_KEY = "country_guess"
GUESS_NAME = "#ВгадайКраїну"
GUESS_HASHTAG = "🌍 ВгадайКраїну"

DETAILS_KEY = "country_details"
DETAILS_NAME = "#КраїнаВДеталях"
DETAILS_HASHTAG = "🗺️ КраїнаВДеталях"

_TEMPLATE_NAME = "Game Mode"

# Орієнтир для порівнянь: підлітку «1,2 млн км²» ні про що не говорить, а
# «дві України» — говорить. Беремо Україну з того самого довідника, а ці
# значення — лише запас, якщо її там раптом не буде.
_UA_POPULATION_FALLBACK = 41_167_335
_UA_AREA_FALLBACK = 603_550.0

_MAX_NEIGHBOURS = 4
_MAX_WATERS = 2
# Менше — це вже не задача, а вгадування навмання.
_MIN_HINTS = 4

# Wikidata дає офіційні назви, а підліток знає розмовні. Повна назва лишається
# в `full_name` — за нею шукаємо статтю у Вікіпедії.
_SHORT_NAMES = {
    "Китайська Народна Республіка": "Китай",
    "Республіка Китай": "Тайвань",
    "Сполучені Штати Америки": "США",
    "Об'єднані Арабські Емірати": "ОАЕ",
    "Демократична Республіка Конго": "ДР Конго",
    "Південно-Африканська Республіка": "ПАР",
    "Королівство Нідерланди": "Нідерланди",
    "Сполучене Королівство Великої Британії та Північної Ірландії": "Велика Британія",
    "Корейська Народно-Демократична Республіка": "Північна Корея",
}


def _format_population(value: int) -> str:
    if value >= 1_000_000:
        millions = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{millions.replace('.', ',')} млн"
    if value >= 1_000:
        return f"{value // 1000} тис."
    return str(value)


def _format_area(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ") + " км²"


def _lower_first(text: str) -> str:
    """Wikidata віддає валюти то з великої, то з малої літери."""
    return text[:1].lower() + text[1:] if text else text


def _flag_emoji(code: str) -> str:
    """Емодзі прапора з ISO-коду: 'jp' → 🇯🇵."""
    code = (code or "").strip().lower()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(char) - ord("a")) for char in code)


def _game_template() -> dict:
    return next(
        (item for item in VISUAL_TEMPLATES if item["name"] == _TEMPLATE_NAME),
        VISUAL_TEMPLATES[0],
    )


def _display_name(name: str) -> str:
    """Назва, яку бачить читач: у варіантах опитування і в підказках."""
    return _SHORT_NAMES.get(name, name)


def _name_stems(name: str) -> list[str]:
    """Корені назви країни, щоб ловити похідні слова.

    «Танзанія» → «танза», і тоді видно, що «танзанійський шилінг» у підказці
    видає відповідь. Корені беремо короткі: краще прибрати зайвий факт, ніж
    здати відповідь у першому ж рядку («кубинський песо» для Куби).
    """
    stems = []
    for word in (name or "").lower().replace("-", " ").split():
        if len(word) >= 4:
            stems.append(word[: max(3, len(word) - 3)])
    return stems


def _leaks_name(name: str, text: str) -> bool:
    """Чи видає цей факт назву країни."""
    lowered = (text or "").lower()
    return any(stem in lowered for stem in _name_stems(name))


def _is_sea(name: str) -> bool:
    """Море, океан чи затока — а не річка й не озеро."""
    lowered = (name or "").lower()
    return any(
        word in lowered
        for word in ("море", "океан", "затока", "протока", "атлантика", "балтика")
    )


def _stem_source(country: dict) -> str:
    """Обидві назви разом: похідні слова бувають від будь-якої з них."""
    return f"{country.get('full_name') or ''} {country.get('name_uk') or ''}"


def _times_word(value: float) -> str:
    """«у 1,5 раза», «у 3 рази», «у 7 разів»."""
    if abs(value - round(value)) > 0.05:
        return "раза"
    whole = int(round(value))
    if whole % 100 in (11, 12, 13, 14):
        return "разів"
    last = whole % 10
    if last == 1:
        return "раз"
    if last in (2, 3, 4):
        return "рази"
    return "разів"


def _format_ratio(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _compare_to_ukraine(value: float, reference: float) -> str:
    """Порівняння з Україною або порожньо, якщо різниця невідчутна."""
    if not value or not reference:
        return ""
    ratio = value / reference
    if 0.85 <= ratio <= 1.15:
        return "майже як в Україні"
    if ratio > 1:
        return f"у {_format_ratio(ratio)} {_times_word(ratio)} більше, ніж в Україні"
    inverse = reference / value
    return f"у {_format_ratio(inverse)} {_times_word(inverse)} менше, ніж в Україні"


def _hint_lines(country: dict, extras: dict, catalog: list[dict]) -> list[str]:
    """Підказки, з яких країну можна вивести, а не лише вгадати.

    Факти, що містять корінь назви країни (Японське море, ісландська крона),
    відкидаємо — інакше це не задача.
    """
    name = _stem_source(country)
    ukraine = next((item for item in catalog if item["code"] == "ua"), None)
    ua_population = int((ukraine or {}).get("population") or _UA_POPULATION_FALLBACK)
    ua_area = float((ukraine or {}).get("area") or _UA_AREA_FALLBACK)

    lines: list[str] = []
    if country.get("continent"):
        lines.append(f"🌍 Частина світу: {country['continent']}")

    if extras.get("is_island"):
        lines.append("🏝️ Острівна держава")
    elif extras.get("landlocked"):
        lines.append("🏔️ Немає виходу до моря")

    # Сусідів лишаємо лише реальні країни з довідника: Wikidata додає до P47
    # і Європейський Союз, і історичні держави, і залежні території.
    # Шукаємо і за офіційною назвою — саме її P47 і повертає.
    by_name: dict[str, dict] = {}
    for item in catalog:
        by_name[item["name_uk"]] = item
        if full := item.get("full_name"):
            by_name[full] = item

    # Сортуємо за населенням — великого сусіда впізнають, дрібного ні.
    neighbours = sorted(
        (
            by_name[item] for item in (extras.get("neighbours") or [])
            if item in by_name and by_name[item]["code"] != country["code"]
        ),
        key=lambda item: int(item.get("population") or 0),
        reverse=True,
    )[:_MAX_NEIGHBOURS]
    if neighbours:
        names = ", ".join(_display_name(item["name_uk"]) for item in neighbours)
        # В островів P47 — це морські межі, тому «сусіди» звучало б неправдою.
        label = "Поблизу" if extras.get("is_island") else "Сусіди"
        lines.append(f"🧭 {label}: {names}")

    # P206 змішує моря з річками й озерами, а в країни без виходу до моря
    # рядок «омиває» суперечив би попередньому.
    waters = [] if extras.get("landlocked") else [
        item for item in (extras.get("waters") or [])
        if _is_sea(item) and not _leaks_name(name, item)
    ][:_MAX_WATERS]
    if waters:
        lines.append(f"🌊 Омиває: {', '.join(waters)}")

    population = int(country.get("population") or 0)
    if population:
        comparison = _compare_to_ukraine(population, ua_population)
        suffix = f" ({comparison})" if comparison else ""
        lines.append(f"👥 Людей: ≈ {_format_population(population)}{suffix}")

    area = float(country.get("area") or 0)
    if area:
        comparison = _compare_to_ukraine(area, ua_area)
        suffix = f" ({comparison})" if comparison else ""
        lines.append(f"🗺️ Площа: {_format_area(area)}{suffix}")

    # Довідник тримає одну випадкову з офіційних мов (у США це виявилась
    # іспанська), тому для підказки беремо повний список із Wikidata.
    languages = [
        _short_language(item) for item in (extras.get("languages") or [])
        if not _leaks_name(name, item)
    ]
    if not languages and (single := _short_language(country.get("language", ""))):
        languages = [single] if not _leaks_name(name, single) else []
    if len(languages) == 1:
        lines.append(f"🗣️ Мова: {languages[0]}")
    elif 2 <= len(languages) <= 3:
        # «Серед мов», бо офіційних буває більше, ніж ми показуємо, а десь
        # (як у США) державної немає взагалі — лише визнані.
        lines.append(f"🗣️ Серед мов: {', '.join(languages)}")

    currency = _lower_first(country.get("currency", ""))
    if currency and not _leaks_name(name, currency):
        lines.append(f"💰 Гроші: {currency}")

    if extras.get("drives_left"):
        lines.append("🚗 Рух лівосторонній")

    return lines


def _short_language(text: str) -> str:
    return (text or "").removesuffix(" мова").strip()


async def _load_countries() -> list[dict]:
    """Довідник країн із кешу, інакше — запит до Wikidata."""
    cached = await get_countries_cache()
    if cached:
        countries = cached
    else:
        try:
            countries = await fetch_countries()
        except Exception as error:
            logger.warning("[country] Wikidata недоступна: %s", error)
            return []
        await save_countries_cache(countries, COUNTRY_CACHE_TTL_DAYS)

    usable = [
        country
        for country in countries
        if country.get("name_uk")
        and country.get("capital")
        and int(country.get("population") or 0) >= COUNTRY_MIN_POPULATION
    ]

    # Офіційну назву лишаємо для Вікіпедії, у пост іде розмовна.
    for country in usable:
        country.setdefault("full_name", country["name_uk"])
        country["name_uk"] = _display_name(country["full_name"])

    return usable


async def _pick_country(countries: list[dict], mode: str) -> dict | None:
    """Країна, якої давно не було в цьому режимі."""
    pool = list(countries)
    random.shuffle(pool)

    for country in pool:
        if not await is_country_used(country["code"], mode):
            return country

    # Усі вже були — беремо будь-яку, ніж лишати слот порожнім.
    logger.info("[country] Усі країни вже показані в режимі %s", mode)
    return pool[0] if pool else None


def _pick_options(countries: list[dict], answer: dict) -> list[dict]:
    """Варіанти для опитування: переважно сусіди по континенту."""
    needed = max(2, COUNTRY_POLL_OPTIONS) - 1
    same_continent = [
        country
        for country in countries
        if country["code"] != answer["code"]
        and country.get("continent")
        and country["continent"] == answer.get("continent")
    ]
    others = [country for country in countries if country["code"] != answer["code"]]

    use_continent = (
        len(same_continent) >= needed
        and random.random() < COUNTRY_SAME_CONTINENT_ODDS
    )
    source = same_continent if use_continent else others
    if len(source) < needed:
        source = others
    if len(source) < needed:
        return []

    options = random.sample(source, needed) + [answer]
    random.shuffle(options)
    return options


def _facts_block(country: dict, extras: dict | None = None) -> str:
    lines = [
        f"Столиця: {country['capital']}",
        f"Населення: {_format_population(int(country['population']))}",
    ]
    if country.get("area"):
        lines.append(f"Площа: {_format_area(float(country['area']))}")
    if country.get("currency"):
        lines.append(f"Валюта: {_lower_first(country['currency'])}")

    # Довідник тримає лише одну з офіційних мов, тому список із Wikidata точніший.
    languages = [_short_language(item) for item in ((extras or {}).get("languages") or [])]
    if len(languages) > 3:
        languages = languages[:3] + ["…"]
    if languages:
        lines.append(f"Мови: {', '.join(languages)}")
    elif country.get("language"):
        lines.append(f"Мова: {_short_language(country['language'])}")
    if country.get("continent"):
        lines.append(f"Частина світу: {country['continent']}")
    return "\n".join(lines)


async def generate_country_guess() -> dict | None:
    """Прапор + опитування. Gemini не викликається."""
    countries = await _load_countries()
    if len(countries) < COUNTRY_POLL_OPTIONS:
        return None

    answer = await _pick_country(countries, GUESS_KEY)
    if not answer:
        return None

    flag = await fetch_flag_png(answer["code"])
    if not flag:
        logger.info("[country] Немає прапора для %s", answer["code"])
        return None

    options = _pick_options(countries, answer)
    if not options:
        return None

    names = [country["name_uk"] for country in options]
    correct_index = next(
        index for index, country in enumerate(options)
        if country["code"] == answer["code"]
    )

    extras = await fetch_country_extras(answer["code"])
    hints = _hint_lines(answer, extras, countries)
    if len(hints) < _MIN_HINTS:
        logger.info(
            "[country] Для %s мало підказок (%s) — пропускаю",
            answer["code"],
            len(hints),
        )
        return None

    question = "Що це за країна?"
    template = _game_template()
    image = await generate_flag_image_async(
        flag_png=flag,
        template=template,
        caption=question,
        hint=answer.get("continent") or "",
    )

    summary = await fetch_wikipedia_summary(answer.get("full_name") or answer["name_uk"])
    extract = (summary.get("extract") or "").split(". ")
    first_sentence = (extract[0].strip() if extract else "")
    if first_sentence and not first_sentence.endswith("."):
        first_sentence += "."

    lamp_post = (
        f"💡 Відповідь · ВгадайКраїну\n\n"
        f"Це {answer['name_uk']} {_flag_emoji(answer['code'])}\n\n"
        f"{_facts_block(answer, extras)}"
    )
    if first_sentence:
        lamp_post += f"\n\n{first_sentence}"

    await mark_country_used(answer["code"], GUESS_KEY, COUNTRY_SEEN_TTL_DAYS)

    return {
        "rubric": GUESS_KEY,
        "topic": f"прапор: {answer['name_uk']}",
        "question": question,
        "poll_question": question,
        "options": names,
        "correct_index": correct_index,
        "lamp_post": lamp_post,
        "caption": (
            f"{GUESS_HASHTAG}\n\n{question}\n\n"
            + "\n".join(hints)
            + "\n\nГолосуй нижче 👇"
        ),
        "image": image,
        "persona": "Географ",
        "template": template["name"],
    }


async def generate_country_answer(poll_id: str, poll_results: dict) -> str:
    """Відповідь наступного дня зі статистикою голосів."""
    from data.redis_client import get_quiz_pending

    pending = await get_quiz_pending(poll_id)
    if not pending:
        return ""

    text = pending.get("lamp_post", "")
    correct_index = int(pending.get("correct_index", 0))
    votes = sum(int(value) for value in (poll_results or {}).values())
    correct_votes = int((poll_results or {}).get(str(correct_index), 0))

    if votes:
        accuracy = round(correct_votes / votes * 100)
        text += f"\n\n📊 Вгадали: {accuracy}% ({correct_votes} з {votes})"
    else:
        text += "\n\n📊 Цього разу ніхто не проголосував."
    return text


async def generate_country_details() -> dict | None:
    """Розбір країни: цифри з Wikidata + абзац із Вікіпедії, текст від Gemini."""
    countries = await _load_countries()
    if not countries:
        return None

    country = await _pick_country(countries, DETAILS_KEY)
    if not country:
        return None

    summary = await fetch_wikipedia_summary(country.get("full_name") or country["name_uk"])
    wiki_extract = (summary.get("extract") or "").strip()
    extras = await fetch_country_extras(country["code"])
    facts = _facts_block(country, extras)
    if neighbours := [_display_name(item) for item in (extras.get("neighbours") or [])][:6]:
        facts += f"\nСусіди: {', '.join(neighbours)}"

    persona = pick_persona()
    template = await pick_template()
    used_topics = await get_used_topics(DETAILS_KEY)

    task = (
        f"Розкажи про країну {country['name_uk']} так, щоб підлітку було цікаво "
        "читати. Використовуй ЛИШЕ факти з даних нижче: не вигадуй цифр, дат, "
        "рекордів і «цікавинок», яких там немає. Якщо чогось не знаєш — не пиши "
        "про це. Знайди в цих фактах те, що справді дивує (масштаб, розташування, "
        "гроші, мова), і побудуй пост навколо цього."
    )
    base = build_base_prompt(
        rubric_name=DETAILS_NAME,
        rubric_hashtag=DETAILS_HASHTAG,
        task=task,
        used_topics=used_topics,
        persona=persona,
        extra_data=(
            f"Дані Wikidata:\n{facts}\n\n"
            f"Опис із української Вікіпедії:\n{wiki_extract or 'немає'}"
        ),
    )
    prompt = base + """
ФОРМАТ ВІДПОВІДІ (тільки JSON):
{
  "topic": "назва країни",
  "title": "заголовок для картинки (макс 8 слів)",
  "post": "🗺️ КраїнаВДеталях\\n\\n[назва країни] — [чим одразу зачіпає]\\n\\n📍 Де це: [розташування простими словами]\\n👥 Людей: [населення і що це означає на дотик — порівняння з Києвом чи Україною]\\n💰 Гроші: [валюта і чим вона цікава]\\n🗣️ Мова: [мова]\\n\\n🤯 Що дивує найбільше: [один факт із даних]\\n\\n💬 [питання до читача]",
  "body_preview": "одне конкретне речення про країну без емодзі"
}
"""
    data = await generate_json(prompt)

    flag = await fetch_flag_png(country["code"])
    image = await generate_flag_image_async(
        flag_png=flag,
        template=template,
        caption=country["name_uk"],
        # Дрібний рядок не переносимо, тому туди йде щось коротке.
        hint=country.get("continent") or DETAILS_NAME,
    )

    topic = data.get("topic") or country["name_uk"]
    await save_topic(DETAILS_KEY, topic)
    await mark_country_used(country["code"], DETAILS_KEY, COUNTRY_SEEN_TTL_DAYS)

    return {
        "rubric": DETAILS_KEY,
        "topic": topic,
        "post": data["post"],
        "image": image,
        "persona": persona["name"],
        "template": template["name"],
    }

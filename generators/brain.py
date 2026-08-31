"""Рубрика #ТренажерМозку — задачі у стилі «Клуб 1%».

Більшість типів генерує код (піраміда, дзеркало, послідовності, ребуси,
детективи). Тип `clever` — кмітливі задачі від Gemini: відповідь має
виводитись з умови, а не з знань. На картинці — велика зона задачі й бейдж
«лише N%», як у шоу.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import random
from dataclasses import dataclass, field

from config import (
    BRAIN_LEVEL_DOWN_ACCURACY,
    BRAIN_LEVEL_MAX,
    BRAIN_LEVEL_MIN,
    BRAIN_LEVEL_UP_ACCURACY,
    BRAIN_MIN_VOTES_FOR_ADAPT,
    BRAIN_SEEN_TTL_DAYS,
    BRAIN_START_LEVEL,
    BRAIN_TYPES,
    VISUAL_TEMPLATES,
)
from data.redis_client import (
    get_brain_level,
    get_brain_stats,
    is_brain_puzzle_seen,
    mark_brain_puzzle_seen,
    save_brain_stats,
    set_brain_level,
)
from data.uk_words import (
    ALIBI_PLACES,
    ALIBI_SCENES,
    CLOSE_CATEGORY_PAIRS,
    DISTANT_CATEGORY_PAIRS,
    LIAR_EVENTS,
    NAMES,
    ORDER_SCENARIOS,
    PLACE_ORDINALS,
    REBUS_DECOYS,
    REBUSES,
    THEMED_WORDS,
    UK_ALPHABET,
    category_of,
    words_of_length,
)
from images.generator import generate_brain_image_async

logger = logging.getLogger(__name__)

RUBRIC_KEY = "brain"
RUBRIC_NAME = "#ТренажерМозку"
RUBRIC_HASHTAG = "🧩 ТренажерМозку"

KIND_LABELS = {
    "anagram": "анаграма",
    "caesar": "шифр",
    "sequence": "закономірність",
    "odd_one_out": "зайве слово",
    "rebus": "ребус",
    "alibi": "детектив: алібі",
    "liar": "детектив: хто бреше",
    "order": "логіка: порядок",
    "pyramid": "піраміда чисел",
    "mirror": "дзеркало літер",
    "hidden_seq": "прихована послідовність",
    "clever": "кмітливість",
}

# Орієнтир «скільки людей розв'язують» — як у шоу, не як шкільна оцінка.
_PERCENT_BY_LEVEL = {1: 90, 2: 70, 3: 45, 4: 20, 5: 5}

# Ваги в ротації: нові «клубні» типи частіше, старі анаграми/Цезар — рідше.
_KIND_WEIGHTS = {
    "clever": 4,
    "pyramid": 3,
    "mirror": 3,
    "hidden_seq": 3,
    "sequence": 2,
    "rebus": 2,
    "odd_one_out": 2,
    "anagram": 1,
    "caesar": 1,
    "alibi": 1,
    "liar": 1,
    "order": 1,
}

# Довжина слова за рівнем складності для анаграм і шифру.
_LENGTH_BY_LEVEL = {1: (4, 5), 2: (5, 6), 3: (6, 7), 4: (7, 9), 5: (8, 12)}
# Зсув шифру за рівнем + чи показувати підказку.
_SHIFT_BY_LEVEL = {1: (1, 3, True), 2: (1, 5, True), 3: (2, 8, False),
                   4: (3, 12, False), 5: (4, 16, False)}

_MAX_ATTEMPTS = 30

# Літери, що збігаються зі своїм дзеркальним відображенням (вертикальна вісь).
_MIRROR_OK = tuple("АЖМНОТФХШІ")
_MIRROR_BAD = tuple("БВГҐДЕЄЗИЙКЛПРСУЦЧЩЬЮЯ")

_MONTHS = (
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
)
_WEEKDAYS = (
    "понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя",
)
_NUMBER_WORDS = (
    "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять", "десять",
)


@dataclass
class Puzzle:
    kind: str
    question: str
    options: list[str]
    correct_index: int
    explanation: str
    fingerprint: str
    # Логічні задачі мають довгу умову: вона йде в підпис до картинки, а в
    # саме опитування — коротке питання (ліміт Telegram — 300 символів).
    poll_question: str = ""
    image_text: str = ""
    # Формальна умова логічної задачі (інтервали, показання, обмеження) —
    # щоб солвер можна було прогнати ще раз у тестах, а не звіряти текст.
    model: dict = field(default_factory=dict)

    @property
    def poll_prompt(self) -> str:
        return self.poll_question or self.question

    @property
    def card_text(self) -> str:
        return self.image_text or self.poll_prompt


def _fingerprint(kind: str, answer: str, extra: str = "") -> str:
    digest = hashlib.md5(f"{kind}|{answer}|{extra}".encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def _shuffled_options(answer: str, decoys: list[str]) -> tuple[list[str], int]:
    """Перемішує варіанти й повертає позицію правильного."""
    options = [answer] + decoys
    random.shuffle(options)
    return options, options.index(answer)


def _pick_decoys(pool: list[str], answer: str, count: int = 3) -> list[str]:
    candidates = [w for w in dict.fromkeys(pool) if w != answer]
    if len(candidates) < count:
        return []
    return random.sample(candidates, count)


def _titles(words: list[str]) -> list[str]:
    return [w.capitalize() for w in words]


def _make_anagram(level: int) -> Puzzle | None:
    low, high = _LENGTH_BY_LEVEL[level]
    pool = list(words_of_length(low, high))
    if len(pool) < 4:
        return None

    word = random.choice(pool)
    letters = list(word)
    for _ in range(10):
        random.shuffle(letters)
        if "".join(letters) != word:
            break
    else:
        return None

    decoys = _pick_decoys(pool, word)
    if not decoys:
        return None

    options, correct = _shuffled_options(word, decoys)
    scrambled = " ".join(letter.upper() for letter in letters)
    category = category_of(word)
    return Puzzle(
        kind="anagram",
        question="Збери слово з цих літер",
        options=_titles(options),
        correct_index=correct,
        explanation=(
            f"З цих літер складається слово «{word}»"
            + (f" (тема: {category})." if category else ".")
        ),
        fingerprint=_fingerprint("anagram", word),
        poll_question="Яке слово вийде?",
        image_text=scrambled,
    )


def _caesar_encode(word: str, shift: int) -> str:
    out = []
    for char in word:
        index = UK_ALPHABET.find(char)
        if index < 0:
            return ""
        out.append(UK_ALPHABET[(index + shift) % len(UK_ALPHABET)])
    return "".join(out)


def _make_caesar(level: int) -> Puzzle | None:
    low, high = _LENGTH_BY_LEVEL[level]
    min_shift, max_shift, show_hint = _SHIFT_BY_LEVEL[level]
    pool = [w for w in words_of_length(low, high) if _caesar_encode(w, 1)]
    if len(pool) < 4:
        return None

    word = random.choice(pool)
    shift = random.randint(min_shift, max_shift)
    encoded = _caesar_encode(word, shift)
    if not encoded or encoded == word:
        return None

    decoys = _pick_decoys(pool, word)
    if not decoys:
        return None

    options, correct = _shuffled_options(word, decoys)
    question = "Розшифруй слово"
    if show_hint:
        question += f" (зсув +{shift})"
    return Puzzle(
        kind="caesar",
        question=question,
        options=_titles(options),
        correct_index=correct,
        explanation=(
            f"Зсув +{shift} за українським алфавітом: "
            f"{encoded[0].upper()} → {word[0].upper()}. Слово: «{word}»."
        ),
        fingerprint=_fingerprint("caesar", word, str(shift)),
        poll_question="Яке слово зашифроване?",
        image_text=encoded.upper(),
    )


def _sequence_rule(level: int) -> tuple[list[int], int, str] | None:
    """Повертає (показані числа, наступне, опис правила)."""
    if level <= 1:
        start, step = random.randint(1, 9), random.randint(2, 5)
        terms = [start + step * i for i in range(6)]
        return terms[:5], terms[5], f"кожне наступне більше на {step}"

    if level == 2:
        start, step = random.randint(20, 60), random.randint(6, 12)
        if random.random() < 0.4:
            step = -step
            start = random.randint(80, 140)
        terms = [start + step * i for i in range(6)]
        word = "більше" if step > 0 else "менше"
        return terms[:5], terms[5], f"кожне наступне {word} на {abs(step)}"

    if level == 3:
        if random.random() < 0.5:
            start, ratio = random.randint(1, 4), random.choice([2, 3])
            terms = [start * ratio**i for i in range(6)]
            return terms[:5], terms[5], f"кожне наступне у {ratio} рази більше"
        start = random.randint(2, 8)
        a, b = random.randint(2, 5), random.randint(6, 11)
        terms = [start]
        for i in range(5):
            terms.append(terms[-1] + (a if i % 2 == 0 else b))
        return terms[:5], terms[5], f"крок чергується: +{a}, +{b}, +{a}, +{b}"

    if level == 4:
        choice = random.choice(["squares", "fib", "double_plus"])
        if choice == "squares":
            start = random.randint(2, 5)
            terms = [(start + i) ** 2 for i in range(6)]
            return terms[:5], terms[5], f"це квадрати чисел від {start}"
        if choice == "fib":
            a, b = random.randint(1, 4), random.randint(2, 6)
            terms = [a, b]
            while len(terms) < 6:
                terms.append(terms[-1] + terms[-2])
            return terms[:5], terms[5], "кожне число — сума двох попередніх"
        start = random.randint(1, 5)
        terms = [start]
        while len(terms) < 6:
            terms.append(terms[-1] * 2 + 1)
        return terms[:5], terms[5], "кожне наступне = попереднє ×2 +1"

    choice = random.choice(["interleaved", "triple_minus", "triangular"])
    if choice == "interleaved":
        a, b = random.randint(2, 9), random.randint(20, 40)
        da, db = random.randint(3, 7), random.randint(4, 9)
        terms = [a, b, a + da, b + db, a + 2 * da, b + 2 * db]
        return terms[:5], terms[5], (
            f"тут два ряди через одне число: один додає {da}, інший — {db}"
        )
    if choice == "triple_minus":
        start = random.randint(2, 5)
        terms = [start]
        while len(terms) < 6:
            terms.append(terms[-1] * 3 - 2)
        return terms[:5], terms[5], "кожне наступне = попереднє ×3 −2"
    start = random.randint(2, 5)
    terms = [sum(range(1, n + 1)) for n in range(start, start + 6)]
    return terms[:5], terms[5], "це трикутні числа: 1+2, 1+2+3, і так далі"


def _make_sequence(level: int) -> Puzzle | None:
    built = _sequence_rule(level)
    if not built:
        return None
    shown, answer, rule = built
    if any(n < 0 for n in shown) or answer < 0:
        return None

    spread = max(2, abs(answer - shown[-1]))
    candidates = {
        answer + spread,
        answer - spread,
        answer + max(1, spread // 2),
        answer - max(1, spread // 2),
        answer + 1,
        answer - 1,
    }
    pool = [n for n in candidates if n != answer and n >= 0]
    if len(pool) < 3:
        return None
    decoys = random.sample(pool, 3)

    options, correct = _shuffled_options(str(answer), [str(d) for d in decoys])
    shown_str = "  ".join(str(n) for n in shown) + "  ?"
    return Puzzle(
        kind="sequence",
        question="Яке число наступне?",
        options=options,
        correct_index=correct,
        explanation=f"Правило: {rule}. Відповідь: {answer}.",
        fingerprint=_fingerprint("sequence", str(answer), shown_str),
        poll_question="Яке число наступне?",
        image_text=shown_str,
    )


def _make_odd_one_out(level: int) -> Puzzle | None:
    pairs = DISTANT_CATEGORY_PAIRS if level <= 2 else CLOSE_CATEGORY_PAIRS
    main_cat, odd_cat = random.choice(pairs)
    main_words = list(THEMED_WORDS.get(main_cat, ()))
    odd_words = list(THEMED_WORDS.get(odd_cat, ()))
    if len(main_words) < 3 or not odd_words:
        return None

    trio = random.sample(main_words, 3)
    odd = random.choice(odd_words)
    options, correct = _shuffled_options(odd, trio)
    body = "   ·   ".join(w.upper() for w in options)
    return Puzzle(
        kind="odd_one_out",
        question="Яке слово тут зайве?",
        options=_titles(options),
        correct_index=correct,
        explanation=(
            f"«{odd}» — це {odd_cat}, а решта слів — {main_cat}."
        ),
        fingerprint=_fingerprint("odd_one_out", odd, "|".join(sorted(trio))),
        poll_question="Яке слово зайве?",
        image_text=body,
    )


def _make_rebus(level: int) -> Puzzle | None:
    puzzle_text, answer, reading = random.choice(REBUSES)
    decoys = _pick_decoys(list(REBUS_DECOYS), answer)
    if not decoys:
        return None
    options, correct = _shuffled_options(answer, decoys)
    return Puzzle(
        kind="rebus",
        question="Що тут зашифровано?",
        options=_titles(options),
        correct_index=correct,
        explanation=f"{puzzle_text} читається як {reading} → «{answer}».",
        fingerprint=_fingerprint("rebus", answer),
        poll_question="Що зашифровано?",
        image_text=puzzle_text,
    )


def _format_pyramid(rows: list[list[str]]) -> str:
    """Малює піраміду з вирівнюванням по центру."""
    cell = max(len(value) for row in rows for value in row) + 2
    width = len(rows[-1]) * cell
    lines = []
    for row in rows:
        chunk = "".join(value.center(cell) for value in row)
        lines.append(chunk.center(width).rstrip())
    return "\n".join(lines)


def _make_pyramid(level: int) -> Puzzle | None:
    """Піраміда: кожне число = сума двох під ним. Знайти верхнє."""
    bottom_len = 3 if level <= 2 else 4
    max_leaf = 4 if level <= 2 else (6 if level <= 4 else 9)
    bottom = [random.randint(1, max_leaf) for _ in range(bottom_len)]
    rows: list[list[int]] = [bottom]
    while len(rows[0]) > 1:
        parent = [rows[0][i] + rows[0][i + 1] for i in range(len(rows[0]) - 1)]
        rows.insert(0, parent)

    answer = rows[0][0]
    display: list[list[str]] = [
        ["?" if (r == 0 and c == 0) else str(value) for c, value in enumerate(row)]
        for r, row in enumerate(rows)
    ]
    # З рівня 4 ховаємо ще одне число посередині — складніше.
    if level >= 4 and len(rows) >= 3:
        mid = len(rows) // 2
        hide_col = random.randrange(len(rows[mid]))
        if not (mid == 0 and hide_col == 0):
            display[mid][hide_col] = "?"

    spread = max(3, answer // 4)
    decoy_pool = {
        answer + spread,
        answer - spread,
        answer + spread * 2,
        answer - max(1, spread // 2),
        sum(bottom),
        answer + 1,
    }
    decoys = [str(n) for n in decoy_pool if n != answer and n > 0]
    if len(decoys) < 3:
        return None
    options, correct = _shuffled_options(str(answer), random.sample(decoys, 3))
    return Puzzle(
        kind="pyramid",
        question="Яке число має стояти зверху? Кожне число — сума двох під ним.",
        options=options,
        correct_index=correct,
        explanation=(
            f"Кожне число = сума двох під ним. Зверху виходить {answer}."
        ),
        fingerprint=_fingerprint("pyramid", str(answer), "|".join(map(str, bottom))),
        poll_question="Яке число зверху?",
        image_text=_format_pyramid(display),
        model={"rows": rows, "answer": answer},
    )


def _make_mirror(level: int) -> Puzzle | None:
    """Яка літера не збігається зі своїм дзеркальним відображенням?"""
    count = 4 if level <= 2 else 5
    bad = random.choice(_MIRROR_BAD)
    good = random.sample(_MIRROR_OK, count - 1)
    letters = good + [bad]
    random.shuffle(letters)

    # На складних рівнях питання інвертуємо: «яка збігається» серед переважно асиметричних.
    find_asymmetric = level <= 3
    if find_asymmetric:
        answer = bad
        decoys = list(good)
        question = "Яка літера НЕ збігається зі своїм дзеркальним відображенням?"
        explanation = (
            f"Літера «{bad}» змінюється у дзеркалі. "
            f"«{', '.join(good)}» виглядають так само."
        )
        poll = "Яка літера несиметрична?"
    else:
        answer = random.choice(good)
        decoys = [bad] + [g for g in good if g != answer]
        decoys = decoys[:3]
        while len(decoys) < 3:
            extra = random.choice(_MIRROR_BAD)
            if extra not in decoys and extra != answer:
                decoys.append(extra)
        question = "Яка літера збігається зі своїм дзеркальним відображенням?"
        explanation = (
            f"«{answer}» виглядає так само у дзеркалі. Решта — ні."
        )
        poll = "Яка літера симетрична?"
        letters = [answer, bad] + random.sample(
            [g for g in _MIRROR_OK if g != answer], 2
        )
        random.shuffle(letters)

    options, correct = _shuffled_options(answer, decoys[:3])
    body = "   ".join(letters)
    return Puzzle(
        kind="mirror",
        question=question,
        options=options,
        correct_index=correct,
        explanation=explanation,
        fingerprint=_fingerprint("mirror", answer, "".join(sorted(letters))),
        poll_question=poll,
        image_text=body,
    )


def _make_hidden_seq(level: int) -> Puzzle | None:
    """Кілька слів злиті докупи без пробілів — що наступне в ряду?"""
    kind = random.choice(["months", "weekdays", "numbers"])
    if kind == "months":
        series = list(_MONTHS)
    elif kind == "weekdays":
        series = list(_WEEKDAYS)
    else:
        series = list(_NUMBER_WORDS)

    shown_count = 3 if level <= 3 else 4
    if len(series) < shown_count + 1:
        return None
    start = random.randint(0, len(series) - shown_count - 1)
    chunk = series[start:start + shown_count]
    answer = series[start + shown_count]

    # Відволікаючі: сусідні в ряду + випадкові з того ж списку.
    decoy_pool = []
    for offset in (-1, 1, 2, shown_count + 1):
        index = start + shown_count + offset
        if 0 <= index < len(series) and series[index] != answer:
            decoy_pool.append(series[index])
    decoy_pool.extend(w for w in series if w != answer and w not in chunk)
    decoys = list(dict.fromkeys(decoy_pool))
    if len(decoys) < 3:
        return None

    # На картинці слова злиті ВЕЛИКИМИ літерами — треба «побачити» межі.
    parts = [word.upper().replace("'", "") for word in chunk]
    if level >= 4:
        # Зайва літера між словами — класичний трюк «побач зайве».
        junk = random.choice("БГДЖЗКЛМПРСТФХЦЧШЩ")
        glued = junk.join(parts)
    else:
        glued = "".join(parts)

    options, correct = _shuffled_options(answer, random.sample(decoys, 3))
    label = {
        "months": "місяці",
        "weekdays": "дні тижня",
        "numbers": "числа словами",
    }[kind]
    return Puzzle(
        kind="hidden_seq",
        question=f"Тут зашифровано {label} підряд. Що наступне?",
        options=_titles(options),
        correct_index=correct,
        explanation=(
            f"У рядку сховано: {', '.join(chunk)}. "
            f"Наступне — «{answer}»."
        ),
        fingerprint=_fingerprint("hidden_seq", answer, glued),
        poll_question="Що наступне?",
        image_text=glued,
        model={"series": chunk, "answer": answer, "kind": kind},
    )


async def _make_clever(level: int) -> Puzzle | None:
    """Кмітлива задача від Gemini у стилі «Клуб 1%»."""
    from generators.gemini import generate_json

    percent = _PERCENT_BY_LEVEL.get(level, 45)
    themes = [
        "літери й слова українською",
        "числа й прості закономірності",
        "розташування слів (над/під/у/без)",
        "абревіатури й перші літери",
        "логіка без спеціальних знань",
        "дзеркала, симетрія, форми літер",
    ]
    theme = random.choice(themes)
    prompt = f"""Ти автор завдань для шоу на кмітливість на кшталт «Клуб 1%».
Аудиторія — підлітки України 12–20 років.

Зроби ОДНУ задачу українською.
Тема-орієнтир: {theme}.
Орієнтовна складність: на таке питання відповідає близько {percent}% людей.

ПРАВИЛА (жорстко):
- Відповідь виводиться ЛИШЕ з умови. Жодних знань фактів, дат, столиць, зірок.
- Рівно одна правильна відповідь.
- Без жестокості, політики, релігії, сексу, алкоголю.
- Без англійських слів у умові (українська).
- Умова коротка: максимум 4 рядки в полі body, кожен рядок до 40 символів.
- body — те, що буде ВЕЛИКИМ на картинці (літери, числа, схема текстом).
- question — коротке питання під картинкою (до 120 символів).
- options — рівно 4 короткі варіанти (до 40 символів кожен), один правильний.
- explanation — 1–2 речення, чому саме ця відповідь.
- Не пиши правильну відповідь у body чи question відкритим текстом.

Формат відповіді — ТІЛЬКИ JSON:
{{
  "body": "рядок1\\nрядок2",
  "question": "Що далі?",
  "options": ["А", "Б", "В", "Г"],
  "correct_index": 0,
  "explanation": "Бо ..."
}}
"""
    try:
        data = await generate_json(prompt)
    except Exception as error:
        logger.warning("[brain] clever: Gemini не відповів (%s)", error)
        return None

    if not isinstance(data, dict):
        return None
    body = str(data.get("body") or "").strip()
    question = str(data.get("question") or "").strip()
    explanation = str(data.get("explanation") or "").strip()
    options_raw = data.get("options") or []
    try:
        correct = int(data.get("correct_index"))
    except (TypeError, ValueError):
        return None

    if not body or not question or not explanation:
        return None
    if not isinstance(options_raw, list) or len(options_raw) != 4:
        return None
    options = [str(item).strip() for item in options_raw]
    if any(len(item) < 1 or len(item) > 60 for item in options):
        return None
    if len(set(options)) < 4:
        return None
    if not 0 <= correct < 4:
        return None
    if len(body) > 220 or len(question) > 200:
        return None

    return Puzzle(
        kind="clever",
        question=question,
        options=options,
        correct_index=correct,
        explanation=explanation,
        fingerprint=_fingerprint("clever", options[correct], body[:80]),
        poll_question=question if len(question) <= 300 else "Яка відповідь?",
        image_text=body,
    )


# ─────────────────────────────────────────
# ДЕТЕКТИВНИЙ КЛУБ: задачі з перебором розв'язків
# ─────────────────────────────────────────
# Тут принципово інший підхід: спочатку будуємо умову, потім перебираємо ВСІ
# варіанти й лишаємо задачу лише тоді, коли розв'язок рівно один. Саме цього
# не гарантує генерація тексту моделлю — вона легко видає задачу без відповіді
# або з двома.

def _verb(person: dict, masculine: str, feminine: str) -> str:
    return feminine if person.get("g") == "f" else masculine


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _people_word(count: int) -> str:
    return {3: "Троє", 4: "Четверо", 5: "П'ятеро"}.get(count, f"{count}")


def _can_reach_scene(start: int, end: int, event: int, travel: int) -> bool:
    """Чи міг бути на місці подій, враховуючи дорогу в обидві сторони."""
    return event + travel <= start or end + travel <= event


def _make_alibi(level: int) -> Puzzle | None:
    count = 4 if level <= 3 else 5
    if len(NAMES) < count or len(ALIBI_PLACES) < count:
        return None

    travel = 0 if level <= 2 else random.choice([5, 10, 15])
    scene = random.choice(ALIBI_SCENES)
    event = random.choice([13 * 60 + 30, 14 * 60, 14 * 60 + 30, 15 * 60, 15 * 60 + 15])

    people = random.sample(list(NAMES), count)
    # Місце самої події не може бути чиїмось алібі — інакше умова суперечлива.
    place_pool = [p for p in ALIBI_PLACES if p["gen"] != scene["scene_gen"]]
    if len(place_pool) < count:
        return None
    places = random.sample(place_pool, count)
    culprit = random.randrange(count)
    # Один невинний виходить майже вчасно — але дорога все одно не дозволяє.
    near_miss = -1
    if travel:
        others = [i for i in range(count) if i != culprit]
        near_miss = random.choice(others)

    windows: list[tuple[int, int]] = []
    culprit_after_event = False
    for index in range(count):
        if index == culprit:
            culprit_after_event = random.random() < 0.5
            if culprit_after_event:
                start = event + travel + random.randint(5, 25)
                end = start + random.randint(30, 70)
            else:
                end = event - travel - random.randint(5, 25)
                start = end - random.randint(30, 70)
        elif index == near_miss:
            # Пішов до події, але з дорогою вже не встигав.
            end = event - travel + random.randint(1, max(1, travel - 1))
            start = end - random.randint(30, 70)
        else:
            start = event - random.randint(10, 40)
            end = event + random.randint(10, 40)
        windows.append((start, end))

    # Солвер: підозрюваним лишається рівно один.
    could = [
        index
        for index, (start, end) in enumerate(windows)
        if _can_reach_scene(start, end, event, travel)
    ]
    if could != [culprit]:
        return None
    if any(start < 0 or end > 24 * 60 for start, end in windows):
        return None

    lines = [
        f"{scene['event'][0].upper()}{scene['event'][1:]} — це сталося о {_hhmm(event)}.",
        "",
    ]
    for person, place, (start, end) in zip(people, places, windows):
        lines.append(
            f"• {person['nom']} {_verb(person, 'був', 'була')} {place['loc']} "
            f"з {_hhmm(start)} до {_hhmm(end)}"
        )
    if travel:
        lines += [
            "",
            f"Дорога до {scene['scene_gen']} з будь-якого з цих місць — "
            f"{travel} хвилин.",
        ]
    poll_question = "Хто міг це зробити?"
    body = "\n".join(lines)
    lines += ["", poll_question]

    guilty = people[culprit]
    start, end = windows[culprit]
    if culprit_after_event:
        reason = (
            f"алібі {guilty['gen']} починається лише о {_hhmm(start)}, "
            f"тобто о {_hhmm(event)} воно ще не діяло"
        )
    else:
        reason = f"алібі {guilty['gen']} закінчилося о {_hhmm(end)}"
        if travel:
            reason += (
                f", і навіть з {travel} хв дороги це {_hhmm(end + travel)} — "
                f"{_verb(guilty, 'встигав', 'встигала')} до {_hhmm(event)}"
            )
        else:
            reason += f", тож на {_hhmm(event)} воно вже не діяло"
    explanation = f"Відповідь: {guilty['nom']} — {reason}."
    if near_miss >= 0:
        other = people[near_miss]
        o_end = windows[near_miss][1]
        explanation += (
            f" Найпідступніший варіант — {other['nom']}: алібі закінчилося о "
            f"{_hhmm(o_end)}, але з дорогою {other['nom']} "
            f"{_verb(other, 'був би', 'була б')} на місці лише о "
            f"{_hhmm(o_end + travel)} — запізно."
        )
    explanation += (
        f" Усі інші о {_hhmm(event)} або були під алібі, або не встигали дійти."
    )

    return Puzzle(
        kind="alibi",
        question="\n".join(lines),
        options=[p["nom"] for p in people],
        correct_index=culprit,
        explanation=explanation,
        fingerprint=_fingerprint(
            "alibi",
            guilty["nom"],
            f"{scene['event']}|{event}|{travel}|"
            + "|".join(f"{s}-{e}" for s, e in windows),
        ),
        poll_question=poll_question,
        image_text=body,
        model={"windows": windows, "event": event, "travel": travel},
    )


def _make_liar(level: int) -> Puzzle | None:
    count = 4 if level <= 3 else 5
    if len(NAMES) < count:
        return None

    one_truth_mode = level >= 4
    people = random.sample(list(NAMES), count)
    event = random.choice(LIAR_EVENTS)

    statements: list[tuple[str, int]] = []
    for index in range(count):
        if random.random() < 0.45:
            statements.append(("deny", -1))
        else:
            target = random.choice([i for i in range(count) if i != index])
            statements.append(("accuse", target))

    def truth_flags(culprit: int) -> list[bool]:
        flags = []
        for index, (kind, target) in enumerate(statements):
            if kind == "deny":
                flags.append(culprit != index)
            else:
                flags.append(culprit == target)
        return flags

    valid = []
    for candidate in range(count):
        flags = truth_flags(candidate)
        truthful = flags.count(True)
        liars = flags.count(False)
        if (truthful == 1) if one_truth_mode else (liars == 1):
            valid.append(candidate)
    if len(valid) != 1:
        return None
    culprit = valid[0]

    lines = [f"Хтось {event}. Ось що кажуть свідки:", ""]
    for person, (kind, target) in zip(people, statements):
        if kind == "deny":
            lines.append(f"• {person['nom']}: «Це не я»")
        else:
            other = people[target]
            lines.append(
                f"• {person['nom']}: «Це {_verb(other, 'зробив', 'зробила')} "
                f"{other['nom']}»"
            )
    condition = (
        "Відомо: рівно один із них сказав правду."
        if one_truth_mode
        else "Відомо: рівно один із них збрехав."
    )
    poll_question = "Хто це зробив?"
    body = "\n".join(lines + ["", condition])
    lines += ["", condition, "", poll_question]

    guilty = people[culprit]
    flags = truth_flags(culprit)
    if one_truth_mode:
        truthful = [people[i] for i, ok in enumerate(flags) if ok][0]
        detail = (
            f"правду {_verb(truthful, 'сказав', 'сказала')} лише "
            f"{truthful['nom']}, а решта показань хибні"
        )
    else:
        liar = [people[i] for i, ok in enumerate(flags) if not ok][0]
        detail = f"єдина неправда — слова {liar['gen']}"
    explanation = (
        f"{_verb(guilty, 'Винен', 'Винна')} {guilty['nom']}. Тоді {detail}, "
        f"рівно як в умові. Кожен інший підозрюваний дає іншу кількість брехунів."
    )

    return Puzzle(
        kind="liar",
        question="\n".join(lines),
        options=[p["nom"] for p in people],
        correct_index=culprit,
        explanation=explanation,
        fingerprint=_fingerprint(
            "liar",
            guilty["nom"],
            f"{event}|{one_truth_mode}|"
            + "|".join(f"{k}{t}" for k, t in statements),
        ),
        poll_question=poll_question,
        image_text=body,
        model={"statements": statements, "one_truth": one_truth_mode},
    )


def _order_holds(constraint: tuple, positions: dict[int, int]) -> bool:
    kind = constraint[0]
    if kind == "before":
        return positions[constraint[1]] < positions[constraint[2]]
    if kind == "imm":
        return positions[constraint[1]] == positions[constraint[2]] + 1
    return positions[constraint[1]] != constraint[2]


def _order_solutions(constraints: list[tuple], count: int) -> list[tuple[int, ...]]:
    found = []
    for candidate in itertools.permutations(range(count)):
        positions = {person: index for index, person in enumerate(candidate)}
        if all(_order_holds(c, positions) for c in constraints):
            found.append(candidate)
            if len(found) > 1:
                break
    return found


def _make_order(level: int) -> Puzzle | None:
    count = 4 if level <= 3 else 5
    if len(NAMES) < count:
        return None

    people = random.sample(list(NAMES), count)
    scenario = random.choice(ORDER_SCENARIOS)
    target = list(range(count))
    random.shuffle(target)
    positions = {person: index for index, person in enumerate(target)}

    pool: list[tuple] = []
    for a, b in itertools.permutations(range(count), 2):
        if positions[a] < positions[b]:
            pool.append(("before", a, b))
        if positions[a] == positions[b] + 1:
            pool.append(("imm", a, b))
    for person in range(count):
        for place in range(count):
            if positions[person] != place:
                pool.append(("notpos", person, place))
    random.shuffle(pool)
    # «Не був на N-му місці» лишаємо на потім: задача цікавіша, коли тримається
    # на зв'язках між людьми, а не на переліку заборонених місць.
    pool.sort(key=lambda constraint: constraint[0] == "notpos")

    chosen: list[tuple] = []
    for constraint in pool:
        if len(chosen) >= count + 2:
            break
        chosen.append(constraint)
        if len(_order_solutions(chosen, count)) == 1:
            break
    if len(_order_solutions(chosen, count)) != 1:
        return None

    # Прибираємо надлишкові умови — задача має бути охайною.
    minimal = list(chosen)
    for constraint in list(minimal):
        trimmed = [c for c in minimal if c != constraint]
        if trimmed and len(_order_solutions(trimmed, count)) == 1:
            minimal = trimmed
    if len(minimal) < 2:
        return None

    lines = [f"{_people_word(count)} друзів {scenario['intro']}.", ""]
    for constraint in minimal:
        person = people[constraint[1]]
        verb = _verb(person, scenario["m"], scenario["f"])
        if constraint[0] == "before":
            other = people[constraint[2]]
            lines.append(
                f"• {person['nom']} {verb} раніше, ніж {other['nom']}"
            )
        elif constraint[0] == "imm":
            other = people[constraint[2]]
            lines.append(
                f"• {person['nom']} {verb} відразу після {other['gen']}"
            )
        else:
            lines.append(
                f"• {person['nom']} не {verb} {PLACE_ORDINALS[constraint[2]]}"
            )

    asked = random.randrange(count)
    poll_question = f"Хто {scenario['m']} {PLACE_ORDINALS[asked]}?"
    body = "\n".join(lines)
    lines += ["", poll_question]

    answer_index = target[asked]
    chain = " → ".join(people[person]["nom"] for person in target)
    explanation = (
        f"Єдиний порядок, що задовольняє всі умови: {chain}. "
        f"Тому {PLACE_ORDINALS[asked]} — {people[answer_index]['nom']}."
    )

    return Puzzle(
        kind="order",
        question="\n".join(lines),
        options=[p["nom"] for p in people],
        correct_index=answer_index,
        explanation=explanation,
        fingerprint=_fingerprint(
            "order",
            people[answer_index]["nom"],
            f"{scenario['intro']}|{asked}|"
            + "|".join(str(c) for c in sorted(minimal)),
        ),
        poll_question=poll_question,
        image_text=body,
        model={"constraints": minimal, "order": tuple(target), "asked": asked},
    )


_BUILDERS = {
    "anagram": _make_anagram,
    "caesar": _make_caesar,
    "sequence": _make_sequence,
    "odd_one_out": _make_odd_one_out,
    "rebus": _make_rebus,
    "alibi": _make_alibi,
    "liar": _make_liar,
    "order": _make_order,
    "pyramid": _make_pyramid,
    "mirror": _make_mirror,
    "hidden_seq": _make_hidden_seq,
}


def _weighted_kind(kinds: list[str]) -> str:
    weights = [_KIND_WEIGHTS.get(kind, 1) for kind in kinds]
    return random.choices(kinds, weights=weights, k=1)[0]


async def _build_unseen_puzzle(level: int, avoid_kind: str = "") -> Puzzle | None:
    """Генерує задачу, якої ще не було. Тип не повторює попередній день."""
    kinds = [k for k in BRAIN_TYPES if k in _BUILDERS or k == "clever"]
    preferred = [k for k in kinds if k != avoid_kind] or kinds

    for attempt in range(_MAX_ATTEMPTS):
        # Перші дві третини спроб тримаємось іншого типу, далі — будь-який.
        pool = preferred if attempt < _MAX_ATTEMPTS * 2 // 3 else kinds
        kind = _weighted_kind(pool)
        if kind == "clever":
            puzzle = await _make_clever(level)
        else:
            puzzle = _BUILDERS[kind](level)
        if not puzzle:
            continue
        if len(puzzle.options) < 4 or len(set(puzzle.options)) < len(puzzle.options):
            continue
        if len(puzzle.poll_prompt) > 300 or len(puzzle.question) > 850:
            continue
        if await is_brain_puzzle_seen(puzzle.fingerprint):
            continue
        return puzzle
    return None


def _level_bar(level: int) -> str:
    return f"{level}/{BRAIN_LEVEL_MAX}"


def _percent_for(level: int) -> int:
    return _PERCENT_BY_LEVEL.get(level, 45)


def _tasks_word(count: int) -> str:
    """Правильний відмінок: 1 задачу, 2 задачі, 5 задач."""
    if count % 100 in (11, 12, 13, 14):
        return "задач"
    last = count % 10
    if last == 1:
        return "задачу"
    if last in (2, 3, 4):
        return "задачі"
    return "задач"


async def generate_brain() -> dict | None:
    """Готує задачу дня як опитування. Тип clever може викликати Gemini."""
    level = await get_brain_level(BRAIN_START_LEVEL)
    level = max(BRAIN_LEVEL_MIN, min(BRAIN_LEVEL_MAX, level))
    percent = _percent_for(level)

    stats = await get_brain_stats()
    puzzle = await _build_unseen_puzzle(level, avoid_kind=stats.get("last_kind", ""))
    if not puzzle:
        logger.warning("[brain] Не вдалося зібрати нову задачу рівня %s", level)
        return None

    await mark_brain_puzzle_seen(puzzle.fingerprint, BRAIN_SEEN_TTL_DAYS)

    template = next(
        (t for t in VISUAL_TEMPLATES if t["name"] == "Game Mode"),
        VISUAL_TEMPLATES[0],
    )
    image_bytes = await generate_brain_image_async(
        body=puzzle.card_text,
        template=template,
        prompt=puzzle.poll_prompt if puzzle.poll_prompt != puzzle.card_text else "",
        percent=percent,
        rubric="ТренажерМозку",
    )

    answer_text = puzzle.options[puzzle.correct_index]
    label = KIND_LABELS.get(puzzle.kind, puzzle.kind)
    lamp_post = (
        f"💡 Розв'язок · ТренажерМозку\n\n"
        f"Тип: {label} · орієнтир «лише {percent}%»\n\n"
        f"Правильно: {answer_text} ✅\n\n"
        f"{puzzle.explanation}"
    )

    stats["last_kind"] = puzzle.kind
    await save_brain_stats(stats)

    return {
        "rubric": RUBRIC_KEY,
        "topic": f"{label}, {percent}%",
        "question": puzzle.question,
        "poll_question": puzzle.poll_prompt,
        "options": puzzle.options,
        "correct_index": puzzle.correct_index,
        "lamp_post": lamp_post,
        "caption": (
            f"{RUBRIC_HASHTAG}\n\n"
            f"{puzzle.question}\n\n"
            f"⏱ ~30 секунд · орієнтир: лише {percent}% розв'язують"
        ),
        "image": image_bytes,
        "persona": "Тренажер",
        "template": template["name"],
        "brain_level": level,
        "brain_kind": puzzle.kind,
    }


def _next_level(level: int, accuracy: int, votes: int) -> int:
    """Рухає складність за точністю каналу; мала вибірка нічого не змінює."""
    if votes < BRAIN_MIN_VOTES_FOR_ADAPT:
        return level
    if accuracy >= BRAIN_LEVEL_UP_ACCURACY:
        return min(BRAIN_LEVEL_MAX, level + 1)
    if accuracy <= BRAIN_LEVEL_DOWN_ACCURACY:
        return max(BRAIN_LEVEL_MIN, level - 1)
    return level


async def generate_brain_answer(poll_id: str, poll_results: dict) -> str:
    """Розв'язок наступного дня: відповідь, статистика і рух складності."""
    from data.redis_client import get_quiz_pending

    pending = await get_quiz_pending(poll_id)
    if not pending:
        return ""

    text = pending.get("lamp_post", "")
    correct_index = pending.get("correct_index", 0)
    level = int(pending.get("brain_level", BRAIN_START_LEVEL))

    votes = sum(int(v) for v in (poll_results or {}).values())
    correct_votes = int((poll_results or {}).get(str(correct_index), 0))
    accuracy = round(correct_votes / votes * 100) if votes else 0

    stats = await get_brain_stats()
    rounds = int(stats.get("rounds", 0)) + 1
    total_votes = int(stats.get("total_votes", 0)) + votes
    total_correct = int(stats.get("correct_votes", 0)) + correct_votes
    streak = int(stats.get("streak", 0))
    best_streak = int(stats.get("best_streak", 0))

    if votes:
        streak = streak + 1 if accuracy >= 50 else 0
        best_streak = max(best_streak, streak)

    new_level = _next_level(level, accuracy, votes)

    stats.update({
        "rounds": rounds,
        "total_votes": total_votes,
        "correct_votes": total_correct,
        "streak": streak,
        "best_streak": best_streak,
        "last_accuracy": accuracy,
    })
    await save_brain_stats(stats)
    if new_level != level:
        await set_brain_level(new_level)

    if votes:
        text += f"\n\n📊 Влучили: {accuracy}% ({correct_votes} з {votes})"
        if streak >= 2:
            text += f"\n🔥 Серія каналу: {streak} {_tasks_word(streak)} підряд"
        overall = round(total_correct / total_votes * 100) if total_votes else 0
        text += f"\n📈 Загальна точність за {rounds} {_tasks_word(rounds)}: {overall}%"
    else:
        text += "\n\n📊 Цього разу ніхто не проголосував."

    if new_level > level:
        text += (
            f"\n🎚 Складніше: орієнтир тепер «лише {_percent_for(new_level)}%»"
        )
    elif new_level < level:
        text += (
            f"\n🎚 Легше: орієнтир тепер «лише {_percent_for(new_level)}%»"
        )

    return text

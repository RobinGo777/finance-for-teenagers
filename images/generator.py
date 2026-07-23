import asyncio
import io
import logging
import re
import httpx
from PIL import Image, ImageDraw, ImageFont
from config import (
    VISUAL_TEMPLATES,
    PEXELS_API_KEY,
    UNSPLASH_ACCESS_KEY,
    STOCK_PHOTO_PROVIDER,
    STOCK_PHOTO_CANDIDATES,
    STOCK_PHOTO_DEDUP_DAYS,
)


# ─────────────────────────────────────────
# НАЛАШТУВАННЯ
# ─────────────────────────────────────────

IMG_WIDTH  = 1280
IMG_HEIGHT = 720
FONT_PATH  = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
logger = logging.getLogger(__name__)


# Діапазони символів, які базовий DejaVu не рендерить (emoji, піктограми).
# На картинці вони перетворюються на «тофу»-квадрати, тож прибираємо їх.
_UNRENDERABLE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # емодзі та піктограми
    "\U00002600-\U000027BF"   # різні символи + dingbats
    "\U00002B00-\U00002BFF"   # стрілки/зірки
    "\U0001F1E6-\U0001F1FF"   # регіональні індикатори
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner
    "\U000020BF"              # символ біткоїна ₿
    "]+",
    flags=re.UNICODE,
)


def _strip_unrenderable(text: str) -> str:
    """Прибирає emoji/піктограми, які шрифт картинки не вміє малювати."""
    cleaned = _UNRENDERABLE.sub("", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


TEXT_MARGIN = 48  # лівий і правий відступ тексту на картинці


def _text_width(text: str, font: ImageFont.ImageFont) -> int:
    """Ширина рядка в пікселях для конкретного шрифту."""
    if hasattr(font, "getlength"):
        try:
            return int(font.getlength(text))
        except Exception:
            pass
    bbox = font.getbbox(text or " ")
    return int(bbox[2] - bbox[0])


def _wrap_text_to_width(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """Переносить текст по піксельній ширині (не по кількості символів).

    textwrap.fill по символах обрізає українські заголовки праворуч —
    широкі літери не вміщаються в «width=28».
    """
    clean = _strip_unrenderable(text)
    if not clean:
        return ""

    words = clean.split()
    lines: list[str] = []
    current = ""

    def _flush() -> None:
        nonlocal current
        if current:
            lines.append(current)
            current = ""

    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if _text_width(candidate, font) <= max_width:
            current = candidate
            continue

        _flush()
        # Дуже довге слово без пробілів — ріжемо по символах.
        if _text_width(word, font) <= max_width:
            current = word
            continue

        chunk = ""
        for ch in word:
            trial = chunk + ch
            if chunk and _text_width(trial, font) > max_width:
                lines.append(chunk)
                chunk = ch
            else:
                chunk = trial
        current = chunk

    _flush()
    return "\n".join(lines)


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _normalize_query(text: str) -> str:
    clean = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:80]


# Англійські запити по рубриках — сток краще розуміє EN, ніж UA-заголовки.
_RUBRIC_EN_QUERIES: dict[str, list[str]] = {
    "ai_news": [
        "artificial intelligence technology workspace",
        "futuristic computer chip neon",
        "robotics laboratory research",
    ],
    "ai_hack": [
        "laptop coding productivity desk",
        "AI assistant smartphone workflow",
        "creative digital workspace night",
    ],
    "crypto": [
        "cryptocurrency trading chart screen",
        "digital finance blockchain abstract",
        "neon data network technology",
    ],
    "crime": [
        "cybersecurity phishing warning laptop",
        "hacker dark room computer screen",
        "online fraud alert smartphone",
    ],
    "cyber": [
        "cybersecurity lock digital shield",
        "password protection laptop",
        "network security operations center",
    ],
    "cost_of_life": [
        "supermarket shopping receipt prices",
        "teenager budgeting cash envelope",
        "city living expenses lifestyle",
    ],
    "side_hustle": [
        "teenager freelance laptop cafe",
        "small business side hustle desk",
        "online marketplace selling phone",
    ],
    "game_economy": [
        "esports gaming setup RGB",
        "video game economy coins interface",
        "gamer streaming desk neon",
    ],
    "subscription_trap": [
        "smartphone app subscriptions screen",
        "credit card online payment trap",
        "monthly bills digital wallet",
    ],
    "money_myth": [
        "piggy bank education coins table",
        "personal finance learning notebook",
        "money myths concept illustration desk",
    ],
    "behavioral_finance": [
        "decision making psychology desk",
        "brain finance concept abstract",
        "impulse shopping smartphone cart",
    ],
    "careers": [
        "future careers technology office",
        "student career laptop planning",
        "modern tech workplace collaboration",
    ],
    "startup_week": [
        "startup founders whiteboard office",
        "pitch deck laptop coworking",
        "innovation lab product prototype",
    ],
    "quiz": [
        "finance quiz education chalkboard",
        "teen learning money concepts",
    ],
}

# Простий UA→EN словник для ключових слів із заголовка.
_UA_EN_KEYWORDS: dict[str, str] = {
    "гроші": "money",
    "грошей": "money",
    "крипта": "cryptocurrency",
    "крипто": "cryptocurrency",
    "біткоїн": "bitcoin",
    "біткойн": "bitcoin",
    "акції": "stocks",
    "інвестиції": "investment",
    "інвестувати": "investing",
    "банк": "bank",
    "картка": "credit card",
    "підписка": "subscription",
    "шахрай": "scam fraud",
    "шахрайство": "scam fraud",
    "фішинг": "phishing",
    "хакер": "hacker cybersecurity",
    "робот": "robot",
    "штучний": "artificial intelligence",
    "інтелект": "intelligence",
    "стартап": "startup",
    "зарплата": "salary",
    "бюджет": "budget",
    "ціна": "price",
    "ціни": "prices",
    "гра": "video game",
    "ігри": "video games",
    "ютуб": "youtube creator",
    "тікток": "tiktok creator",
}

# Кліше стоку — знижуємо пріоритет.
_CLICHE_TERMS = (
    "handshake", "hand shake", "businessman smiling", "suit tie portrait",
    "piggy bank gold", "stacks of cash", "dollar bills flying",
    "bitcoin coin physical", "crypto coin 3d", "thumbs up office",
    "woman pointing laptop stock", "diverse team high five",
)


def _rubric_key(rubric: str) -> str:
    """Нормалізує rubric/#хештег до ключа генератора."""
    raw = (rubric or "").strip().lstrip("#").lower()
    aliases = {
        "техновини": "ai_news",
        "ші_лайфхак": "ai_hack",
        "криптобезхайпу": "crypto",
        "фінтрукрайм": "crime",
        "кібербезпека": "cyber",
        "скількикоштує": "cost_of_life",
        "першігроші": "side_hustle",
        "геймекономіка": "game_economy",
        "підпискапастка": "subscription_trap",
        "міфпрогроші": "money_myth",
        "грошівголові": "behavioral_finance",
        "професіїмайбутнього": "careers",
        "стартаптижня": "startup_week",
        "фінквіз": "quiz",
    }
    if raw in _RUBRIC_EN_QUERIES:
        return raw
    compact = re.sub(r"[^a-zа-яіїєґ0-9_]", "", raw, flags=re.IGNORECASE)
    return aliases.get(compact, raw)


def _title_en_keywords(title: str, body: str) -> list[str]:
    """Витягує EN-ключі з заголовка/тіла (латиниця + UA словник)."""
    text = f"{title} {body}".lower()
    words = re.findall(r"[a-zA-Zа-яА-ЯіїєґІЇЄҐ]{3,}", text)
    keys: list[str] = []
    for word in words:
        low = word.lower()
        if re.fullmatch(r"[a-z]{3,}", low):
            keys.append(low)
        elif low in _UA_EN_KEYWORDS:
            keys.append(_UA_EN_KEYWORDS[low])
    # унікальні, зберегти порядок
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:8]


def _photo_queries(title: str, body: str, rubric: str) -> list[str]:
    """Список EN-запитів: тема → рубрика → загальний fallback."""
    queries: list[str] = []
    topic_keys = _title_en_keywords(title, body)
    if topic_keys:
        queries.append(_normalize_query(" ".join(topic_keys[:5])))
        queries.append(_normalize_query(f"{' '.join(topic_keys[:3])} finance technology"))

    for q in _RUBRIC_EN_QUERIES.get(_rubric_key(rubric), []):
        queries.append(q)

    queries.extend(
        [
            "modern finance technology workspace",
            "teenager learning money laptop",
            "digital economy abstract data",
        ]
    )

    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        nq = _normalize_query(q)
        if nq and nq not in seen:
            seen.add(nq)
            unique.append(nq)
    return unique


def _candidate_text(photo: dict) -> str:
    parts = [
        str(photo.get("alt", "")),
        str(photo.get("description", "")),
        str(photo.get("url", "")),
        " ".join(photo.get("tags", [])),
    ]
    return " ".join(parts).lower()


def _score_photo(photo: dict, query: str, topic_keys: list[str]) -> float:
    """Локальний рейтинг кандидата (без Gemini)."""
    text = _candidate_text(photo)
    score = 0.0

    q_tokens = {t for t in re.findall(r"[a-z]{3,}", query.lower())}
    text_tokens = set(re.findall(r"[a-z]{3,}", text))
    overlap = len(q_tokens & text_tokens)
    score += min(overlap, 5) * 2.0

    for key in topic_keys:
        for token in key.lower().split():
            if len(token) > 2 and token in text:
                score += 1.5

    for term in _CLICHE_TERMS:
        if term in text:
            score -= 4.0

    width = int(photo.get("width") or 0)
    height = int(photo.get("height") or 0)
    if width >= 1600:
        score += 2.0
    elif width >= 1200:
        score += 1.0
    if height and width and width >= height:
        score += 0.5  # landscape

    # Unsplash likes / Pexels не завжди дає — бонус якщо є.
    likes = int(photo.get("likes") or 0)
    if likes >= 200:
        score += 1.5
    elif likes >= 50:
        score += 0.5

    return score


def _fetch_pexels_candidates(client: httpx.Client, query: str) -> list[dict]:
    if not PEXELS_API_KEY:
        return []
    try:
        resp = client.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={
                "query": query,
                "per_page": max(1, min(STOCK_PHOTO_CANDIDATES, 15)),
                "orientation": "landscape",
            },
        )
        resp.raise_for_status()
        photos = []
        for item in resp.json().get("photos", []):
            src = item.get("src", {})
            url = src.get("large2x") or src.get("large") or src.get("original")
            if not url:
                continue
            photos.append(
                {
                    "id": f"pexels:{item.get('id')}",
                    "url": url,
                    "alt": item.get("alt") or "",
                    "description": "",
                    "width": item.get("width") or 0,
                    "height": item.get("height") or 0,
                    "likes": 0,
                    "provider": "pexels",
                    "query": query,
                }
            )
        return photos
    except Exception as exc:
        logger.warning("Pexels search failed: %s", exc)
        return []


def _fetch_unsplash_candidates(client: httpx.Client, query: str) -> list[dict]:
    if not UNSPLASH_ACCESS_KEY:
        return []
    try:
        resp = client.get(
            "https://api.unsplash.com/search/photos",
            params={
                "query": query,
                "page": 1,
                "per_page": max(1, min(STOCK_PHOTO_CANDIDATES, 15)),
                "orientation": "landscape",
                "client_id": UNSPLASH_ACCESS_KEY,
            },
        )
        resp.raise_for_status()
        photos = []
        for item in resp.json().get("results", []):
            urls = item.get("urls", {})
            url = urls.get("regular") or urls.get("full")
            if not url:
                continue
            tags = [
                t.get("title", "")
                for t in (item.get("tags") or [])
                if isinstance(t, dict)
            ]
            photos.append(
                {
                    "id": f"unsplash:{item.get('id')}",
                    "url": url,
                    "alt": item.get("alt_description") or "",
                    "description": item.get("description") or "",
                    "width": item.get("width") or 0,
                    "height": item.get("height") or 0,
                    "likes": item.get("likes") or 0,
                    "tags": tags,
                    "provider": "unsplash",
                    "query": query,
                }
            )
        return photos
    except Exception as exc:
        logger.warning("Unsplash search failed: %s", exc)
        return []


def _pick_best_photo(
    candidates: list[dict],
    topic_keys: list[str],
    used_ids: set[str],
) -> dict | None:
    ranked: list[tuple[float, dict]] = []
    for photo in candidates:
        pid = str(photo.get("id") or "")
        if not pid or pid in used_ids or not photo.get("url"):
            continue
        score = _score_photo(photo, photo.get("query", ""), topic_keys)
        ranked.append((score, photo))

    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, best = ranked[0]
    logger.info(
        "[photo] обрано %s score=%.1f з %s кандидатів (query=%r)",
        best.get("id"),
        best_score,
        len(ranked),
        (best.get("query") or "")[:60],
    )
    # Занадто слабкий матч — краще шаблонна картинка.
    if best_score < 0.5 and len(ranked) < 2:
        return None
    return best


def _compose_stock_image(img: Image.Image, title: str, template: dict) -> bytes:
    """Ресайз + темний градієнт під текст + заголовок."""
    img = img.resize((IMG_WIDTH, IMG_HEIGHT), Image.Resampling.LANCZOS)
    accent_color = _hex_to_rgb(template["accent"])
    white = (255, 255, 255)

    base = img.convert("RGBA")
    overlay = Image.new("RGBA", (IMG_WIDTH, IMG_HEIGHT), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    # Легке затемнення всього кадру + сильніший градієнт знизу під заголовок.
    overlay_draw.rectangle([(0, 0), (IMG_WIDTH, IMG_HEIGHT)], fill=(0, 0, 0, 45))
    for i in range(280):
        # Лінійний градієнт: 0 → 200 alpha
        alpha = int(200 * (i / 279))
        y = IMG_HEIGHT - 280 + i
        overlay_draw.line([(0, y), (IMG_WIDTH, y)], fill=(0, 0, 0, alpha))

    composed = Image.alpha_composite(base, overlay).convert("RGB")
    draw = ImageDraw.Draw(composed)

    font_title = _load_font(FONT_PATH, 68)
    max_title_w = IMG_WIDTH - TEXT_MARGIN * 2
    wrapped = _wrap_text_to_width(title, font_title, max_title_w)
    lines = wrapped.count("\n") + 1 if wrapped else 1
    title_h = lines * 78
    title_y = max(TEXT_MARGIN, IMG_HEIGHT - 48 - title_h)

    draw.rectangle([(0, 0), (8, IMG_HEIGHT)], fill=accent_color)
    draw.text((TEXT_MARGIN, title_y), wrapped, font=font_title, fill=white)
    return _save_image(composed)


def _build_stock_photo(
    title: str,
    body: str,
    rubric: str,
    persona_name: str,
    template: dict,
    used_photo_ids: set[str] | None = None,
) -> tuple[bytes | None, str | None]:
    """Шукає стокове фото, скорить кандидатів, повертає (png, photo_id)."""
    providers: list[str]
    if STOCK_PHOTO_PROVIDER == "pexels":
        providers = ["pexels"]
    elif STOCK_PHOTO_PROVIDER == "unsplash":
        providers = ["unsplash"]
    else:
        providers = ["pexels", "unsplash"]

    queries = _photo_queries(title, body, rubric)
    if not queries:
        return None, None

    topic_keys = _title_en_keywords(title, body)
    used_ids = set(used_photo_ids or ())
    candidates: list[dict] = []
    seen_ids: set[str] = set()

    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            # До 3 запитів достатньо — далі fallback-запити рідко кращі.
            for query in queries[:3]:
                for provider in providers:
                    batch = (
                        _fetch_pexels_candidates(client, query)
                        if provider == "pexels"
                        else _fetch_unsplash_candidates(client, query)
                    )
                    for photo in batch:
                        pid = str(photo.get("id") or "")
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            candidates.append(photo)
                if len(candidates) >= STOCK_PHOTO_CANDIDATES:
                    break

            best = _pick_best_photo(candidates, topic_keys, used_ids)
            if not best:
                logger.info("[photo] немає якісних кандидатів — шаблонна картинка")
                return None, None

            image_resp = client.get(best["url"])
            image_resp.raise_for_status()
            img = Image.open(io.BytesIO(image_resp.content)).convert("RGB")
            return _compose_stock_image(img, title, template), str(best["id"])
    except Exception as exc:
        logger.warning("Stock photo pipeline failed: %s", exc)
        return None, None


# ─────────────────────────────────────────
# БАЗОВА ГЕНЕРАЦІЯ КАРТИНКИ
# ─────────────────────────────────────────

def generate_post_image(
    title: str,
    body: str,
    rubric: str,
    persona_name: str,
    template: dict,
    used_photo_ids: set[str] | None = None,
) -> bytes:
    """Генерує картинку для поста (PNG bytes)."""
    image, _photo_id = generate_post_image_result(
        title=title,
        body=body,
        rubric=rubric,
        persona_name=persona_name,
        template=template,
        used_photo_ids=used_photo_ids,
    )
    return image


def generate_post_image_result(
    title: str,
    body: str,
    rubric: str,
    persona_name: str,
    template: dict,
    used_photo_ids: set[str] | None = None,
) -> tuple[bytes, str | None]:
    """
    Генерує картинку для поста через Pillow.
    Повертає (PNG bytes, stock photo_id або None).
    """
    stock_photo, photo_id = _build_stock_photo(
        title=title,
        body=body,
        rubric=rubric,
        persona_name=persona_name,
        template=template,
        used_photo_ids=used_photo_ids,
    )
    if stock_photo:
        return stock_photo, photo_id

    bg_color     = _hex_to_rgb(template["bg"])
    accent_color = _hex_to_rgb(template["accent"])
    white        = (255, 255, 255)
    muted        = (180, 180, 180)

    img  = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), bg_color)
    draw = ImageDraw.Draw(img)

    # ── Акцентна смуга зліва ──
    draw.rectangle([(0, 0), (8, IMG_HEIGHT)], fill=accent_color)

    # ── Великий emoji шаблону (фон) ──
    font_emoji_bg = _load_font(FONT_PATH, 320)
    draw.text(
        (IMG_WIDTH - 380, IMG_HEIGHT // 2 - 180),
        template["emoji"],
        font=font_emoji_bg,
        fill=(*accent_color, 18),  # дуже прозорий
    )

    # ── Заголовок (великий) ──
    font_title = _load_font(FONT_PATH, 74)
    max_text_w = IMG_WIDTH - TEXT_MARGIN * 2
    wrapped_title = _wrap_text_to_width(title, font_title, max_text_w)
    draw.text((TEXT_MARGIN, 64), wrapped_title, font=font_title, fill=white)

    # ── Підзаголовок / тіло ──
    title_lines = wrapped_title.count("\n") + 1 if wrapped_title else 1
    body_y = 64 + title_lines * 86 + 24

    font_body = _load_font(FONT_PATH_REGULAR, 36)
    wrapped_body = _wrap_text_to_width(body, font_body, max_text_w)
    draw.text((TEXT_MARGIN, body_y), wrapped_body, font=font_body, fill=muted)

    # ── Нижня панель ──
    draw.rectangle(
        [(0, IMG_HEIGHT - 80), (IMG_WIDTH, IMG_HEIGHT)],
        fill=tuple(max(0, c - 15) for c in bg_color),
    )

    # ── Назва каналу (нижній правий) ──
    font_persona = _load_font(FONT_PATH, 26)
    channel_text = "ФінПро для дітей"
    bbox = draw.textbbox((0, 0), channel_text, font=font_persona)
    text_width = bbox[2] - bbox[0]
    draw.text(
        (IMG_WIDTH - text_width - 48, IMG_HEIGHT - 54),
        channel_text,
        font=font_persona,
        fill=muted,
    )

    # ── Акцентна крапка біля назви каналу ──
    draw.ellipse(
        [
            (IMG_WIDTH - text_width - 64, IMG_HEIGHT - 46),
            (IMG_WIDTH - text_width - 52, IMG_HEIGHT - 34),
        ],
        fill=accent_color,
    )

    # ── Зберігаємо в bytes ──
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer.read(), None


# ─────────────────────────────────────────
# ГРАФІК ДЛЯ #БіржаДляДітей
# ─────────────────────────────────────────

def generate_chart_image(
    labels: list,
    values: list,
    title: str,
    template: dict,
) -> bytes:
    """
    Генерує простий bar chart через Pillow (без matplotlib).
    labels — назви стовпців, values — числа.
    """
    bg_color     = _hex_to_rgb(template["bg"])
    accent_color = _hex_to_rgb(template["accent"])
    white        = (255, 255, 255)
    muted        = (160, 160, 160)

    img  = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), bg_color)
    draw = ImageDraw.Draw(img)

    font_title = _load_font(FONT_PATH, 56)
    font_label = _load_font(FONT_PATH_REGULAR, 28)
    font_value = _load_font(FONT_PATH, 32)

    # ── Заголовок ──
    max_text_w = IMG_WIDTH - TEXT_MARGIN * 2
    wrapped_title = _wrap_text_to_width(title, font_title, max_text_w)
    draw.text((TEXT_MARGIN, 40), wrapped_title, font=font_title, fill=white)
    title_lines = wrapped_title.count("\n") + 1 if wrapped_title else 1
    line_y = 40 + title_lines * 64 + 8
    draw.line([(TEXT_MARGIN, line_y), (IMG_WIDTH - TEXT_MARGIN, line_y)], fill=accent_color, width=2)

    # ── Параметри графіку ──
    chart_x      = 80
    chart_y      = 560
    chart_width  = IMG_WIDTH - 160
    chart_height = 380
    bar_gap      = 30
    n            = len(labels)
    bar_width    = (chart_width - bar_gap * (n - 1)) // n

    if not values:
        return _save_image(img)

    max_val = max(abs(v) for v in values) or 1

    for i, (label, value) in enumerate(zip(labels, values)):
        x = chart_x + i * (bar_width + bar_gap)

        # Висота стовпця пропорційна значенню
        bar_h = int((abs(value) / max_val) * chart_height * 0.85)
        color = accent_color if value >= 0 else (255, 80, 80)

        # Стовпець
        draw.rectangle(
            [(x, chart_y - bar_h), (x + bar_width, chart_y)],
            fill=color,
        )

        # Заокруглений верх (імітація)
        draw.ellipse(
            [(x, chart_y - bar_h - 8), (x + bar_width, chart_y - bar_h + 8)],
            fill=color,
        )

        # Значення над стовпцем
        val_text = f"{value:+.1f}%" if isinstance(value, float) else str(value)
        bbox = draw.textbbox((0, 0), val_text, font=font_value)
        val_w = bbox[2] - bbox[0]
        draw.text(
            (x + bar_width // 2 - val_w // 2, chart_y - bar_h - 44),
            val_text,
            font=font_value,
            fill=white,
        )

        # Підпис під стовпцем
        bbox = draw.textbbox((0, 0), label, font=font_label)
        lbl_w = bbox[2] - bbox[0]
        draw.text(
            (x + bar_width // 2 - lbl_w // 2, chart_y + 12),
            label,
            font=font_label,
            fill=muted,
        )

    # ── Базова лінія ──
    draw.line([(chart_x, chart_y), (chart_x + chart_width, chart_y)], fill=muted, width=2)

    # ── Лого ──
    font_logo = _load_font(FONT_PATH, 24)
    draw.text((IMG_WIDTH - 280, IMG_HEIGHT - 40), "ФінПро для дітей", font=font_logo, fill=muted)

    return _save_image(img)


# ─────────────────────────────────────────
# КАРТИНКА ДЛЯ КВІЗУ
# ─────────────────────────────────────────

def generate_quiz_image(question: str, template: dict) -> bytes:
    """Картинка з питанням для #ФінКвіз."""
    bg_color     = _hex_to_rgb(template["bg"])
    accent_color = _hex_to_rgb(template["accent"])
    white        = (255, 255, 255)

    img  = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), bg_color)
    draw = ImageDraw.Draw(img)

    # Акцентні кути
    size = 60
    draw.rectangle([(0, 0), (size, 8)], fill=accent_color)
    draw.rectangle([(0, 0), (8, size)], fill=accent_color)
    draw.rectangle([(IMG_WIDTH - size, 0), (IMG_WIDTH, 8)], fill=accent_color)
    draw.rectangle([(IMG_WIDTH - 8, 0), (IMG_WIDTH, size)], fill=accent_color)

    # Велике питання по центру
    font_q = _load_font(FONT_PATH, 62)
    max_q_w = IMG_WIDTH - TEXT_MARGIN * 2
    wrapped = _wrap_text_to_width(question, font_q, max_q_w)
    lines   = wrapped.split("\n") if wrapped else []
    total_h = len(lines) * 64
    start_y = (IMG_HEIGHT - total_h) // 2 - 20

    for i, line in enumerate(lines):
        bbox  = draw.textbbox((0, 0), line, font=font_q)
        line_w = bbox[2] - bbox[0]
        draw.text(
            ((IMG_WIDTH - line_w) // 2, start_y + i * 68),
            line,
            font=font_q,
            fill=white,
        )

    # Підказка знизу
    font_hint = _load_font(FONT_PATH_REGULAR, 28)
    hint = "Голосуй нижче"
    bbox  = draw.textbbox((0, 0), hint, font=font_hint)
    hint_w = bbox[2] - bbox[0]
    draw.text(
        ((IMG_WIDTH - hint_w) // 2, IMG_HEIGHT - 80),
        hint,
        font=font_hint,
        fill=accent_color,
    )

    return _save_image(img)


# ─────────────────────────────────────────
# ДОПОМІЖНА ФУНКЦІЯ
# ─────────────────────────────────────────

def _save_image(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer.read()


# ─────────────────────────────────────────
# ASYNC-ОБГОРТКИ
# ─────────────────────────────────────────
# Малювання Pillow + завантаження стокових фото — блокуючі операції.
# Виносимо їх у потік, щоб не зупиняти event loop (polling і monitor).

async def generate_post_image_async(**kwargs) -> bytes:
    """Асинхронна генерація з дедупом стокових фото в Redis."""
    from data.redis_client import is_photo_used, mark_photo_used

    used: set[str] = set(kwargs.pop("used_photo_ids", None) or ())
    image = b""
    photo_id: str | None = None

    for _ in range(4):
        image, photo_id = await asyncio.to_thread(
            generate_post_image_result,
            used_photo_ids=used,
            **kwargs,
        )
        if not photo_id:
            return image
        if await is_photo_used(photo_id):
            used.add(photo_id)
            continue
        await mark_photo_used(photo_id, STOCK_PHOTO_DEDUP_DAYS)
        return image

    return image


async def generate_quiz_image_async(**kwargs) -> bytes:
    return await asyncio.to_thread(generate_quiz_image, **kwargs)


async def generate_chart_image_async(**kwargs) -> bytes:
    return await asyncio.to_thread(generate_chart_image, **kwargs)

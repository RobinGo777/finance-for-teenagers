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


def _photo_queries(title: str, body: str, rubric: str) -> list[str]:
    queries = []
    base = f"{title} {rubric}".strip()
    if base:
        queries.append(_normalize_query(base))
    if body:
        queries.append(_normalize_query(body))
    queries.extend(
        [
            "finance business technology",
            "money investment economics",
            "startup office data chart",
        ]
    )
    return [q for q in queries if q]


def _fetch_pexels_url(client: httpx.Client, query: str) -> str | None:
    if not PEXELS_API_KEY:
        return None
    try:
        resp = client.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 1, "orientation": "landscape"},
        )
        resp.raise_for_status()
        data = resp.json()
        photos = data.get("photos", [])
        if not photos:
            return None
        src = photos[0].get("src", {})
        return src.get("large2x") or src.get("large") or src.get("original")
    except Exception as exc:
        logger.warning("Pexels search failed: %s", exc)
        return None


def _fetch_unsplash_url(client: httpx.Client, query: str) -> str | None:
    if not UNSPLASH_ACCESS_KEY:
        return None
    try:
        resp = client.get(
            "https://api.unsplash.com/search/photos",
            params={
                "query": query,
                "page": 1,
                "per_page": 1,
                "orientation": "landscape",
                "client_id": UNSPLASH_ACCESS_KEY,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        urls = results[0].get("urls", {})
        return urls.get("regular") or urls.get("full")
    except Exception as exc:
        logger.warning("Unsplash search failed: %s", exc)
        return None


def _build_stock_photo(
    title: str,
    body: str,
    rubric: str,
    persona_name: str,
    template: dict,
) -> bytes | None:
    providers: list[str]
    if STOCK_PHOTO_PROVIDER == "pexels":
        providers = ["pexels"]
    elif STOCK_PHOTO_PROVIDER == "unsplash":
        providers = ["unsplash"]
    else:
        providers = ["pexels", "unsplash"]

    queries = _photo_queries(title, body, rubric)
    if not queries:
        return None

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            photo_url = None
            for query in queries:
                for provider in providers:
                    if provider == "pexels":
                        photo_url = _fetch_pexels_url(client, query)
                    else:
                        photo_url = _fetch_unsplash_url(client, query)
                    if photo_url:
                        break
                if photo_url:
                    break

            if not photo_url:
                return None

            image_resp = client.get(photo_url)
            image_resp.raise_for_status()

            img = Image.open(io.BytesIO(image_resp.content)).convert("RGB")
            img = img.resize((IMG_WIDTH, IMG_HEIGHT), Image.Resampling.LANCZOS)

            draw = ImageDraw.Draw(img)
            accent_color = _hex_to_rgb(template["accent"])
            white = (255, 255, 255)

            # Overlay for readability on bright photos.
            overlay_top = Image.new("RGBA", (IMG_WIDTH, IMG_HEIGHT), (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay_top)
            overlay_draw.rectangle([(0, IMG_HEIGHT - 220), (IMG_WIDTH, IMG_HEIGHT)], fill=(0, 0, 0, 130))
            img = Image.alpha_composite(img.convert("RGBA"), overlay_top).convert("RGB")
            draw = ImageDraw.Draw(img)

            font_title = _load_font(FONT_PATH, 58)
            max_title_w = IMG_WIDTH - TEXT_MARGIN * 2

            draw.rectangle([(0, 0), (8, IMG_HEIGHT)], fill=accent_color)
            draw.text(
                (TEXT_MARGIN, IMG_HEIGHT - 180),
                _wrap_text_to_width(title, font_title, max_title_w),
                font=font_title,
                fill=white,
            )

            return _save_image(img)
    except Exception as exc:
        logger.warning("Stock photo pipeline failed: %s", exc)
        return None


# ─────────────────────────────────────────
# БАЗОВА ГЕНЕРАЦІЯ КАРТИНКИ
# ─────────────────────────────────────────

def generate_post_image(
    title: str,
    body: str,
    rubric: str,
    persona_name: str,
    template: dict,
) -> bytes:
    """
    Генерує картинку для поста через Pillow.
    Повертає PNG у вигляді bytes.
    """
    stock_photo = _build_stock_photo(
        title=title,
        body=body,
        rubric=rubric,
        persona_name=persona_name,
        template=template,
    )
    if stock_photo:
        return stock_photo

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
    font_title = _load_font(FONT_PATH, 64)
    max_text_w = IMG_WIDTH - TEXT_MARGIN * 2
    wrapped_title = _wrap_text_to_width(title, font_title, max_text_w)
    draw.text((TEXT_MARGIN, 64), wrapped_title, font=font_title, fill=white)

    # ── Підзаголовок / тіло ──
    title_lines = wrapped_title.count("\n") + 1 if wrapped_title else 1
    body_y = 64 + title_lines * 76 + 24

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
    return buffer.read()


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

    font_title = _load_font(FONT_PATH, 48)
    font_label = _load_font(FONT_PATH_REGULAR, 28)
    font_value = _load_font(FONT_PATH, 32)

    # ── Заголовок ──
    max_text_w = IMG_WIDTH - TEXT_MARGIN * 2
    wrapped_title = _wrap_text_to_width(title, font_title, max_text_w)
    draw.text((TEXT_MARGIN, 40), wrapped_title, font=font_title, fill=white)
    title_lines = wrapped_title.count("\n") + 1 if wrapped_title else 1
    line_y = 40 + title_lines * 56 + 8
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
    font_q = _load_font(FONT_PATH, 54)
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
    return await asyncio.to_thread(generate_post_image, **kwargs)


async def generate_quiz_image_async(**kwargs) -> bytes:
    return await asyncio.to_thread(generate_quiz_image, **kwargs)


async def generate_chart_image_async(**kwargs) -> bytes:
    return await asyncio.to_thread(generate_chart_image, **kwargs)

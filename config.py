import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID  = os.getenv("TELEGRAM_CHANNEL_ID", "")   # напр. @finpro_ua
MODERATOR_CHAT_ID    = int(os.getenv("MODERATOR_CHAT_ID", "0"))  # твій Telegram ID

# ─────────────────────────────────────────
# GEMINI API
# ─────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Flash-моделі для текстових постів — пріоритет (перевірено на API-ключі).
_GEMINI_FLASH_ORDER = (
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-flash-latest",
    "gemini-1.5-flash",
)

# Спеціалізовані моделі — не для генерації постів (image/tts/embedding/live тощо).
_GEMINI_MODEL_BLOCKLIST = (
    "-image",
    "-tts",
    "embedding",
    "-live",
    "native-audio",
    "robotics",
    "computer-use",
    "omni",
    "translate",
    "customtools",
    "-lite",
    "lite-latest",
)

# Скільки моделей максимум пробуємо за один запит (щоб не палити квоту каскадом).
GEMINI_MAX_MODELS_PER_REQUEST = int(os.getenv("GEMINI_MAX_MODELS_PER_REQUEST", "2"))


def _is_usable_text_model(name: str) -> bool:
    """True лише для flash-моделей генерації тексту."""
    n = name.lower()
    if "-pro" in n or n.endswith("pro-latest"):
        return False
    return not any(marker in n for marker in _GEMINI_MODEL_BLOCKLIST)


def _normalize_gemini_models(raw: list[str]) -> list[str]:
    """Flash-моделі для постів; Pro/image/tts/live відсіюються."""
    usable = [m for m in raw if _is_usable_text_model(m)]
    seen: set[str] = set()
    ordered: list[str] = []

    for model in _GEMINI_FLASH_ORDER:
        if model in usable and model not in seen:
            ordered.append(model)
            seen.add(model)

    for model in usable:
        if model not in seen:
            ordered.append(model)
            seen.add(model)

    if "gemini-2.0-flash" not in seen:
        ordered.append("gemini-2.0-flash")
        seen.add("gemini-2.0-flash")

    cap = max(1, GEMINI_MAX_MODELS_PER_REQUEST)
    return ordered[:cap] or ["gemini-2.5-flash", "gemini-2.0-flash"]


_GEMINI_RAW = [
    model.strip().removeprefix("models/")
    for model in os.getenv(
        "GEMINI_MODELS",
        "gemini-2.5-flash,gemini-2.0-flash",
    ).split(",")
    if model.strip()
]
GEMINI_MODELS = _normalize_gemini_models(_GEMINI_RAW)

# Free tier: ~5–15 RPM. Тримаємо паузу між викликами і чекаємо Retry-After на 429.
GEMINI_MIN_INTERVAL_SEC = float(os.getenv("GEMINI_MIN_INTERVAL_SEC", "6"))
GEMINI_MAX_RETRY_WAIT_SEC = float(os.getenv("GEMINI_MAX_RETRY_WAIT_SEC", "180"))
# Скільки разів scheduler відкладає рубрику при 429 (через RPM, не денний ліміт).
GEMINI_SCHEDULE_RETRIES = int(os.getenv("GEMINI_SCHEDULE_RETRIES", "2"))
GEMINI_SCHEDULE_RETRY_DELAY_SEC = int(os.getenv("GEMINI_SCHEDULE_RETRY_DELAY_SEC", "180"))
# Глобальна пауза Gemini після вичерпання квоти (сек) — Redis, переживає рестарт.
GEMINI_GLOBAL_COOLDOWN_SEC = int(os.getenv("GEMINI_GLOBAL_COOLDOWN_SEC", "14400"))

# ─────────────────────────────────────────
# UPSTASH REDIS
# ─────────────────────────────────────────
UPSTASH_REDIS_URL   = os.getenv("UPSTASH_REDIS_URL", "")
UPSTASH_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_TOKEN", "")

# ─────────────────────────────────────────
# ЗОВНІШНІ API
# ─────────────────────────────────────────
YOUTUBE_API_KEY      = os.getenv("YOUTUBE_API_KEY", "")
NEWSAPI_KEY          = os.getenv("NEWSAPI_KEY", "")
PEXELS_API_KEY       = os.getenv("PEXELS_API_KEY", "")
UNSPLASH_ACCESS_KEY  = os.getenv("UNSPLASH_ACCESS_KEY", "")
STOCK_PHOTO_PROVIDER = os.getenv("STOCK_PHOTO_PROVIDER", "auto").lower()

# Безкоштовні — ключі не потрібні
COINGECKO_URL        = "https://api.coingecko.com/api/v3"
NBU_URL              = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
FRED_URL             = "https://api.stlouisfed.org/fred/series/observations"
WORLD_BANK_URL       = "https://api.worldbank.org/v2"
GITHUB_API_URL       = "https://api.github.com/search/repositories"

# RSS джерела
RSS_FEEDS = [
    "https://ain.ua/feed",
    "https://mind.ua/rss",
    "https://minfin.com.ua/rss/news.xml",
    "https://techcrunch.com/feed/",
    "https://openai.com/blog/rss",
]

# ─────────────────────────────────────────
# 4 ПЕРСОНИ-АВТОРИ
# ─────────────────────────────────────────
PERSONAS = [
    {
        "name": "Тато",
        "role": "Аналітик і технар",
        "style": "Говориш фактами і цифрами. Коротко, чітко, без зайвих слів. Любиш конкретику.",
        "emoji_style": "📊📈🔢",
        "cta_style": "аналітичний — 'Перевір сам', 'Ось дані'",
    },
    {
        "name": "Мама",
        "role": "Вчителька і наставниця",
        "style": "Пояснюєш складне просто, з прикладами з повсякденного життя. Тепло і турботливо.",
        "emoji_style": "💡📚🌟",
        "cta_style": "мотиваційний — 'Спробуй сьогодні', 'Ти впораєшся'",
    },
    {
        "name": "Дядя",
        "role": "Підприємець і мотиватор",
        "style": "Енергійно, з драйвом. Говориш про можливості і дії. Надихаєш на старт.",
        "emoji_style": "🚀🔥💪",
        "cta_style": "заклик до дії — 'Зроби це зараз', 'Не чекай'",
    },
    {
        "name": "Баба",
        "role": "Скептик і критик",
        "style": "Мудро, обережно. Завжди попереджаєш про ризики. 'Не все золото що блищить'.",
        "emoji_style": "⚠️🤔👀",
        "cta_style": "застережливий — 'Будь обережний', 'Перевір двічі'",
    },
]

# ─────────────────────────────────────────
# 7 СТИЛІВ ОФОРМЛЕННЯ КАРТИНОК
# ─────────────────────────────────────────
VISUAL_TEMPLATES = [
    {"name": "Dark Space",     "bg": "#0a0a1a", "accent": "#00d4ff", "emoji": "🌌"},
    {"name": "Organic Growth", "bg": "#0a1a0f", "accent": "#00ff94", "emoji": "🌿"},
    {"name": "Warm Alert",     "bg": "#1a0a00", "accent": "#ff4d1a", "emoji": "🔥"},
    {"name": "Cool Data",      "bg": "#0a0f1a", "accent": "#4d8fff", "emoji": "💎"},
    {"name": "Golden Flash",   "bg": "#0a0a00", "accent": "#ffd60a", "emoji": "⚡"},
    {"name": "Game Mode",      "bg": "#1a0a2a", "accent": "#ff6bba", "emoji": "🎮"},
    {"name": "Newspaper",      "bg": "#0a0a0a", "accent": "#ffffff", "emoji": "📰"},
]

# ─────────────────────────────────────────
# РОЗКЛАД ПУБЛІКАЦІЙ
# ─────────────────────────────────────────
# Примітка: рубрики "video" тут НЕМАЄ навмисно — #ВідеоТижня публікується
# подієво реалтайм-монітором, лише коли реально трапляється варте відео,
# а не примусово за графіком.
#
# Правила слотів:
# - будні: після 16:30 (школа);
# - вихідні: після обіду (не раніше ~13:00);
# - між двома постами дня — кілька годин проміжку;
# - хвилини «нерівні», щоб не виглядало як бот на xx:00.
# Фактичний час = слот + рандомний зсув (див. нижче).
SCHEDULE = {
    "monday": [
        {"time": "16:47", "rubric": "ai_news"},
        {"time": "19:23", "rubric": "game_economy"},
    ],
    "tuesday": [
        {"time": "17:14", "rubric": "cost_of_life"},
        {"time": "19:48", "rubric": "ai_hack"},
    ],
    "wednesday": [
        {"time": "16:53", "rubric": "side_hustle"},
        {"time": "19:31", "rubric": "crime"},
    ],
    "thursday": [
        {"time": "17:08", "rubric": "crypto"},
        {"time": "19:42", "rubric": "behavioral_finance"},
    ],
    "friday": [
        {"time": "16:41", "rubric": "subscription_trap"},
        {"time": "18:57", "rubric": "quiz"},
    ],
    "saturday": [
        {"time": "13:27", "rubric": "careers"},
        {"time": "16:44", "rubric": "startup_week"},
    ],
    "sunday": [
        {"time": "14:18", "rubric": "money_myth"},
        {"time": "17:53", "rubric": "quiz"},
    ],
}

# Кібербезпека — 1-й і 3-й вівторок місяця (окремий крон у scheduler).
CYBER_SCHEDULE_TIME = "20:17"

# Відповідь на квіз — щодня перевірка «дозрілих» pending.
QUIZ_ANSWER_CRON_TIME = "20:41"

# Рандомний зсув від базового слота (хвилини) — «наче людина написала».
SCHEDULE_RANDOM_OFFSET_MIN = -11
SCHEDULE_RANDOM_OFFSET_MAX = 17

# ─────────────────────────────────────────
# РЕАЛТАЙМ МОНІТОР
# ─────────────────────────────────────────
# Фіксовані слоти за Києвом (кожні 3 год у денному вікні).
MONITOR_HOURS = [11, 14, 17, 20]
MONITOR_MAX_PER_DAY = 4          # макс реалтайм постів на день
YOUTUBE_MIN_VIEWS = 20_000       # мінімум переглядів для відео (пом'якшено)

# ─────────────────────────────────────────
# ФІЛЬТРИ ВІДЕО-РУБРИКИ
# ─────────────────────────────────────────
# Пошук бере кандидатів із широкого вікна одним проходом, а потім оцінює
# корисність і видовищність. Модель може відмовитися від слабких кандидатів.
VIDEO_PUBLISHED_AFTER_HOURS = 96      # шукаємо відео за останні 4 дні
VIDEO_MIN_VIEWS_FLOOR       = 5_000   # абсолютний мінімум для fallback
VIDEO_MATCH_BONUS_ONLY      = True    # збіг зі свіжими новинами = бонус, не фільтр
# Скільки YouTube Search запитів за один цикл монітора (кожен ≈ 100 units квоти).
# Раніше брали всі 7 тем → легко ловили 429. Ротація покриває всі теми за кілька днів.
VIDEO_SEARCH_QUERIES_PER_RUN = 3
# Кеш результатів пошуку в Redis — щоб повторні цикли не палили квоту.
VIDEO_SEARCH_CACHE_TTL_SEC = 10800    # 3 години (як інтервал монітора)

# Економія Gemini: не питати модель про слабкі/вже відхилені набори.
VIDEO_MIN_RANK_SCORE = 1              # мін. локальний score топ-кандидата перед Gemini
VIDEO_MIN_CANDIDATES = 2              # не питати Gemini, якщо лише 1 слабкий кандидат
VIDEO_GEMINI_COOLDOWN_HOURS = 8       # макс. 1 спроба Gemini для відео за цей період
VIDEO_REJECT_TTL_SEC = 18 * 3600      # негативний кеш відхилених video_id (18 год)

# Приймаємо лише відео цими мовами аудіо (порожня = невідомо, теж пропускаємо).
# Мета — не постити ролики, які підліток не зрозуміє (напр. гінді на NDTV India).
VIDEO_ALLOWED_LANGUAGES = ["en", "uk"]

# Загальні новинні канали: часто це «балакучі» сюжети диктора без реального
# показу події (напр. NDTV з фото прем'єра замість запуску ракети).
# Не забороняємо повністю, але знижуємо пріоритет на користь каналів із кадрами.
NEWS_CHANNEL_HINTS = [
    "ndtv", "cnn", "bbc news", "fox news", "msnbc", "wion", "aljazeera",
    "al jazeera", "times of india", "india today", "republic", "abp",
    "zee news", "reuters", "ap ", "sky news", "euronews", "dw news",
]

TRUSTED_VIDEO_CHANNEL_HINTS = [
    "techcrunch",
    "wired",
    "the verge",
    "cnet",
    "new scientist",
    "nasa",
    "mit",
    "stanford",
    "openai",
    "google",
    "microsoft",
    "nvidia",
    "boston dynamics",
    "veritasium",
    "kurzgesagt",
    "mark rober",
    "stuff made here",
    "mkbhd",
    "marques brownlee",
    "linus tech tips",
    "two minute papers",
    "coldfusion",
    "fireship",
    "the coding train",
    "y combinator",
    "ted-ed",
    "smartereveryday",
]

# Ознаки відео, де щось реально показують або доступно пояснюють.
VIDEO_ENGAGING_TERMS = [
    "demo", "demonstration", "tested", "testing", "experiment", "prototype",
    "review", "hands-on", "explained", "how it works", "inside", "built",
    "building", "invention", "footage", "launch", "first look", "showcase",
]

# Ознаки телесюжету/політичного переказу замість корисного відео.
VIDEO_LOW_VALUE_TERMS = [
    "breaking news", "speech", "prime minister", "president", "minister",
    "press conference", "interview", "debate", "statement", "addresses",
    "reacts to", "exclusive news", "live news",
]

VIDEO_CLICKBAIT_TERMS = [
    "giveaway",
    "free money",
    "guaranteed profit",
    "casino",
    "betting",
    "shocking",
    "you won't believe",
    "100x",
    "pump",
    "get rich quick",
]

VIDEO_MATCH_STOPWORDS = [
    "the", "and", "for", "with", "this", "that", "from", "into", "about",
    "нове", "новий", "нова", "про", "для", "цей", "ця", "що", "як", "та",
    "video", "shorts", "short", "news", "today",
]

# ─────────────────────────────────────────
# ЗАГАЛЬНІ НАЛАШТУВАННЯ
# ─────────────────────────────────────────
TIMEZONE = "Europe/Kyiv"
MAX_USED_TOPICS_IN_PROMPT = 30      # скільки використаних тем передаємо в промпт

# Через скільки годин після квізу публікувати відповідь зі статистикою.
# Крон перевіряє щодня (див. QUIZ_ANSWER_CRON_TIME) і публікує лише «дозрілі» квізи.
QUIZ_ANSWER_DELAY_HOURS = 20

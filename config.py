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
GEMINI_MODELS = [
    model.strip().removeprefix("models/")
    for model in os.getenv(
        "GEMINI_MODELS",
        # Обидві flash-моделі мають по 1500 запитів/день, але це ОКРЕМІ пули,
        # тож fallback подвоює денну ємність. Pro не беремо: на безкоштовному
        # тарифі лише 50/день — це лише додає 429. Новіші моделі — через env.
        "gemini-2.5-flash,gemini-2.0-flash",
    ).split(",")
    if model.strip()
] or ["gemini-2.5-flash"]

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
SCHEDULE = {
    "monday":    {"time": "18:00", "rubrics": ["ai_news", "game_economy"]},
    "tuesday":   {"time": "18:30", "rubrics": ["cost_of_life", "ai_hack"]},
    "wednesday": {"time": "19:00", "rubrics": ["side_hustle", "crime"]},
    "thursday":  {"time": "18:00", "rubrics": ["crypto", "behavioral_finance"]},
    "friday":    {"time": "17:45", "rubrics": ["subscription_trap", "quiz"]},
    "saturday":  {"time": "12:00", "rubrics": ["careers", "startup_week"]},
    "sunday":    {"time": "19:00", "rubrics": ["money_myth", "quiz"]},
}

# Рандомний зсув часу публікації (хвилини)
SCHEDULE_RANDOM_OFFSET_MIN = -15
SCHEDULE_RANDOM_OFFSET_MAX = 40

# ─────────────────────────────────────────
# РЕАЛТАЙМ МОНІТОР
# ─────────────────────────────────────────
MONITOR_INTERVAL_HOURS = 2          # перевірка кожні 2 год
MONITOR_QUIET_START    = 0          # тиша з 00:00
MONITOR_QUIET_END      = 7          # до 07:00
MONITOR_MAX_PER_DAY    = 4          # макс реалтайм постів на день
YOUTUBE_MIN_VIEWS      = 20_000     # мінімум переглядів для відео (пом'якшено)

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
VIDEO_SEARCH_CACHE_TTL_SEC = 7200     # 2 години

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
# Крон перевіряє щодня о 19:10 і публікує лише «дозрілі» квізи.
QUIZ_ANSWER_DELAY_HOURS = 20

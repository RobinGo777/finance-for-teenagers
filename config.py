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
        # Робочу/дешевшу flash-модель ставимо ПЕРШОЮ: у неї вищі ліміти запитів,
        # і на кожну рубрику йде 1 виклик, а не перебір неіснуючих моделей
        # (це швидко вигорало квоту й давало 429). Новіші можна додати через env.
        "gemini-2.5-flash,gemini-2.5-pro",
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
SCHEDULE = {
    "monday":    {"time": "18:00", "rubrics": ["ai_news", "game_economy"]},
    "tuesday":   {"time": "18:30", "rubrics": ["cost_of_life", "video", "ai_hack"]},
    "wednesday": {"time": "19:00", "rubrics": ["side_hustle", "crime"]},
    "thursday":  {"time": "18:00", "rubrics": ["crypto", "video", "ai_hack"]},
    "friday":    {"time": "17:45", "rubrics": ["subscription_trap", "quiz"]},
    "saturday":  {"time": "12:00", "rubrics": ["money_hack", "careers"]},
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
# Фільтри навмисно м'які: краще показати трохи менш «ідеальне» відео,
# ніж не показати нічого. Прогресивне послаблення в generators/video.py.
VIDEO_PUBLISHED_AFTER_HOURS = 96      # шукаємо відео за останні 4 дні
VIDEO_MIN_VIEWS_FLOOR       = 5_000   # абсолютний мінімум для fallback
VIDEO_MATCH_BONUS_ONLY      = True    # збіг зі свіжими новинами = бонус, не фільтр

TRUSTED_VIDEO_CHANNEL_HINTS = [
    "techcrunch",
    "wired",
    "the verge",
    "cnet",
    "mashable",
    "new scientist",
    "nasa",
    "mit",
    "stanford",
    "openai",
    "google",
    "microsoft",
    "nvidia",
    "boston dynamics",
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

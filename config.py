import os
from dataclasses import dataclass

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
GEMINI_MODEL   = "gemini-1.5-flash"

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
ALPHA_VANTAGE_KEY    = os.getenv("ALPHA_VANTAGE_KEY", "")
PEXELS_API_KEY       = os.getenv("PEXELS_API_KEY", "")
UNSPLASH_ACCESS_KEY  = os.getenv("UNSPLASH_ACCESS_KEY", "")
STOCK_PHOTO_PROVIDER = os.getenv("STOCK_PHOTO_PROVIDER", "auto").lower()

# Безкоштовні — ключі не потрібні
COINGECKO_URL        = "https://api.coingecko.com/api/v3"
NBU_URL              = "https://bank.gov.ua/NBUStatService/v1/statdataservice/exchange"
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
    "monday":    {"time": "18:00", "rubrics": ["ai_news", "trends", "digit_of_week"]},
    "tuesday":   {"time": "18:30", "rubrics": ["stocks", "video", "ai_hack"]},
    "wednesday": {"time": "19:00", "rubrics": ["business", "crime"]},
    "thursday":  {"time": "18:00", "rubrics": ["crypto", "video", "ai_hack"]},
    "friday":    {"time": "17:45", "rubrics": ["fin_literacy", "quiz"]},
    "saturday":  {"time": "12:00", "rubrics": ["money_hack", "careers"]},
    "sunday":    {"time": "19:00", "rubrics": ["digest", "quiz"]},
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
YOUTUBE_MIN_VIEWS      = 50_000     # мінімум переглядів для відео

# ─────────────────────────────────────────
# ЗАГАЛЬНІ НАЛАШТУВАННЯ
# ─────────────────────────────────────────
TIMEZONE = "Europe/Kyiv"
MAX_USED_TOPICS_IN_PROMPT = 30      # скільки використаних тем передаємо в промпт

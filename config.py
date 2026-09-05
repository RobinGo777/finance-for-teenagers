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

# Flash-моделі для текстових постів — пріоритет.
# Спершу конкретні версії, аліас `flash-latest` — в кінець: він періодично
# віддає 503 і таймаути, бо за ним стоїть найзавантаженіша модель.
_GEMINI_FLASH_ORDER = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
)

# Моделі, які Google вже вивів з обігу: віддають 404 «no longer available».
# Тримаємо окремо, бо застаріле значення в .env інакше з'їдає половину
# ланцюжка — і один 503 на решті валить увесь пост.
_GEMINI_RETIRED = (
    "gemini-1.0",
    "gemini-1.5",
    "gemini-2.0",
    "gemini-2.5",
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
# Менше трьох — ризиковано: 503 на одній моделі означає втрачений пост.
GEMINI_MAX_MODELS_PER_REQUEST = int(os.getenv("GEMINI_MAX_MODELS_PER_REQUEST", "4"))


def _is_usable_text_model(name: str) -> bool:
    """True лише для живих flash-моделей генерації тексту."""
    n = name.lower()
    if "-pro" in n or n.endswith("pro-latest"):
        return False
    if any(n.startswith(retired) for retired in _GEMINI_RETIRED):
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

    cap = max(1, GEMINI_MAX_MODELS_PER_REQUEST)

    # Добиваємо ланцюжок відомими живими моделями до ліміту: якщо в .env лишились
    # одна-дві назви (чи мертві), один 503 не має валити пост.
    for model in _GEMINI_FLASH_ORDER:
        if len(ordered) >= cap:
            break
        if model not in seen:
            ordered.append(model)
            seen.add(model)

    return ordered[:cap] or list(_GEMINI_FLASH_ORDER[:2])


_GEMINI_RAW = [
    model.strip().removeprefix("models/")
    for model in os.getenv(
        "GEMINI_MODELS",
        ",".join(_GEMINI_FLASH_ORDER),
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
# Денний ліміт «опційних» Gemini-викликів (відео + банкноти).
# Розклад рубрик його НЕ чіпає — щоб free tier вистачало на звичайні пости.
# 0 = без ліміту (платний ключ).
GEMINI_OPTIONAL_MAX_PER_DAY = int(os.getenv("GEMINI_OPTIONAL_MAX_PER_DAY", "2"))

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
# Скільки кандидатів тягнемо зі стоку за один пошук (для скорингу).
STOCK_PHOTO_CANDIDATES = int(os.getenv("STOCK_PHOTO_CANDIDATES", "8"))
# Не повторювати те саме фото протягом N днів.
STOCK_PHOTO_DEDUP_DAYS = int(os.getenv("STOCK_PHOTO_DEDUP_DAYS", "14"))

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
# БАНКНОТИ — лише свіжі / ювілейні випуски
# ─────────────────────────────────────────
# Немає окремого API «нових банкнот» — збираємо NewsAPI + Google News RSS,
# жорстко фільтруємо аукціони/каталоги, постимо подієво (монітор).
BANKNOTE_NEWS_QUERY = (
    '("new banknote" OR "commemorative banknote" OR "anniversary banknote" '
    'OR "jubilee banknote" OR "new series" banknotes OR "issues new" banknote '
    'OR "unveils" banknote OR "put into circulation" banknote)'
)
BANKNOTE_RSS_FEEDS = [
    (
        "https://news.google.com/rss/search?"
        "q=%22new+banknote%22+OR+%22commemorative+banknote%22+"
        "OR+%22anniversary+banknote%22+OR+%22new+series%22+banknotes"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    (
        "https://news.google.com/rss/search?"
        "q=%D0%BD%D0%BE%D0%B2%D0%B0+%D0%B1%D0%B0%D0%BD%D0%BA%D0%BD%D0%BE%D1%82%D0%B0+"
        "OR+%D1%8E%D0%B2%D1%96%D0%BB%D0%B5%D0%B9%D0%BD%D0%B0+%D0%B1%D0%B0%D0%BD%D0%BA%D0%BD%D0%BE%D1%82%D0%B0"
        "&hl=uk&gl=UA&ceid=UA:uk"
    ),
]
BANKNOTE_LOOKBACK_DAYS = int(os.getenv("BANKNOTE_LOOKBACK_DAYS", "21"))
BANKNOTE_MIN_SCORE = int(os.getenv("BANKNOTE_MIN_SCORE", "10"))
BANKNOTE_MAX_CANDIDATES = int(os.getenv("BANKNOTE_MAX_CANDIDATES", "8"))
# 0 = без денного ліміту алертів (дедуп усе одно блокує повтори).
BANKNOTE_MAX_PER_DAY = int(os.getenv("BANKNOTE_MAX_PER_DAY", "0"))
# Не треба оперативність: раз на N днів достатньо, аби не пропустити випуски.
BANKNOTE_POLL_DAYS = int(os.getenv("BANKNOTE_POLL_DAYS", "7"))
# Застарілий інтервал у хвилинах (якщо BANKNOTE_POLL_DAYS не задано через старий .env).
BANKNOTE_POLL_MINUTES = int(os.getenv("BANKNOTE_POLL_MINUTES", str(7 * 24 * 60)))
BANKNOTE_MAX_PER_CYCLE = int(os.getenv("BANKNOTE_MAX_PER_CYCLE", "3"))
BANKNOTE_USE_SEARCH = os.getenv("BANKNOTE_USE_SEARCH", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
# bonistika.net — спеціалізовані новини (RU→UA через Gemini).
BANKNOTE_BONISTIKA = os.getenv("BANKNOTE_BONISTIKA", "1").strip().lower() in {
    "1", "true", "yes", "on",
}
BANKNOTE_BONISTIKA_LIMIT = int(os.getenv("BANKNOTE_BONISTIKA_LIMIT", "6"))

# ─────────────────────────────────────────
# 4 ПЕРСОНИ-АВТОРИ
# ─────────────────────────────────────────
PERSONAS = [
    {
        "name": "Тато",
        "role": "Аналітик і технар",
        "style": (
            "Пишеш у Telegram як батько, що шарить у цифрах — коротко, по суті, "
            "без лекцій. Факти й конкретні приклади, інколи сухуватий гумор."
        ),
        "emoji_style": "📊📈🔢",
        "cta_style": "аналітичний — 'Перевір сам', 'Ось дані'",
    },
    {
        "name": "Мама",
        "role": "Вчителька і наставниця",
        "style": (
            "Пояснюєш складнощі простими словами, як у переписці з підлітком. "
            "Тепло, без моралізаторства; 1 побутовий приклад краще за теорію."
        ),
        "emoji_style": "💡📚🌟",
        "cta_style": "мотиваційний — 'Спробуй сьогодні', 'Ти впораєшся'",
    },
    {
        "name": "Дядя",
        "role": "Підприємець і мотиватор",
        "style": (
            "Жваво, як друг, що вже щось пробував. Про можливості й перший крок, "
            "без криків «мільйонер за тиждень» і без мотиваційних кліше."
        ),
        "emoji_style": "🚀🔥💪",
        "cta_style": "заклик до дії — 'Зроби це зараз', 'Не чекай'",
    },
    {
        "name": "Баба",
        "role": "Скептик і критик",
        "style": (
            "Обережно й по-людськи попереджаєш про ризики. Не лякаєш — пояснюєш, "
            "де підвох, мовою «краще перевірити двічі»."
        ),
        "emoji_style": "⚠️🤔👀",
        "cta_style": "застережливий — 'Будь обережний', 'Перевір двічі'",
    },
]

# ─────────────────────────────────────────
# РЕДАКЦІЙНИЙ БРИФ — роль моделі перед персоною
# ─────────────────────────────────────────
# Модель працює як команда, а не як чат-бот: стратегія + копірайтинг +
# креатив + робота зі спільнотою. Ціль ланцюжка: увага → дочитування →
# довіра → підписка → коментар.
EDITORIAL_TEAM_BRIEF = (
    "Ти працюєш одночасно як content strategist, копірайтер, креативний "
    "директор, community manager і аналітик каналу — не як чат-бот. "
    "Ланцюг цілей кожного посту: зачепити з першого рядка → дати дочитати "
    "до кінця → залишити відчуття «тут пишуть нормально» → отримати "
    "коментар чи збереження. Пост мусить читатися так, ніби його написала "
    "жива людина, якій самій цікава тема, а не згенерував асистент."
)

# Ротація зачіпок — щоб пости не починалися однаково.
POST_HOOK_STYLES = [
    "почни з конкретної цифри або факту, який ламає очікування",
    "почни з короткої сцени на 1–2 речення, у яку читач може себе поставити",
    "почни одразу з суті, без розгону — перше речення вже по темі",
    "почни з несподіваного порівняння з побутовою річчю",
    "почни з того, що більшість робить неправильно, без повчального тону",
    "почни з живої деталі або цитати з ситуації",
]

# Ротація фіналів — не кожен пост мусить закінчуватись питанням.
POST_ENDING_STYLES = [
    "закінчи конкретним питанням про особистий досвід читача (не загальним «а що думаєш ти?»)",
    "закінчи мікро-дією, яку можна зробити за 1 хвилину",
    "закінчи чесним нюансом або тим, де тут легко влетіти",
    "закінчи короткою власною думкою-реплікою, без питання",
    "закінчи вибором із двох варіантів, щоб читачу було що написати в комментарі",
]

# Фрази, які видають ШІ або вже набили оскому в каналі.
BANNED_AI_PHRASES = [
    "що це означає для тебе",
    "що це значить для тебе",
    "чому це важливо саме тобі",
    "для тебе це означає",
    "важливо зазначити",
    "варто підкреслити",
    "у сучасному світі",
    "в епоху цифрових технологій",
    "давайте розберемо",
    "підсумовуючи",
    "отже, підсумуємо",
    "сподіваюся, це було корисно",
    "а що думаєш ти?",
    "як бачиш",
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
# - між постами дня — кілька годин проміжку;
# - хвилини «нерівні», щоб не виглядало як бот на xx:00.
# Фактичний час = слот + рандомний зсув (див. нижче).
#
# Денний слот (~14:40–15:10) тримаємо під рубрики, що не витрачають Gemini:
# опитування, дані з відкритих API, шаблонні картки. Див. PLAN.md.
SCHEDULE = {
    "monday": [
        # Тимчасово вимкнено #ВгадайКраїну / #КраїнаВДеталях — повернути слот нижче.
        # {"time": "14:52", "rubric": "country"},
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

# Ротація рубрик у межах одного слота: ключ у SCHEDULE → що він чергує.
# Лічильник живе в Redis, тому перезапуск бота не збиває чергу.
# Ключі-ротації самі в GENERATORS не потрібні — розгортаються в scheduler.
ROTATIONS = {
    "country": ["country_guess", "country_details"],
}

# Чим закрити слот, якщо рубрика не знайшла матеріалу (порожній результат).
# Має бути безкоштовною рубрикою. Порожньо = слот просто пропускається.
RUBRIC_FALLBACK = os.getenv("RUBRIC_FALLBACK", "")

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
# videoDuration="short" у YouTube API = коротше 4 хв, тому демо й розбори
# (5-20 хв) у видачу майже не потрапляли — лишались Shorts. Тривалість
# фільтруємо самі за contentDetails.
VIDEO_MIN_DURATION_SEC = int(os.getenv("VIDEO_MIN_DURATION_SEC", "90"))
VIDEO_MAX_DURATION_SEC = int(os.getenv("VIDEO_MAX_DURATION_SEC", "1500"))
# order="date" віддавав просто найновіше за вікном, а не найкраще по темі.
YOUTUBE_SEARCH_ORDER = os.getenv("YOUTUBE_SEARCH_ORDER", "relevance")
# Скільки YouTube Search запитів за один цикл монітора (кожен ≈ 100 units квоти).
# Раніше брали всі 7 тем → легко ловили 429. Ротація покриває всі теми за кілька днів.
VIDEO_SEARCH_QUERIES_PER_RUN = 3
# Кеш результатів пошуку в Redis — щоб повторні цикли не палили квоту.
VIDEO_SEARCH_CACHE_TTL_SEC = 10800    # 3 години (як інтервал монітора)

# Економія Gemini на free tier:
# - 1 сканування YouTube+Gemini на день; зайві «норм» відео — у чергу на наступні дні;
# - відео/банкноти ділять GEMINI_OPTIONAL_MAX_PER_DAY.
VIDEO_MIN_RANK_SCORE = 1
VIDEO_MIN_CANDIDATES = 1
VIDEO_GEMINI_COOLDOWN_HOURS = int(os.getenv("VIDEO_GEMINI_COOLDOWN_HOURS", "8"))
VIDEO_REJECT_TTL_SEC = 12 * 3600
# Скільки «зайвих» відео тримати в черзі на наступні дні.
VIDEO_QUEUE_MAX = int(os.getenv("VIDEO_QUEUE_MAX", "5"))
# Щоденний слот #ВідеоТижня (Київ) — не в реалтайм-моніторі.
VIDEO_SCHEDULE_TIME = os.getenv("VIDEO_SCHEDULE_TIME", "18:27")

# ─────────────────────────────────────────
# ТРЕНАЖЕР МОЗКУ (#ТренажерМозку)
# ─────────────────────────────────────────
# Стиль «Клуб 1%»: кмітливість, візуальна задача на картинці, бейдж «лише N%».
# Більшість типів — код без Gemini. Тип `clever` викликає Gemini.
BRAIN_SCHEDULE_TIME = os.getenv("BRAIN_SCHEDULE_TIME", "21:07")
BRAIN_TYPES = (
    # Нові «клубні» типи — частіше в ротації (ваги в generators/brain.py).
    "pyramid", "mirror", "hidden_seq", "clever",
    "sequence", "rebus", "odd_one_out",
    "anagram", "caesar",
    # Детективний клуб: задачі з солвером, відповідь гарантовано єдина.
    "alibi", "liar", "order",
)
BRAIN_LEVEL_MIN = 1
BRAIN_LEVEL_MAX = 5
BRAIN_START_LEVEL = int(os.getenv("BRAIN_START_LEVEL", "2"))
# Складність рухається за точністю відповідей каналу.
BRAIN_LEVEL_UP_ACCURACY = int(os.getenv("BRAIN_LEVEL_UP_ACCURACY", "75"))
BRAIN_LEVEL_DOWN_ACCURACY = int(os.getenv("BRAIN_LEVEL_DOWN_ACCURACY", "40"))
# Менше голосів — вибірка невиразна, складність не рухаємо.
BRAIN_MIN_VOTES_FOR_ADAPT = int(os.getenv("BRAIN_MIN_VOTES_FOR_ADAPT", "5"))
# Скільки днів пам'ятаємо задачу, щоб не повторити її.
BRAIN_SEEN_TTL_DAYS = int(os.getenv("BRAIN_SEEN_TTL_DAYS", "120"))

# ─────────────────────────────────────────
# КРАЇНИ (#ВгадайКраїну / #КраїнаВДеталях)
# ─────────────────────────────────────────
# Дані — Wikidata (українські назви в комплекті) + прапори з FlagCDN.
# Обидва джерела без ключів; REST Countries із 2026-го вимагає авторизацію.
# Кеш довідника, бо SPARQL-запит триває десятки секунд.
COUNTRY_CACHE_TTL_DAYS = int(os.getenv("COUNTRY_CACHE_TTL_DAYS", "14"))
# Скільки днів не повторювати країну (окремо для кожного режиму).
COUNTRY_SEEN_TTL_DAYS = int(os.getenv("COUNTRY_SEEN_TTL_DAYS", "180"))
# Відсіюємо мікродержави: про них немає що розповісти підлітку.
COUNTRY_MIN_POPULATION = int(os.getenv("COUNTRY_MIN_POPULATION", "300000"))
# «Вгадай країну»: скільки варіантів у опитуванні і скільки з них — сусіди
# по континенту (з одного континенту складніше, ніж навмання).
COUNTRY_POLL_OPTIONS = int(os.getenv("COUNTRY_POLL_OPTIONS", "4"))
COUNTRY_SAME_CONTINENT_ODDS = float(os.getenv("COUNTRY_SAME_CONTINENT_ODDS", "0.7"))

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

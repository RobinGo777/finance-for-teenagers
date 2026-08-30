import json
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx

from bot.publisher import (
    _clean_poll_options,
    _clean_poll_question,
    _prepare_html,
    _split_message,
    _split_caption,
    _strip_hashtags,
    CAPTION_LIMIT,
)
from generators.video import (
    SEARCH_QUERY_GROUPS,
    _news_match_score,
    _queries_for_day,
    _video_rank,
)
from bot.moderator import get_test_rubrics
from config import SCHEDULE, VIDEO_SEARCH_QUERIES_PER_RUN, _normalize_gemini_models
from data import redis_client
from generators import gemini
from images.generator import generate_post_image, _score_photo, _photo_queries
from scheduler.daily_scheduler import GENERATORS
from scheduler.monitor import _stable_id
from utils.http_safe import redact_secrets, safe_error_text


class PublisherHelpersTests(unittest.TestCase):
    def test_post_image_omits_rubric_and_persona(self) -> None:
        template = {
            "name": "Test",
            "bg": "#101010",
            "accent": "#00ff94",
            "emoji": "",
        }
        with patch("images.generator.ImageDraw.ImageDraw.text") as draw_text:
            generate_post_image(
                title="Чистий заголовок",
                body="Коротке пояснення",
                rubric="#ТаємнийХештег",
                persona_name="Таємний Автор",
                template=template,
            )

        drawn_strings = [
            arg
            for call in draw_text.call_args_list
            for arg in call.args
            if isinstance(arg, str)
        ]
        self.assertNotIn("#ТаємнийХештег", drawn_strings)
        self.assertFalse(any("Таємний Автор" in text for text in drawn_strings))

    def test_photo_queries_prefer_english(self) -> None:
        queries = _photo_queries(
            title="Крипта і шахрайство для підлітків",
            body="Як не втратити гроші",
            rubric="crime",
        )
        joined = " ".join(queries).lower()
        self.assertTrue(any("cryptocurrency" in q or "scam" in q or "cyber" in q for q in queries))
        self.assertNotIn("крипта і шахрайство", joined)

    def test_photo_score_penalizes_cliches(self) -> None:
        good = {
            "alt": "cybersecurity phishing warning on laptop screen",
            "description": "",
            "url": "https://example.com/a.jpg",
            "width": 1800,
            "height": 1200,
            "likes": 80,
            "query": "cybersecurity phishing warning laptop",
        }
        cliche = {
            "alt": "businessman smiling handshake suit tie portrait",
            "description": "stacks of cash dollar bills flying",
            "url": "https://example.com/b.jpg",
            "width": 800,
            "height": 600,
            "likes": 10,
            "query": "cybersecurity phishing warning laptop",
        }
        self.assertGreater(
            _score_photo(good, good["query"], ["phishing", "scam"]),
            _score_photo(cliche, cliche["query"], ["phishing", "scam"]),
        )

    def test_html_is_escaped_and_limited(self) -> None:
        raw = "<b>5 & 7</b>" + ("x" * CAPTION_LIMIT)
        prepared = _prepare_html(raw, CAPTION_LIMIT)

        self.assertIn("&lt;b&gt;5 &amp; 7&lt;/b&gt;", prepared)
        self.assertTrue(prepared.endswith("…"))
        self.assertNotIn("<b>", prepared)
        self.assertLessEqual(len(prepared), CAPTION_LIMIT)

    def test_strip_hashtags_from_posts(self) -> None:
        raw = "🧾 #СкількиКоштує\n\nЦіль: новий телефон\n#Фінанси"
        cleaned = _strip_hashtags(raw)
        self.assertNotIn("#", cleaned)
        self.assertIn("🧾", cleaned)
        self.assertIn("СкількиКоштує", cleaned)
        self.assertIn("Ціль: новий телефон", cleaned)
        self.assertIn("Фінанси", cleaned)

    def test_poll_values_follow_telegram_limits(self) -> None:
        question = _clean_poll_question("q" * 500)
        options = _clean_poll_options(["", "a" * 150, " normal ", *range(20)])

        self.assertEqual(len(question), 300)
        self.assertLessEqual(len(options), 10)
        self.assertTrue(all(0 < len(option) <= 100 for option in options))

    def test_long_preview_is_split_without_data_loss(self) -> None:
        text = ("абзац\n" * 1000).strip()
        chunks = _split_message(text, limit=200)

        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_split_caption_keeps_full_text(self) -> None:
        text = "x" * 3000
        caption, rest = _split_caption(text)

        self.assertLessEqual(len(caption), 1000)
        self.assertLessEqual(len(caption), CAPTION_LIMIT)
        self.assertEqual(caption + "".join(rest), text)

    def test_split_caption_short_text_has_no_rest(self) -> None:
        caption, rest = _split_caption("короткий пост")

        self.assertEqual(caption, "короткий пост")
        self.assertEqual(rest, [])


class VideoScoringTests(unittest.TestCase):
    def test_daily_queries_rotate_subset_not_all_categories(self) -> None:
        first_day = _queries_for_day(date(2026, 7, 19))
        next_day = _queries_for_day(date(2026, 7, 20))

        self.assertEqual(len(first_day), VIDEO_SEARCH_QUERIES_PER_RUN)
        self.assertEqual(len(next_day), VIDEO_SEARCH_QUERIES_PER_RUN)
        self.assertLessEqual(VIDEO_SEARCH_QUERIES_PER_RUN, len(SEARCH_QUERY_GROUPS))
        self.assertTrue(
            {category for category, _ in first_day}.issubset(SEARCH_QUERY_GROUPS)
        )
        self.assertNotEqual(first_day, next_day)

        # За тиждень ротація має покрити всі категорії.
        seen: set[str] = set()
        for offset in range(len(SEARCH_QUERY_GROUPS)):
            day = date(2026, 7, 19 + offset)
            seen.update(category for category, _ in _queries_for_day(day))
        self.assertEqual(seen, set(SEARCH_QUERY_GROUPS))

    def test_visual_trusted_video_outranks_talking_news(self) -> None:
        demo = {
            "title": "We built and tested a new robot prototype",
            "description": "Hands-on engineering demo",
            "channel": "Veritasium",
            "views": 20_000,
        }
        talking_news = {
            "title": "Prime Minister addresses rocket programme",
            "description": "Breaking news speech and statement",
            "channel": "NDTV India",
            "views": 2_000_000,
        }

        self.assertGreater(_video_rank(demo, []), _video_rank(talking_news, []))

    def test_news_match_is_soft_bonus_not_filter(self) -> None:
        # Збіг зі свіжими новинами додає бонус…
        score = _news_match_score(
            "OpenAI releases new robot model",
            ["OpenAI robot breakthrough announced"],
        )
        self.assertGreaterEqual(score, 2)

        # …але відсутність збігу дає 0, а не виключає відео (фільтр не жорсткий).
        self.assertEqual(
            _news_match_score("random cooking tutorial", ["stock market update"]),
            0,
        )


class MonitorTests(unittest.TestCase):
    def test_stable_id_is_deterministic(self) -> None:
        first = _stable_id("news", "Одна й та сама новина")
        second = _stable_id("news", "Одна й та сама новина")

        self.assertEqual(first, second)
        self.assertNotEqual(first, _stable_id("news", "Інша новина"))


class ScheduleTests(unittest.TestCase):
    def test_every_scheduled_rubric_has_generator(self) -> None:
        from config import ROTATIONS

        scheduled = {
            slot["rubric"]
            for day in SCHEDULE.values()
            for slot in day
        }

        self.assertTrue(scheduled)
        # Ключ слота — або рубрика, або ротація, що розгортається в рубрики.
        self.assertTrue(scheduled.issubset(set(GENERATORS) | set(ROTATIONS)))
        self.assertNotIn("digit_of_week", scheduled)
        self.assertNotIn("money_hack", GENERATORS)
        self.assertIn("behavioral_finance", scheduled)
        self.assertIn("startup_week", scheduled)
        self.assertIn("cyber", GENERATORS)

    def test_test_all_contains_every_generator_once(self) -> None:
        rubrics = get_test_rubrics()

        self.assertEqual(len(rubrics), len(set(rubrics)))
        self.assertEqual(set(rubrics), set(GENERATORS))


class RedisHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_smembers_returns_builtin_set(self) -> None:
        with patch.object(
            redis_client,
            "_request",
            new=AsyncMock(return_value={"result": ["one", "two"]}),
        ):
            result = await redis_client.smembers("topics")

        self.assertEqual(result, {"one", "two"})

    async def test_save_topic_uses_bounded_list(self) -> None:
        with patch.object(
            redis_client,
            "_request",
            new=AsyncMock(return_value={"result": "OK"}),
        ) as request:
            await redis_client.save_topic("crypto", "Bitcoin")

        commands = [call.args[0] for call in request.await_args_list]
        self.assertEqual(commands[0], ["LPUSH", "crypto:used_topics", "Bitcoin"])
        self.assertEqual(commands[1][0:3], ["LTRIM", "crypto:used_topics", 0])

    async def test_quiz_results_tally_votes(self) -> None:
        # user_id -> обраний варіант; підсумок = скільки за кожен варіант.
        with patch.object(
            redis_client,
            "hgetall",
            new=AsyncMock(return_value={"11": "0", "22": "1", "33": "0"}),
        ):
            results = await redis_client.get_quiz_results("poll1")

        self.assertEqual(results, {"0": 2, "1": 1})


class GeminiFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_json_mode_without_search_sets_mime_type(self) -> None:
        payload = gemini._build_payload("test", use_search=False, json_mode=True)

        self.assertEqual(
            payload["generationConfig"]["responseMimeType"],
            "application/json",
        )
        self.assertNotIn("tools", payload)
        self.assertNotIn("thinkingConfig", payload["generationConfig"])
        self.assertNotIn("key=", gemini._model_url("gemini-test"))

    def test_text_mode_includes_thinking_config(self) -> None:
        payload = gemini._build_payload("test", use_search=False, json_mode=False)
        self.assertIn("thinkingConfig", payload["generationConfig"])

    def test_json_mode_never_enables_search_tool(self) -> None:
        # google_search + JSON-вивід несумісні → порожня відповідь. У JSON-режимі
        # інструмент пошуку не додаємо навіть якщо use_search=True.
        payload = gemini._build_payload("test", use_search=True, json_mode=True)

        self.assertNotIn("tools", payload)
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"],
            "application/json",
        )

    def test_search_tool_used_for_plain_text_generation(self) -> None:
        payload = gemini._build_payload("test", use_search=True, json_mode=False)

        self.assertEqual(payload["tools"], [{"google_search": {}}])
        self.assertNotIn("responseMimeType", payload["generationConfig"])

    def test_extract_json_handles_trailing_extra_brace(self) -> None:
        # Модель інколи додає зайву `}` у кінці (реальний кейс рубрики video).
        raw = '```json\n{"video_id": "abc", "post": "текст"}\n}\n```'
        parsed = json.loads(gemini._extract_json(raw))

        self.assertEqual(parsed["video_id"], "abc")
        self.assertEqual(parsed["post"], "текст")

    async def test_invalid_json_switches_to_fallback_model(self) -> None:
        request = AsyncMock(side_effect=["not json", '{"ok": true}'])
        with (
            patch.object(gemini, "GEMINI_MODELS", ["primary", "fallback"]),
            # Саме JSON_MODEL_RETRIES керує повторами в generate_json.
            patch.object(gemini, "JSON_MODEL_RETRIES", 1),
            patch.object(gemini, "_ensure_not_paused", AsyncMock()),
            patch.object(gemini, "_request_model", new=request),
        ):
            result = await gemini.generate_json("test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            [call.args[0] for call in request.await_args_list],
            ["primary", "fallback"],
        )

    async def test_model_403_switches_to_fallback(self) -> None:
        """Недоступна модель (403) не валить весь ланцюжок — беремо наступну."""
        forbidden = httpx.HTTPStatusError(
            "Forbidden",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(
                403,
                json={"error": {"message": "model not found", "status": "NOT_FOUND"}},
            ),
        )

        async def request(model: str, payload: dict) -> str:
            if model == "broken":
                raise forbidden
            return '{"ok": true}'

        with (
            patch.object(gemini, "GEMINI_MODELS", ["broken", "fallback"]),
            patch.object(gemini, "JSON_MODEL_RETRIES", 2),
            patch.object(gemini, "_ensure_not_paused", AsyncMock()),
            patch.object(gemini, "_request_model", new=request),
        ):
            result = await gemini.generate_json("test")

        self.assertEqual(result, {"ok": True})

    async def test_503_on_first_model_falls_through_to_a_working_one(self) -> None:
        """Саме цей шлях падав у продакшені: 503 не має валити рубрику."""
        unavailable = httpx.HTTPStatusError(
            "Service Unavailable",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(503, json={"error": {"message": "overloaded"}}),
        )
        calls: list[str] = []

        async def request(model: str, payload: dict) -> str:
            calls.append(model)
            if model in ("dead", "overloaded"):
                raise unavailable
            return '{"ok": true}'

        with (
            patch.object(gemini, "GEMINI_MODELS", ["dead", "overloaded", "alive"]),
            patch.object(gemini, "JSON_MODEL_RETRIES", 1),
            patch.object(gemini, "_ensure_not_paused", AsyncMock()),
            patch.object(gemini, "_sleep_before_retry", AsyncMock()),
            patch.object(gemini, "_request_model", new=request),
        ):
            result = await gemini.generate_json("test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["dead", "overloaded", "alive"])

    def test_fatal_auth_detects_invalid_key_not_model_403(self) -> None:
        invalid_key = httpx.HTTPStatusError(
            "Forbidden",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(
                403,
                json={
                    "error": {
                        "message": "API key not valid. Please pass a valid API key.",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            ),
        )
        model_denied = httpx.HTTPStatusError(
            "Forbidden",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(
                403,
                json={"error": {"message": "models/gemini-3.5-flash is not found"}},
            ),
        )
        self.assertTrue(gemini._is_fatal_auth_error(invalid_key))
        self.assertFalse(gemini._is_fatal_auth_error(model_denied))


class GeminiModelConfigTests(unittest.TestCase):
    def test_normalize_flash_only_no_pro(self) -> None:
        from config import GEMINI_MAX_MODELS_PER_REQUEST

        raw = [
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
            "gemini-3.5-flash-image",
            "gemini-embedding-001",
        ]
        ordered = _normalize_gemini_models(raw)
        self.assertEqual(ordered[0], "gemini-3.5-flash")
        self.assertNotIn("gemini-2.5-pro", ordered)
        self.assertNotIn("gemini-3.1-pro-preview", ordered)
        self.assertNotIn("gemini-3.5-flash-image", ordered)
        self.assertNotIn("gemini-embedding-001", ordered)
        self.assertLessEqual(len(ordered), GEMINI_MAX_MODELS_PER_REQUEST)

    def test_retired_models_are_dropped(self) -> None:
        """Мертві моделі віддають 404 і з'їдають ланцюжок — їх не беремо."""
        ordered = _normalize_gemini_models(
            ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
        )
        for retired in ("gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"):
            self.assertNotIn(retired, ordered)
        self.assertTrue(ordered, "ланцюжок не може бути порожнім")

    def test_chain_is_padded_so_one_503_does_not_kill_the_post(self) -> None:
        from config import GEMINI_MAX_MODELS_PER_REQUEST

        ordered = _normalize_gemini_models(["gemini-3.5-flash"])
        self.assertEqual(len(ordered), GEMINI_MAX_MODELS_PER_REQUEST)
        self.assertEqual(ordered[0], "gemini-3.5-flash")
        self.assertEqual(len(set(ordered)), len(ordered))

    def test_flash_latest_alias_is_not_first(self) -> None:
        """Аліас періодично віддає 503 — тримаємо його в хвості."""
        ordered = _normalize_gemini_models(["gemini-flash-latest"])
        self.assertEqual(ordered[0], "gemini-flash-latest")
        # А в дефолтному порядку конкретні версії йдуть попереду аліаса.
        from config import _GEMINI_FLASH_ORDER

        self.assertEqual(_GEMINI_FLASH_ORDER[-1], "gemini-flash-latest")


class GeminiRateLimitTests(unittest.TestCase):
    def test_daily_quota_detected_from_quota_id(self) -> None:
        error = httpx.HTTPStatusError(
            "Too Many Requests",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(
                429,
                json={
                    "error": {
                        "message": "Resource exhausted",
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                "metadata": {
                                    "quota_id": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                },
                            }
                        ],
                    }
                },
            ),
        )
        # Повідомлення без явного per_day у message — додамо в message для детектора.
        error.response = httpx.Response(
            429,
            json={
                "error": {
                    "message": "Quota exceeded for GenerateRequestsPerDayPerProjectPerModel",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"retryDelay": "3600s"}],
                }
            },
        )
        self.assertTrue(gemini._is_daily_quota_exhausted(error))

    def test_rpm_429_is_not_daily_quota(self) -> None:
        error = httpx.HTTPStatusError(
            "Too Many Requests",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(
                429,
                json={
                    "error": {
                        "message": "Quota exceeded for requests per minute",
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [{"retryDelay": "32s"}],
                    }
                },
            ),
        )
        self.assertFalse(gemini._is_daily_quota_exhausted(error))


class SecretRedactionTests(unittest.TestCase):
    def test_redacts_youtube_and_newsapi_keys(self) -> None:
        raw = (
            "Client error for url "
            "'https://www.googleapis.com/youtube/v3/search?q=ai&key=AIzaSySECRET'"
        )
        self.assertIn("key=***", redact_secrets(raw))
        self.assertNotIn("AIzaSySECRET", redact_secrets(raw))

        news = "https://newsapi.org/v2/everything?apiKey=fa170a69secret&q=AI"
        self.assertIn("apiKey=***", redact_secrets(news))
        self.assertNotIn("fa170a69secret", safe_error_text(Exception(news)))


class BanknoteFilterTests(unittest.TestCase):
    def test_accepts_new_and_commemorative_issues(self) -> None:
        from data.banknotes import score_banknote_item

        self.assertGreater(
            score_banknote_item(
                "Central Bank unveils new 50 euro commemorative banknote"
            ),
            0,
        )
        self.assertGreater(
            score_banknote_item(
                "НБУ презентував ювілейну банкноту до річниці Незалежності"
            ),
            0,
        )

    def test_rejects_auctions_and_price_noise(self) -> None:
        from data.banknotes import score_banknote_item

        self.assertEqual(
            score_banknote_item("Rare 1918 banknote sold for $40,000 at auction"),
            0,
        )
        self.assertEqual(
            score_banknote_item("How much is this old banknote worth? Price guide"),
            0,
        )
        self.assertEqual(
            score_banknote_item("Bitcoin hits new high amid crypto rally"),
            0,
        )

    def test_filter_dedupes_and_ranks(self) -> None:
        from data.banknotes import filter_banknote_candidates

        items = [
            {
                "title": "Bank of X unveils new 20 polymer banknote series",
                "summary": "New series enters circulation next month",
                "url": "https://example.com/a?utm=1",
                "published": "2026-08-01T12:00:00Z",
            },
            {
                "title": "Bank of X unveils new 20 polymer banknote series",
                "summary": "New series enters circulation next month",
                "url": "https://www.example.com/a",
                "published": "2026-08-01T13:00:00Z",
            },
            {
                "title": "Collectors auction rare banknote for record price",
                "url": "https://example.com/spam",
            },
        ]
        filtered = filter_banknote_candidates(items, lookback_days=30)
        self.assertEqual(len(filtered), 1)
        self.assertIn("polymer", filtered[0]["title"].lower())

    def test_banknotes_in_generators(self) -> None:
        self.assertIn("banknotes", GENERATORS)

    def test_same_banknote_different_headlines_match(self) -> None:
        from data.banknotes import issue_fingerprint, titles_too_similar

        self.assertTrue(
            titles_too_similar(
                "ECB unveils new 20 euro commemorative banknote",
                "European Central Bank issues commemorative 20 euro banknote",
            )
        )
        a = issue_fingerprint("euro area", "20 EUR", "ювілейна")
        b = issue_fingerprint("Euro Area", "20 eur", "commemorative")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("bn:issue:"))

    def test_title_fingerprint_stable_across_wording(self) -> None:
        from data.banknotes import title_fingerprint

        # Різний порядок слів, ті самі ключові токени → той самий відбиток.
        a = title_fingerprint("Poland 500 zloty commemorative banknote unveiled")
        b = title_fingerprint("Commemorative 500 zloty banknote Poland unveiled")
        self.assertEqual(a, b)


class BrainTrainerTests(unittest.TestCase):
    def test_every_builder_produces_valid_poll_at_every_level(self) -> None:
        from config import BRAIN_LEVEL_MAX, BRAIN_LEVEL_MIN
        from generators.brain import _BUILDERS

        for kind, build in _BUILDERS.items():
            for level in range(BRAIN_LEVEL_MIN, BRAIN_LEVEL_MAX + 1):
                built = None
                # Генератори випадкові: даємо кілька спроб на комбінацію.
                for _ in range(40):
                    built = build(level)
                    if built:
                        break
                self.assertIsNotNone(built, f"{kind} рівень {level} не згенерувався")
                assert built is not None
                options = built.options
                self.assertGreaterEqual(len(options), 4, kind)
                self.assertEqual(len(set(options)), len(options), kind)
                self.assertTrue(0 <= built.correct_index < len(options), kind)
                self.assertTrue(built.question.strip(), kind)
                self.assertTrue(built.explanation.strip(), kind)
                self.assertTrue(built.fingerprint.startswith(f"{kind}:"), kind)

    def test_poll_values_fit_telegram_limits(self) -> None:
        from bot.publisher import CAPTION_LIMIT, POLL_OPTION_LIMIT, POLL_QUESTION_LIMIT
        from config import BRAIN_LEVEL_MAX, BRAIN_LEVEL_MIN
        from generators.brain import _BUILDERS, RUBRIC_HASHTAG

        for kind, build in _BUILDERS.items():
            for level in range(BRAIN_LEVEL_MIN, BRAIN_LEVEL_MAX + 1):
                for _ in range(10):
                    built = build(level)
                    if not built:
                        continue
                    # В опитування йде коротке питання…
                    self.assertLessEqual(
                        len(built.poll_prompt), POLL_QUESTION_LIMIT, kind
                    )
                    # …а повна умова — у підпис до картинки.
                    caption = f"{RUBRIC_HASHTAG}\n\n{built.question}"
                    self.assertLessEqual(len(caption), CAPTION_LIMIT, kind)
                    for option in built.options:
                        self.assertLessEqual(len(option), POLL_OPTION_LIMIT, kind)

    def test_alibi_answer_is_the_only_suspect_without_alibi(self) -> None:
        from generators.brain import _can_reach_scene, _hhmm, _make_alibi

        # Дорога 10 хв: хто вийшов о 14:25, о 14:30 на місці бути не міг.
        event = 14 * 60 + 30
        self.assertFalse(_can_reach_scene(13 * 60, 14 * 60 + 25, event, 10))
        self.assertTrue(_can_reach_scene(13 * 60, 14 * 60, event, 10))
        self.assertEqual(_hhmm(14 * 60 + 5), "14:05")

        for level in range(1, 6):
            for _ in range(20):
                built = _make_alibi(level)
                if not built:
                    continue
                model = built.model
                could = [
                    index
                    for index, (start, end) in enumerate(model["windows"])
                    if _can_reach_scene(start, end, model["event"], model["travel"])
                ]
                self.assertEqual(could, [built.correct_index])
                self.assertEqual(built.poll_prompt, "Хто міг це зробити?")
                self.assertIn(built.options[built.correct_index], built.explanation)
                # На складних рівнях дорога має справді враховуватись.
                if level >= 3:
                    self.assertGreater(model["travel"], 0)

    def test_liar_puzzle_has_single_consistent_culprit(self) -> None:
        from generators.brain import _make_liar

        for level in range(1, 6):
            for _ in range(20):
                built = _make_liar(level)
                if not built:
                    continue
                statements = built.model["statements"]
                one_truth = built.model["one_truth"]
                count = len(statements)

                valid = []
                for candidate in range(count):
                    truths = 0
                    for index, (kind, target) in enumerate(statements):
                        said_true = (
                            candidate != index if kind == "deny"
                            else candidate == target
                        )
                        truths += 1 if said_true else 0
                    ok = truths == 1 if one_truth else (count - truths) == 1
                    if ok:
                        valid.append(candidate)
                self.assertEqual(valid, [built.correct_index])
                self.assertIn("Відомо:", built.question)

    def test_order_constraints_pin_exactly_one_order(self) -> None:
        from generators.brain import _make_order, _order_holds, _order_solutions

        self.assertTrue(_order_holds(("before", 0, 1), {0: 0, 1: 1}))
        self.assertFalse(_order_holds(("notpos", 0, 0), {0: 0, 1: 1}))

        for level in range(1, 6):
            for _ in range(20):
                built = _make_order(level)
                if not built:
                    continue
                count = len(built.options)
                solutions = _order_solutions(built.model["constraints"], count)
                self.assertEqual(solutions, [built.model["order"]])
                asked = built.model["asked"]
                self.assertEqual(built.model["order"][asked], built.correct_index)
                # Умови мінімізовані: без будь-якої з них розв'язок не єдиний.
                constraints = list(built.model["constraints"])
                self.assertGreaterEqual(len(constraints), 2)
                for constraint in constraints:
                    trimmed = [c for c in constraints if c != constraint]
                    self.assertNotEqual(
                        _order_solutions(trimmed, count),
                        [built.model["order"]],
                        "залишилась зайва умова",
                    )

    def test_logic_puzzle_condition_goes_to_caption_not_poll(self) -> None:
        from bot.publisher import POLL_QUESTION_LIMIT, _poll_prompt
        from generators.brain import _make_alibi

        built = None
        for _ in range(40):
            built = _make_alibi(5)
            if built:
                break
        self.assertIsNotNone(built)
        assert built is not None

        self.assertLessEqual(len(_poll_prompt({"poll_question": built.poll_prompt, "question": built.question})), POLL_QUESTION_LIMIT)
        self.assertGreater(len(built.question), len(built.poll_prompt))
        self.assertTrue(built.image_text or built.question)

    def test_pyramid_top_equals_sum_of_children(self) -> None:
        from generators.brain import _make_pyramid

        for level in range(1, 6):
            for _ in range(15):
                built = _make_pyramid(level)
                if not built:
                    continue
                rows = built.model["rows"]
                for r in range(len(rows) - 1):
                    for c, value in enumerate(rows[r]):
                        self.assertEqual(value, rows[r + 1][c] + rows[r + 1][c + 1])
                self.assertEqual(str(rows[0][0]), built.options[built.correct_index])
                self.assertIn("?", built.image_text)

    def test_mirror_answer_matches_symmetry_rule(self) -> None:
        from generators.brain import _MIRROR_BAD, _MIRROR_OK, _make_mirror

        for level in range(1, 6):
            for _ in range(20):
                built = _make_mirror(level)
                if not built:
                    continue
                answer = built.options[built.correct_index]
                if "НЕ збігається" in built.question:
                    self.assertIn(answer, _MIRROR_BAD)
                else:
                    self.assertIn(answer, _MIRROR_OK)

    def test_hidden_sequence_next_item_is_correct(self) -> None:
        from generators.brain import _MONTHS, _NUMBER_WORDS, _WEEKDAYS, _make_hidden_seq

        catalogs = {
            "months": list(_MONTHS),
            "weekdays": list(_WEEKDAYS),
            "numbers": list(_NUMBER_WORDS),
        }
        for level in range(1, 6):
            for _ in range(20):
                built = _make_hidden_seq(level)
                if not built:
                    continue
                series = built.model["series"]
                answer = built.model["answer"]
                catalog = catalogs[built.model["kind"]]
                start = catalog.index(series[0])
                self.assertEqual(catalog[start:start + len(series)], series)
                self.assertEqual(catalog[start + len(series)], answer)
                self.assertEqual(
                    answer.capitalize() if answer[0].islower() else answer,
                    built.options[built.correct_index],
                )

    def test_percent_badge_grows_harder_with_level(self) -> None:
        from generators.brain import _percent_for

        percents = [_percent_for(level) for level in range(1, 6)]
        self.assertEqual(percents, sorted(percents, reverse=True))
        self.assertEqual(percents[0], 90)
        self.assertEqual(percents[-1], 5)

    def test_clever_accepts_valid_gemini_payload(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, patch
        import generators.brain as brain

        payload = {
            "body": "ЧЕРВЕНЬЛИПЕНЬСЕРПЕНЬ",
            "question": "Що далі?",
            "options": ["Вересень", "Жовтень", "Травень", "Квітень"],
            "correct_index": 0,
            "explanation": "Це місяці підряд, далі вересень.",
        }
        with patch(
            "generators.gemini.generate_json", AsyncMock(return_value=payload)
        ):
            built = asyncio.run(brain._make_clever(3))

        self.assertIsNotNone(built)
        assert built is not None
        self.assertEqual(built.kind, "clever")
        self.assertEqual(built.options[built.correct_index], "Вересень")
        self.assertEqual(built.image_text, payload["body"])

    def test_clever_rejects_broken_gemini_payload(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, patch
        import generators.brain as brain

        with patch(
            "generators.gemini.generate_json",
            AsyncMock(return_value={"body": "x", "options": ["a"]}),
        ):
            built = asyncio.run(brain._make_clever(2))
        self.assertIsNone(built)

    def test_caesar_answer_matches_shift(self) -> None:
        from generators.brain import _caesar_encode

        # б→в, а→б, н→о, к→л за українським алфавітом.
        self.assertEqual(_caesar_encode("банк", 1), "вбол")
        # Зсув уперед і назад повертає те саме слово.
        word = "монета"
        encoded = _caesar_encode(word, 7)
        self.assertNotEqual(encoded, word)
        self.assertEqual(_caesar_encode(encoded, -7), word)

    def test_sequence_answer_follows_stated_rule(self) -> None:
        from generators.brain import _sequence_rule

        for level in range(1, 6):
            for _ in range(30):
                built = _sequence_rule(level)
                self.assertIsNotNone(built)
                assert built is not None
                shown, answer, rule = built
                self.assertEqual(len(shown), 5)
                self.assertTrue(rule.strip())
                self.assertIsInstance(answer, int)

    def test_anagram_letters_match_the_answer(self) -> None:
        from generators.brain import _make_anagram

        for _ in range(30):
            puzzle = _make_anagram(3)
            if not puzzle:
                continue
            letters = sorted(ch.lower() for ch in puzzle.image_text if ch.strip())
            answer = puzzle.options[puzzle.correct_index].lower()
            self.assertEqual(letters, sorted(answer))

    def test_level_adaptation_needs_enough_votes(self) -> None:
        from config import (
            BRAIN_LEVEL_MAX,
            BRAIN_MIN_VOTES_FOR_ADAPT,
        )
        from generators.brain import _next_level

        # Мала вибірка не рухає складність, навіть при 100% точності.
        self.assertEqual(_next_level(3, 100, BRAIN_MIN_VOTES_FOR_ADAPT - 1), 3)
        self.assertEqual(_next_level(3, 90, BRAIN_MIN_VOTES_FOR_ADAPT), 4)
        self.assertEqual(_next_level(3, 20, BRAIN_MIN_VOTES_FOR_ADAPT), 2)
        self.assertEqual(_next_level(3, 60, BRAIN_MIN_VOTES_FOR_ADAPT), 3)
        self.assertEqual(_next_level(BRAIN_LEVEL_MAX, 100, 50), BRAIN_LEVEL_MAX)
        self.assertEqual(_next_level(1, 0, 50), 1)

    def test_brain_goes_through_quiz_poll_path(self) -> None:
        from bot.publisher import POLL_QUIZ_RUBRICS, _moderation_body

        self.assertIn("brain", POLL_QUIZ_RUBRICS)
        self.assertIn("quiz", POLL_QUIZ_RUBRICS)

        body = _moderation_body({
            "rubric": "brain",
            "question": "Збери слово з літер: А Б Н К",
            "options": ["Банк", "Кабан", "Бант", "Кана"],
            "correct_index": 0,
        })
        self.assertIn("Збери слово", body)
        self.assertIn("Правильна: Банк", body)

    def test_tasks_word_declension(self) -> None:
        from generators.brain import _tasks_word

        self.assertEqual(_tasks_word(1), "задачу")
        self.assertEqual(_tasks_word(3), "задачі")
        self.assertEqual(_tasks_word(7), "задач")
        self.assertEqual(_tasks_word(11), "задач")
        self.assertEqual(_tasks_word(13), "задач")
        self.assertEqual(_tasks_word(21), "задачу")
        self.assertEqual(_tasks_word(22), "задачі")

    def test_rebus_answers_are_unique(self) -> None:
        from data.uk_words import REBUSES

        answers = [answer for _, answer, _ in REBUSES]
        self.assertEqual(len(answers), len(set(answers)))
        for puzzle_text, answer, reading in REBUSES:
            self.assertTrue(puzzle_text.strip())
            self.assertTrue(answer.strip())
            self.assertTrue(reading.strip())


class YouTubeSearchTests(unittest.TestCase):
    def test_parse_iso_duration(self) -> None:
        from data.fetchers import parse_iso_duration

        self.assertEqual(parse_iso_duration("PT9M54S"), 594)
        self.assertEqual(parse_iso_duration("PT24S"), 24)
        self.assertEqual(parse_iso_duration("PT1H2M3S"), 3723)
        self.assertEqual(parse_iso_duration("P1DT1H"), 90000)
        # Стріми віддають P0D, сміття — нерозпізнане: обидва → 0 (відсіюємо).
        self.assertEqual(parse_iso_duration("P0D"), 0)
        self.assertEqual(parse_iso_duration(""), 0)
        self.assertEqual(parse_iso_duration("не тривалість"), 0)

    def test_duration_window_excludes_shorts_and_streams(self) -> None:
        from config import VIDEO_MIN_DURATION_SEC, VIDEO_MAX_DURATION_SEC
        from data.fetchers import parse_iso_duration

        def accepted(iso: str) -> bool:
            sec = parse_iso_duration(iso)
            return VIDEO_MIN_DURATION_SEC <= sec <= VIDEO_MAX_DURATION_SEC

        self.assertFalse(accepted("PT24S"))     # Shorts
        self.assertFalse(accepted("P0D"))       # стрім
        self.assertFalse(accepted("PT3H15M"))   # багатогодинний
        self.assertTrue(accepted("PT6M44S"))    # демо
        self.assertTrue(accepted("PT9M54S"))    # розбір

    def test_search_uses_relevance_and_no_duration_filter(self) -> None:
        import inspect

        from data import fetchers

        src = inspect.getsource(fetchers.fetch_youtube_videos)
        # "short" у YouTube API = <4 хв і викидає всі демо — не повертаємо його.
        self.assertNotIn('"videoDuration"', src)
        self.assertNotIn('"order": "date"', src)
        self.assertIn("YOUTUBE_SEARCH_ORDER", src)
        self.assertIn("contentDetails", src)


class PostVoiceTests(unittest.TestCase):
    def test_detects_robotic_phrases(self) -> None:
        from utils.text_quality import find_banned_phrases

        found = find_banned_phrases(
            "Нова модель вийшла.\n\nЩо це означає для тебе: буде дешевше."
        )
        self.assertIn("що це означає для тебе", found)
        self.assertEqual(find_banned_phrases("Просто живий текст без штампів."), [])

    def test_base_prompt_bans_phrases_and_rotates_style(self) -> None:
        from config import EDITORIAL_TEAM_BRIEF, POST_HOOK_STYLES
        from generators.gemini import build_base_prompt

        prompts = {
            build_base_prompt(
                rubric_name="#Тест",
                rubric_hashtag="🧪 Тест",
                task="Напиши тестовий пост",
                used_topics=[],
                persona={
                    "name": "Тато",
                    "role": "Аналітик",
                    "style": "коротко",
                    "emoji_style": "📊",
                    "cta_style": "перевір сам",
                },
            )
            for _ in range(40)
        }

        sample = next(iter(prompts))
        self.assertIn(EDITORIAL_TEAM_BRIEF, sample)
        self.assertIn("що це означає для тебе", sample)
        # Зачіпка ротується → серія промптів не може бути однаковою.
        self.assertGreater(len(prompts), 1)
        self.assertTrue(any(h in sample for h in POST_HOOK_STYLES))


class RotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_cycles_variants_in_order(self) -> None:
        from utils import rotation

        counter = {"value": 0}

        async def fake_incr(key: str) -> int:
            self.assertEqual(key, "rotation:country")
            counter["value"] += 1
            return counter["value"]

        with patch.dict(
            rotation.ROTATIONS, {"country": ["country_guess", "country_details"]}
        ), patch.object(rotation, "incr", fake_incr):
            picked = [await rotation.next_rubric("country") for _ in range(5)]

        self.assertEqual(
            picked,
            [
                "country_guess",
                "country_details",
                "country_guess",
                "country_details",
                "country_guess",
            ],
        )

    async def test_rotation_survives_restart_because_counter_is_in_redis(self) -> None:
        from utils import rotation

        # Лічильник уже «накрутило» попереднє життя процесу.
        with patch.dict(
            rotation.ROTATIONS, {"country": ["country_guess", "country_details"]}
        ), patch.object(rotation, "incr", AsyncMock(return_value=8)):
            self.assertEqual(await rotation.next_rubric("country"), "country_details")

    async def test_key_without_rotation_returns_itself(self) -> None:
        from utils import rotation

        with patch.object(rotation, "incr", AsyncMock()) as incr_mock:
            self.assertEqual(await rotation.next_rubric("crypto"), "crypto")
        incr_mock.assert_not_called()

    async def test_broken_redis_still_gives_a_rubric(self) -> None:
        from utils import rotation

        with patch.dict(
            rotation.ROTATIONS, {"country": ["country_guess", "country_details"]}
        ), patch.object(rotation, "incr", AsyncMock(side_effect=RuntimeError("down"))):
            self.assertEqual(await rotation.next_rubric("country"), "country_guess")

    def test_rotation_variants_are_registered_generators(self) -> None:
        from config import ROTATIONS

        for slot, variants in ROTATIONS.items():
            self.assertNotIn(slot, GENERATORS, f"{slot} — ключ ротації, не рубрика")
            for variant in variants:
                self.assertIn(variant, GENERATORS, variant)

    def test_schedule_rubrics_are_generators_or_rotations(self) -> None:
        from config import ROTATIONS

        for day, slots in SCHEDULE.items():
            for slot in slots:
                rubric = slot["rubric"]
                self.assertTrue(
                    rubric in GENERATORS or rubric in ROTATIONS,
                    f"{day}: {rubric} нікуди не веде",
                )

    def test_daily_slots_do_not_collide(self) -> None:
        """У межах дня слоти мають розходитись у часі, а не лягати поруч."""
        for day, slots in SCHEDULE.items():
            minutes = sorted(
                int(slot["time"][:2]) * 60 + int(slot["time"][3:]) for slot in slots
            )
            gaps = [second - first for first, second in zip(minutes, minutes[1:])]
            for gap in gaps:
                self.assertGreaterEqual(gap, 60, f"{day}: слоти надто близько")


class FallbackRubricTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_slot_is_closed_by_fallback(self) -> None:
        from scheduler import daily_scheduler

        published: list[dict] = []
        empty = AsyncMock(return_value=None)
        filler = AsyncMock(return_value={"rubric": "fact", "post": "факт"})

        with patch.object(daily_scheduler, "RUBRIC_FALLBACK", "fact"), \
             patch.dict(daily_scheduler.GENERATORS, {"crypto": empty, "fact": filler}), \
             patch.object(daily_scheduler, "redis_get", AsyncMock(return_value=None)), \
             patch.object(
                 daily_scheduler,
                 "publish",
                 AsyncMock(side_effect=lambda data: published.append(data)),
             ):
            await daily_scheduler.publish_rubric("crypto")

        self.assertEqual([item["rubric"] for item in published], ["fact"])

    async def test_fallback_does_not_recurse_into_itself(self) -> None:
        from scheduler import daily_scheduler

        empty = AsyncMock(return_value=None)

        with patch.object(daily_scheduler, "RUBRIC_FALLBACK", "fact"), \
             patch.dict(daily_scheduler.GENERATORS, {"fact": empty}), \
             patch.object(daily_scheduler, "redis_get", AsyncMock(return_value=None)), \
             patch.object(daily_scheduler, "publish", AsyncMock()) as publish_mock:
            await daily_scheduler.publish_rubric("fact")

        publish_mock.assert_not_called()
        self.assertEqual(empty.await_count, 1)

    async def test_no_fallback_configured_means_slot_is_skipped(self) -> None:
        from scheduler import daily_scheduler

        with patch.object(daily_scheduler, "RUBRIC_FALLBACK", ""), \
             patch.dict(
                 daily_scheduler.GENERATORS,
                 {"crypto": AsyncMock(return_value=None)},
             ), \
             patch.object(daily_scheduler, "redis_get", AsyncMock(return_value=None)), \
             patch.object(daily_scheduler, "publish", AsyncMock()) as publish_mock:
            await daily_scheduler.publish_rubric("crypto")

        publish_mock.assert_not_called()


class CountryRubricTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _countries() -> list[dict]:
        return [
            {
                "code": code,
                "name_uk": name,
                "capital": capital,
                "population": population,
                "area": 100_000.0,
                "currency": currency,
                "language": "англійська мова",
                "continent": continent,
            }
            for code, name, capital, population, currency, continent in [
                ("jp", "Японія", "Токіо", 123_802_000, "єна", "Азія"),
                ("kr", "Південна Корея", "Сеул", 51_000_000, "вона", "Азія"),
                ("mn", "Монголія", "Улан-Батор", 3_409_939, "тугрик", "Азія"),
                ("vn", "В'єтнам", "Ханой", 98_000_000, "донг", "Азія"),
                ("th", "Таїланд", "Бангкок", 71_000_000, "бат", "Азія"),
                ("pe", "Перу", "Ліма", 34_157_732, "новий соль", "Південна Америка"),
                ("cl", "Чилі", "Сантьяго", 19_000_000, "песо", "Південна Америка"),
                ("is", "Ісландія", "Рейк'явік", 364_260, "крона", "Європа"),
            ]
        ]

    def test_number_formatting_is_readable_in_ukrainian(self) -> None:
        from generators.country import _format_area, _format_population

        self.assertEqual(_format_population(123_802_000), "123,8 млн")
        self.assertEqual(_format_population(41_167_335), "41,2 млн")
        self.assertEqual(_format_population(2_000_000), "2 млн")
        self.assertEqual(_format_population(364_260), "364 тис.")
        self.assertEqual(_format_area(1_221_037.0), "1 221 037 км²")

    def test_flag_emoji_from_iso_code(self) -> None:
        from generators.country import _flag_emoji

        self.assertEqual(_flag_emoji("ua"), "🇺🇦")
        self.assertEqual(_flag_emoji("JP"), "🇯🇵")
        self.assertEqual(_flag_emoji("zzz"), "")

    def test_poll_options_always_include_the_answer_and_are_unique(self) -> None:
        from config import COUNTRY_POLL_OPTIONS
        from generators.country import _pick_options

        countries = self._countries()
        answer = countries[0]
        for _ in range(50):
            options = _pick_options(countries, answer)
            names = [item["name_uk"] for item in options]
            self.assertEqual(len(options), COUNTRY_POLL_OPTIONS)
            self.assertEqual(len(set(names)), len(names))
            self.assertIn(answer["name_uk"], names)

    def test_options_can_be_same_continent_and_can_be_mixed(self) -> None:
        from generators.country import _pick_options

        countries = self._countries()
        answer = countries[0]
        seen_all_asian = False
        seen_mixed = False
        for _ in range(80):
            options = _pick_options(countries, answer)
            continents = {item["continent"] for item in options}
            if continents == {"Азія"}:
                seen_all_asian = True
            else:
                seen_mixed = True

        self.assertTrue(seen_all_asian, "жодного разу не взяв сусідів по континенту")
        self.assertTrue(seen_mixed, "жодного разу не змішав континенти")

    @staticmethod
    def _extras() -> dict:
        return {
            "neighbours": ["Південна Корея", "Монголія", "Європейський Союз", "Гренландія"],
            "waters": ["Японське море", "Тихий океан"],
            "languages": ["японська мова"],
            "drives_left": True,
        }

    def test_hints_never_leak_the_name_of_the_country(self) -> None:
        from generators.country import _hint_lines, _leaks_name

        self.assertTrue(_leaks_name("Японія", "японська мова"))
        self.assertTrue(_leaks_name("Танзанія", "танзанійський шилінг"))
        self.assertTrue(_leaks_name("Ісландія", "ісландська крона"))
        self.assertTrue(_leaks_name("Іран", "іранський ріал"))
        self.assertTrue(_leaks_name("Куба", "кубинський песо"))
        self.assertFalse(_leaks_name("Перу", "новий соль"))

        countries = self._countries()
        japan = countries[0]
        lines = _hint_lines(japan, self._extras(), countries)
        block = "\n".join(lines).lower()
        self.assertNotIn("япон", block)

    def test_hints_list_only_real_neighbour_countries(self) -> None:
        from generators.country import _hint_lines

        countries = self._countries()
        lines = _hint_lines(countries[0], self._extras(), countries)
        neighbours = next(line for line in lines if line.startswith("🧭"))

        # Найбільший сусід іде першим — його впізнають швидше за дрібного.
        self.assertTrue(neighbours.endswith("Південна Корея, Монголія"), neighbours)
        # Wikidata пише в P47 і союзи, і залежні території — їм тут не місце.
        self.assertNotIn("Європейський Союз", neighbours)
        self.assertNotIn("Гренландія", neighbours)

    def test_hints_stay_consistent_for_islands_and_landlocked(self) -> None:
        from generators.country import _hint_lines

        countries = self._countries()
        island = _hint_lines(countries[0], {**self._extras(), "is_island": True}, countries)
        self.assertIn("🏝️ Острівна держава", island)
        # Для острова P47 — це морські межі, тому не «сусіди».
        self.assertTrue(any(line.startswith("🧭 Поблизу:") for line in island))

        landlocked = _hint_lines(
            countries[2],
            {"waters": ["Боденське озеро", "Інн"], "landlocked": True},
            countries,
        )
        self.assertIn("🏔️ Немає виходу до моря", landlocked)
        self.assertFalse(any(line.startswith("🌊") for line in landlocked))

    def test_rivers_and_lakes_do_not_become_seas(self) -> None:
        from generators.country import _hint_lines, _is_sea

        self.assertTrue(_is_sea("Тихий океан"))
        self.assertTrue(_is_sea("Карибське море"))
        self.assertTrue(_is_sea("Мексиканська затока"))
        self.assertFalse(_is_sea("Боденське озеро"))
        self.assertFalse(_is_sea("Інн"))

        countries = self._countries()
        lines = _hint_lines(countries[3], {"waters": ["Меконг", "Південнокитайське море"]}, countries)
        waters = next(line for line in lines if line.startswith("🌊"))
        self.assertIn("Південнокитайське море", waters)
        self.assertNotIn("Меконг", waters)

    def test_official_languages_come_from_wikidata_not_from_one_sample(self) -> None:
        from generators.country import _hint_lines

        countries = self._countries()
        # У довіднику лежить одна випадкова мова, у Wikidata — весь список.
        lines = _hint_lines(
            countries[4],
            {"languages": ["тайська мова", "англійська мова"]},
            countries,
        )
        self.assertIn("🗣️ Серед мов: тайська, англійська", lines)

        single = _hint_lines(countries[4], {"languages": ["тайська мова"]}, countries)
        self.assertIn("🗣️ Мова: тайська", single)

    async def test_official_names_become_the_names_teenagers_use(self) -> None:
        import generators.country as country

        catalog = [
            {
                "code": "cn",
                "name_uk": "Китайська Народна Республіка",
                "capital": "Пекін",
                "population": 1_404_900_000,
                "area": 9_596_961.0,
                "currency": "юань",
                "language": "путунхуа",
                "continent": "Азія",
            }
        ]
        with patch.object(
            country, "get_countries_cache", AsyncMock(return_value=catalog)
        ), patch.object(country, "save_countries_cache", AsyncMock()):
            loaded = await country._load_countries()

        self.assertEqual(loaded[0]["name_uk"], "Китай")
        # Офіційна назва лишається — за нею шукаємо статтю у Вікіпедії.
        self.assertEqual(loaded[0]["full_name"], "Китайська Народна Республіка")

    def test_hints_compare_size_with_ukraine(self) -> None:
        from generators.country import _compare_to_ukraine, _hint_lines

        self.assertEqual(_compare_to_ukraine(41_000_000, 41_167_335), "майже як в Україні")
        self.assertIn("у 3 рази більше", _compare_to_ukraine(120_000_000, 40_000_000))
        self.assertIn("у 1,5 раза більше", _compare_to_ukraine(60_000_000, 40_000_000))
        self.assertIn("у 8 разів менше", _compare_to_ukraine(5_000_000, 40_000_000))

        countries = self._countries() + [
            {
                "code": "ua",
                "name_uk": "Україна",
                "capital": "Київ",
                "population": 41_167_335,
                "area": 603_550.0,
                "currency": "гривня",
                "language": "українська мова",
                "continent": "Європа",
            }
        ]
        lines = _hint_lines(countries[0], self._extras(), countries)
        people = next(line for line in lines if line.startswith("👥"))
        self.assertIn("ніж в Україні", people)

    async def test_guess_post_is_a_valid_poll_without_gemini(self) -> None:
        from bot.publisher import (
            POLL_OPTION_LIMIT,
            POLL_QUESTION_LIMIT,
            POLL_QUIZ_RUBRICS,
            _poll_prompt,
        )
        import generators.country as country

        with patch.object(
            country, "get_countries_cache", AsyncMock(return_value=self._countries())
        ), patch.object(country, "save_countries_cache", AsyncMock()), \
             patch.object(country, "is_country_used", AsyncMock(return_value=False)), \
             patch.object(country, "mark_country_used", AsyncMock()) as mark_used, \
             patch.object(country, "fetch_flag_png", AsyncMock(return_value=b"PNG")), \
             patch.object(
                 country, "fetch_country_extras", AsyncMock(return_value=self._extras())
             ), \
             patch.object(
                 country,
                 "fetch_wikipedia_summary",
                 AsyncMock(return_value={"extract": "Це держава в Азії. Друге речення."}),
             ), \
             patch.object(
                 country, "generate_flag_image_async", AsyncMock(return_value=b"card")
             ), \
             patch.object(country, "generate_json", AsyncMock()) as gemini_mock:
            post = await country.generate_country_guess()

        self.assertIsNotNone(post)
        assert post is not None
        gemini_mock.assert_not_called()
        self.assertIn(post["rubric"], POLL_QUIZ_RUBRICS)
        self.assertLessEqual(len(_poll_prompt(post)), POLL_QUESTION_LIMIT)
        for option in post["options"]:
            self.assertLessEqual(len(option), POLL_OPTION_LIMIT)
        answer = post["options"][post["correct_index"]]
        self.assertIn(answer, post["lamp_post"])
        # Перше речення з Вікіпедії доїжджає, друге — ні.
        self.assertIn("Це держава в Азії.", post["lamp_post"])
        self.assertNotIn("Друге речення", post["lamp_post"])
        mark_used.assert_awaited_once()

        # Підпис — це задача: кілька фактів, ліміт Telegram і жодної назви.
        caption = post["caption"]
        self.assertLessEqual(len(caption), 1024)
        self.assertGreaterEqual(len([line for line in caption.split("\n") if ":" in line]), 4)
        self.assertNotIn(answer, caption)

    async def test_guess_skips_country_when_there_is_almost_nothing_to_reason_from(self) -> None:
        import generators.country as country

        bare = [
            {
                "code": code,
                "name_uk": name,
                "capital": "—",
                "population": 5_000_000,
                "area": 0.0,
                "currency": "",
                "language": "",
                "continent": "",
            }
            for code, name in [
                ("aa", "Аландія"),
                ("bb", "Бендерія"),
                ("cc", "Ценландія"),
                ("dd", "Драговія"),
                ("ee", "Емерія"),
            ]
        ]
        with patch.object(
            country, "get_countries_cache", AsyncMock(return_value=bare)
        ), patch.object(country, "save_countries_cache", AsyncMock()), \
             patch.object(country, "is_country_used", AsyncMock(return_value=False)), \
             patch.object(country, "mark_country_used", AsyncMock()) as mark_used, \
             patch.object(country, "fetch_flag_png", AsyncMock(return_value=b"PNG")), \
             patch.object(country, "fetch_country_extras", AsyncMock(return_value={})):
            self.assertIsNone(await country.generate_country_guess())

        mark_used.assert_not_awaited()

    async def test_guess_skips_country_without_flag(self) -> None:
        import generators.country as country

        with patch.object(
            country, "get_countries_cache", AsyncMock(return_value=self._countries())
        ), patch.object(country, "save_countries_cache", AsyncMock()), \
             patch.object(country, "is_country_used", AsyncMock(return_value=False)), \
             patch.object(country, "mark_country_used", AsyncMock()), \
             patch.object(country, "fetch_flag_png", AsyncMock(return_value=b"")):
            self.assertIsNone(await country.generate_country_guess())

    async def test_wikidata_outage_leaves_slot_empty_instead_of_broken_post(self) -> None:
        import generators.country as country

        with patch.object(country, "get_countries_cache", AsyncMock(return_value=[])), \
             patch.object(
                 country,
                 "fetch_countries",
                 AsyncMock(side_effect=httpx.ConnectError("no network")),
             ), patch.object(country, "save_countries_cache", AsyncMock()):
            self.assertIsNone(await country.generate_country_guess())
            self.assertIsNone(await country.generate_country_details())

    async def test_tiny_states_are_filtered_out(self) -> None:
        import generators.country as country
        from config import COUNTRY_MIN_POPULATION

        countries = self._countries() + [
            {
                "code": "nr",
                "name_uk": "Науру",
                "capital": "Ярен",
                "population": 12_000,
                "area": 21.0,
                "currency": "долар",
                "language": "науру мова",
                "continent": "Австралія й Океанія",
            }
        ]
        with patch.object(
            country, "get_countries_cache", AsyncMock(return_value=countries)
        ), patch.object(country, "save_countries_cache", AsyncMock()):
            loaded = await country._load_countries()

        self.assertTrue(all(c["population"] >= COUNTRY_MIN_POPULATION for c in loaded))
        self.assertNotIn("nr", [c["code"] for c in loaded])

    async def test_answer_post_adds_vote_statistics(self) -> None:
        import generators.country as country

        pending = {"lamp_post": "💡 Відповідь · Це Японія", "correct_index": 1}
        with patch(
            "data.redis_client.get_quiz_pending", AsyncMock(return_value=pending)
        ):
            text = await country.generate_country_answer("poll-1", {"0": 2, "1": 6})
            empty = await country.generate_country_answer("poll-1", {})

        self.assertIn("75%", text)
        self.assertIn("6 з 8", text)
        self.assertIn("ніхто не проголосував", empty)

    def test_answer_dispatch_knows_the_guess_rubric(self) -> None:
        from bot.publisher import _ANSWER_GENERATORS

        self.assertIn("country_guess", _ANSWER_GENERATORS)
        for module_name, function_name in _ANSWER_GENERATORS.values():
            module = __import__(module_name, fromlist=[function_name])
            self.assertTrue(callable(getattr(module, function_name)))


if __name__ == "__main__":
    unittest.main()

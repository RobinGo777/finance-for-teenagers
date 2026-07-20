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
from images.generator import generate_post_image
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

    def test_html_is_escaped_and_limited(self) -> None:
        raw = "<b>5 & 7</b>" + ("x" * CAPTION_LIMIT)
        prepared = _prepare_html(raw, CAPTION_LIMIT)

        self.assertIn("&lt;b&gt;5 &amp; 7&lt;/b&gt;", prepared)
        self.assertTrue(prepared.endswith("…"))
        self.assertNotIn("<b>", prepared)
        self.assertLessEqual(len(prepared), CAPTION_LIMIT)

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
        scheduled = {
            rubric
            for day in SCHEDULE.values()
            for rubric in day["rubrics"]
        }

        self.assertTrue(scheduled)
        self.assertTrue(scheduled.issubset(GENERATORS))
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
            patch.object(gemini, "MODEL_RETRIES", 1),
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
            patch.object(gemini, "MODEL_RETRIES", 2),
            patch.object(gemini, "_request_model", new=request),
        ):
            result = await gemini.generate_json("test")

        self.assertEqual(result, {"ok": True})

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
    def test_normalize_puts_flash_first_and_pro_last(self) -> None:
        raw = [
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ]
        ordered = _normalize_gemini_models(raw)
        self.assertEqual(ordered[0], "gemini-2.5-flash")
        self.assertIn("gemini-2.0-flash", ordered)
        self.assertLess(
            ordered.index("gemini-2.5-flash"),
            ordered.index("gemini-2.5-pro"),
        )
        self.assertLess(
            ordered.index("gemini-3.5-flash"),
            ordered.index("gemini-3.1-pro-preview"),
        )


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


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import AsyncMock, patch

from bot.publisher import (
    _clean_poll_options,
    _clean_poll_question,
    _prepare_html,
    _split_message,
    _split_caption,
    CAPTION_LIMIT,
)
from generators.video import _news_match_score
from bot.moderator import get_test_rubrics
from config import SCHEDULE
from data import redis_client
from generators import gemini
from scheduler.daily_scheduler import GENERATORS
from scheduler.monitor import _stable_id


class PublisherHelpersTests(unittest.TestCase):
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
    def test_payload_uses_current_search_tool_and_json_mode(self) -> None:
        payload = gemini._build_payload("test", use_search=True, json_mode=True)

        self.assertEqual(payload["tools"], [{"google_search": {}}])
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"],
            "application/json",
        )
        self.assertNotIn("key=", gemini._model_url("gemini-test"))

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


if __name__ == "__main__":
    unittest.main()

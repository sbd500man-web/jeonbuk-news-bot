"""실제 메시지를 보내지 않는 동작 검증: python -m unittest -v"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import news_bot as bot

NOW = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)


def article(n, source="네이버"):
    return bot.make_article(source, f"전북 경찰 기사 {n}", "전북 소방 현장",
                            f"https://example.com/news/{n}", NOW - timedelta(minutes=n), "전북")


def page(title="전북 경찰 구조", date="3분 전"):
    return f'''<div id="dnsColl"><ul><li data-docid="one">
    <div class="item-title"><a href="http://v.daum.net/v/20261106005700001">{title}</a></div>
    <p class="conts-desc">전북 <b>소방</b> 소식입니다.</p>
    <span class="gem-subinfo">{date}</span></li></ul></div>'''


class NewsBotTests(unittest.TestCase):
    def test_duplicate_urls_and_keywords(self):
        state = bot.fresh_state()
        a = article(1)
        same = dict(a, id=bot.article_id(a["url"] + "?utm_source=naver"))
        bot.enqueue(state, "네이버", [a, same, a], NOW)
        self.assertEqual(len(state["pending"]), 1)

    def test_first_run_only_five_then_new_items(self):
        state = bot.fresh_state()
        bot.enqueue(state, "네이버", [article(i) for i in range(10)], NOW)
        self.assertEqual(len(state["pending"]), 5)
        self.assertEqual(len(state["seen"]), 5)
        bot.enqueue(state, "네이버", [article(i) for i in range(11)], NOW)
        self.assertEqual(len(state["pending"]), 6)

    @patch("news_bot.time.sleep")
    def test_partial_delivery_failure_survives_restart(self, _):
        state = bot.fresh_state()
        bot.enqueue(state, "네이버", [article(1), article(2)], NOW)
        calls = []
        def sender(message):
            calls.append(message)
            if len(calls) == 2:
                raise bot.BotError("failed")
        with TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            bot.save_state(state, path)
            with self.assertRaises(bot.BotError):
                bot.flush_pending(state, NOW, sender, lambda s: bot.save_state(s, path))
            restored = bot.load_state(path)
            self.assertEqual(len(restored["pending"]), 1)
            self.assertEqual(len(restored["seen"]), 1)
            retry = []
            bot.flush_pending(restored, NOW, retry.append, lambda s: bot.save_state(s, path))
            self.assertEqual(len(retry), 1)
            self.assertEqual(retry[0], calls[1])
            bot.enqueue(restored, "네이버", [article(1), article(2)], NOW)
            self.assertFalse(restored["pending"])

    @patch("news_bot.time.sleep")
    def test_send_cap_keeps_queue(self, _):
        state = bot.fresh_state()
        state["initialized"] = ["네이버"]
        bot.enqueue(state, "네이버", [article(i) for i in range(35)], NOW)
        with patch("news_bot.MAX_SEND_PER_RUN", 30):
            count = bot.flush_pending(state, NOW, lambda _: None, lambda _: None)
        self.assertEqual(count, 30)
        self.assertEqual(len(state["pending"]), 5)

    def test_corrupt_state_fails_closed(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text('{"version":1}', encoding="utf-8")
            with self.assertRaises(bot.BotError):
                bot.load_state(path)

    def test_daum_real_structure_and_relative_time(self):
        results, dates = bot.parse_daum(page(), "전북", NOW)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "전북 경찰 구조")
        self.assertEqual(results[0]["url"], "https://v.daum.net/v/20261106005700001")
        self.assertEqual(dates[0], NOW - timedelta(minutes=3))

    def test_daum_old_news_filtered(self):
        results, dates = bot.parse_daum(page(date="2일 전"), "전북", NOW)
        self.assertFalse(results)
        self.assertEqual(len(dates), 1)

    def test_daum_structure_change_is_failure(self):
        with self.assertRaises(bot.BotError):
            bot.parse_daum("<html>Access denied</html>", "전북", NOW)

    def test_daum_absolute_date_korea_time(self):
        self.assertEqual(bot.daum_date("2026.11.06.", NOW).hour, 0)
        self.assertEqual(bot.daum_date("2026.11.06.", NOW).utcoffset(), timedelta(hours=9))

    def test_naver_empty_page_and_original_link_dedup(self):
        item = {"title": "<b>전북</b> 경찰", "description": "수사 &amp; 소방",
                "link": "https://n.news.naver.com/mnews/article/001/123",
                "originallink": "https://example.com/news/1", "pubDate": "Fri, 06 Nov 2026 00:55:00 +0900"}
        with patch.dict("os.environ", {"NAVER_CLIENT_ID": "fake", "NAVER_CLIENT_SECRET": "fake"}), \
                patch("news_bot.request", return_value=json.dumps({"items": [item, item]})) as api:
            result = bot.collect_naver(NOW, [])
        self.assertEqual(api.call_count, len(bot.KEYWORDS))
        for call in api.call_args_list:
            self.assertTrue(call.args[0].startswith("https://naverapihub.apigw.ntruss.com/search/v1/news?"))
            self.assertIn("format=json", call.args[0])
            self.assertEqual(call.kwargs["headers"], {
                "X-NCP-APIGW-API-KEY-ID": "fake", "X-NCP-APIGW-API-KEY": "fake"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "전북 경찰")
        self.assertEqual(result[0]["id"], bot.article_id(item["originallink"]))

    def test_keyword_spacing_and_irrelevant_news(self):
        self.assertIn("전북 경찰", article(1)["keywords"])
        self.assertIsNone(bot.make_article("네이버", "서울 뉴스", "서울 소식", "https://example.com/1", NOW, "전북"))

    @patch("news_bot.time.sleep")
    def test_post_ambiguous_network_failure_is_not_retried(self, _):
        with patch("news_bot.urlopen", side_effect=TimeoutError("secret url")) as call:
            with self.assertRaises(bot.BotError) as error:
                bot.request("https://example.com/SECRET", payload={"text": "hi"})
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("SECRET", str(error.exception))

    @patch("news_bot.time.sleep")
    def test_get_server_errors_are_retried_without_token_leak(self, _):
        exc = HTTPError("https://example.com/SECRET", 503, "bad", {}, None)
        with patch("news_bot.urlopen", side_effect=exc) as call:
            with self.assertRaises(bot.BotError) as error:
                bot.request("https://example.com/SECRET")
        self.assertEqual(call.call_count, 3)
        self.assertNotIn("SECRET", str(error.exception))

    def test_message_length_and_no_markup_mode(self):
        a = article(1)
        a["title"] = "😀" * 500
        a["summary"] = "😀" * 3000
        a["url"] = "https://example.com/" + "a" * 2000
        self.assertLess(len(bot.format_article(a).encode("utf-16-le")) // 2, 4096)


if __name__ == "__main__":
    unittest.main()

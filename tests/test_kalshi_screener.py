"""Tests for kalshi_screener.py: pricing, the screen's filters, pagination and the CLI.

No network: urlopen and the loaders are replaced with fakes. Market data is fictional.
"""
import io
import json
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import kalshi_screener as ks

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def args(*argv):
    return ks.parse_args(list(argv))


def market(**overrides):
    """A market that passes the default screen (78c mid, 60 days out, 750k volume)."""
    m = {
        "ticker": "KXTEST-26-A", "event_ticker": "EV-SPORTS", "title": "Will the Example Owls win?",
        "yes_bid_dollars": "0.77", "yes_ask_dollars": "0.79", "last_price_dollars": "0.78",
        "volume_fp": "750000", "close_time": (NOW + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    m.update(overrides)
    return m


CATS = {"EV-SPORTS": "Sports", "EV-POL": "Politics"}


class FrozenDatetime(datetime):
    """datetime whose now() is pinned to NOW, so main() is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return NOW


class MidPriceTests(unittest.TestCase):
    def test_two_sided_book_uses_the_mid(self):
        self.assertAlmostEqual(ks.mid_price(market(yes_bid_dollars="0.70", yes_ask_dollars="0.80")), 0.75)

    def test_falls_back_to_last_trade_when_one_side_missing(self):
        m = market(yes_bid_dollars="0.72", yes_ask_dollars=None, last_price_dollars="0.81")
        self.assertAlmostEqual(ks.mid_price(m), 0.81)

    def test_one_sided_book_without_last_trade_uses_the_side_present(self):
        self.assertAlmostEqual(ks.mid_price(market(yes_bid_dollars="0", yes_ask_dollars="0.83",
                                                   last_price_dollars="0")), 0.83)
        self.assertAlmostEqual(ks.mid_price(market(yes_bid_dollars="0.66", yes_ask_dollars="",
                                                   last_price_dollars=None)), 0.66)

    def test_no_usable_price(self):
        self.assertIsNone(ks.mid_price({"yes_bid_dollars": "n/a", "last_price_dollars": "0"}))


class SmallHelperTests(unittest.TestCase):
    def test_is_parlay(self):
        self.assertTrue(ks.is_parlay({"mve_selected_legs": [{"ticker": "X"}]}))
        self.assertTrue(ks.is_parlay({"event_ticker": "KXMVESPORTS-26"}))
        self.assertFalse(ks.is_parlay({"event_ticker": "KXNBA-26", "mve_selected_legs": []}))
        self.assertFalse(ks.is_parlay({}))

    def test_parse_time_handles_zulu_and_garbage(self):
        self.assertEqual(ks._parse_time("2026-06-28T09:00:00Z"),
                         datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc))
        self.assertIsNone(ks._parse_time("next tuesday"))
        self.assertIsNone(ks._parse_time(""))
        self.assertIsNone(ks._parse_time(None))

    def test_trunc(self):
        self.assertEqual(ks._trunc("short", 10), "short")
        self.assertEqual(ks._trunc("line one\nline two", 10), "line one …")
        self.assertEqual(len(ks._trunc("x" * 50, 26)), 26)


class ScreenTests(unittest.TestCase):
    def screen(self, markets, *argv):
        return ks.screen(markets, CATS, args(*argv), NOW)

    def test_passing_market_row_contents(self):
        [row] = self.screen([market()])
        self.assertEqual(row["category"], "Sports")
        self.assertEqual(row["ticker"], "KXTEST-26-A")
        self.assertAlmostEqual(row["mid_c"], 78.0)
        self.assertAlmostEqual(row["bid_c"], 77.0)
        self.assertAlmostEqual(row["ask_c"], 79.0)
        self.assertAlmostEqual(row["spread_c"], 2.0)
        self.assertEqual(row["volume"], 750000.0)
        self.assertAlmostEqual(row["days_to_close"], 60.0)

    def test_excludes_parlays_and_untracked_categories(self):
        rows = self.screen([market(event_ticker="KXMVE-1"), market(event_ticker="EV-WEATHER"),
                            market(mve_selected_legs=["leg"])])
        self.assertEqual(rows, [])

    def test_price_band_is_inclusive(self):
        rows = self.screen([
            market(ticker="LOW-EDGE", yes_bid_dollars="0.70", yes_ask_dollars="0.70"),
            market(ticker="HIGH-EDGE", yes_bid_dollars="0.90", yes_ask_dollars="0.90"),
            market(ticker="TOO-LOW", yes_bid_dollars="0.68", yes_ask_dollars="0.70"),
            market(ticker="TOO-HIGH", yes_bid_dollars="0.90", yes_ask_dollars="0.92"),
            market(ticker="NO-PRICE", yes_bid_dollars=None, yes_ask_dollars=None, last_price_dollars=None),
        ])
        self.assertEqual(sorted(r["ticker"] for r in rows), ["HIGH-EDGE", "LOW-EDGE"])

    def test_days_to_close_window(self):
        def closes_in(days):
            return (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")

        markets = [market(ticker="SOON", close_time=closes_in(29.9)),
                   market(ticker="EDGE", close_time=closes_in(30)),
                   market(ticker="FAR", close_time=closes_in(400)),
                   market(ticker="UNDATED", close_time="")]
        self.assertEqual(sorted(r["ticker"] for r in self.screen(markets)), ["EDGE", "FAR"])
        capped = self.screen(markets, "--max-days", "365")
        self.assertEqual([r["ticker"] for r in capped], ["EDGE"])

    def test_volume_floor_treats_missing_volume_as_zero(self):
        rows = self.screen([market(ticker="THIN", volume_fp="499999"),
                            market(ticker="NONE", volume_fp=None),
                            market(ticker="OK", volume_fp="500000")])
        self.assertEqual([r["ticker"] for r in rows], ["OK"])
        self.assertEqual(len(self.screen([market(volume_fp=None)], "--min-volume", "0")), 1)

    def test_sorted_by_volume_and_top_n(self):
        markets = [market(ticker=t, volume_fp=v) for t, v in
                   [("B", "600000"), ("A", "900000"), ("C", "550000")]]
        self.assertEqual([r["ticker"] for r in self.screen(markets)], ["A", "B", "C"])
        self.assertEqual([r["ticker"] for r in self.screen(markets, "--top", "2")], ["A", "B"])

    def test_one_sided_row_has_no_spread_and_title_fallback(self):
        [row] = self.screen([market(title="", yes_sub_title="Owls by 3+",
                                    yes_bid_dollars=None, yes_ask_dollars="0.80", last_price_dollars="0.80")])
        self.assertIsNone(row["spread_c"])
        self.assertEqual(row["bid_c"], 0.0)
        self.assertEqual(row["title"], "Owls by 3+")


class ParseArgsTests(unittest.TestCase):
    def test_defaults(self):
        a = args()
        self.assertEqual((a.min_price, a.max_price, a.min_days, a.max_days, a.min_volume, a.top),
                         (0.70, 0.90, 30, None, 500000, None))
        self.assertEqual(a.categories, ["Sports", "Politics", "Elections"])

    def test_rejects_unknown_category(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            args("--categories", "Crypto")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_429(url):
    return urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO())


class FetchPaginatedTests(unittest.TestCase):
    def run_fetch(self, responses, path="/markets", params=None, **kw):
        requests = []
        for r in responses:  # HTTPError objects hold a file handle; close them when the test ends
            if isinstance(r, urllib.error.HTTPError):
                self.addCleanup(r.close)

        def fake_urlopen(req, timeout):
            requests.append(req)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return FakeResponse(json.dumps(r).encode())

        with mock.patch.object(ks.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(ks.time, "sleep") as sleep:
            pages = list(ks.fetch_paginated(path, params, **kw))
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query) for r in requests]
        return pages, requests, queries, sleep

    def test_follows_cursor_until_exhausted(self):
        pages, requests, queries, _ = self.run_fetch(
            [{"markets": [1], "cursor": "abc"}, {"markets": [2], "cursor": ""}],
            params={"status": "open"})
        self.assertEqual([p["markets"] for p in pages], [[1], [2]])
        self.assertNotIn("cursor", queries[0])
        self.assertEqual(queries[1]["cursor"], ["abc"])
        self.assertEqual(queries[0]["limit"], ["200"])
        self.assertEqual(queries[0]["status"], ["open"])
        self.assertTrue(requests[0].full_url.startswith(ks.BASE_URL + "/markets?"))
        self.assertEqual(requests[0].get_header("User-agent"), ks.USER_AGENT)

    def test_page_limit_stops_early(self):
        pages, _, _, _ = self.run_fetch([{"cursor": "1"}, {"cursor": "2"}, {"cursor": "3"}], page_limit=2)
        self.assertEqual(len(pages), 2)

    def test_retries_rate_limits_with_backoff(self):
        pages, requests, _, sleep = self.run_fetch(
            [http_429("u"), http_429("u"), {"events": ["ok"]}], path="/events")
        self.assertEqual(pages, [{"events": ["ok"]}])
        self.assertEqual(len(requests), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    def test_gives_up_after_six_rate_limits(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.run_fetch([http_429("u") for _ in range(6)])

    def test_other_http_errors_are_not_retried(self):
        err = urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO())
        responses = [err, {"never": "reached"}]
        with self.assertRaises(urllib.error.HTTPError):
            self.run_fetch(responses)
        self.assertEqual(len(responses), 1)


class LoaderTests(unittest.TestCase):
    def test_event_categories_keep_only_wanted(self):
        pages = [{"events": [{"event_ticker": "E1", "category": "Sports"},
                             {"event_ticker": "E2", "category": "Weather"}]},
                 {"events": [{"event_ticker": "E3", "category": "Elections"}]}]
        with mock.patch.object(ks, "fetch_paginated", return_value=iter(pages)) as fp:
            mapping = ks.load_event_categories(["Sports", "Elections"])
        self.assertEqual(mapping, {"E1": "Sports", "E3": "Elections"})
        fp.assert_called_once_with("/events", {"status": "open"})

    def test_markets_push_close_window_as_whole_seconds(self):
        with mock.patch.object(ks, "fetch_paginated",
                               return_value=iter([{"markets": [1, 2]}, {"markets": [3]}])) as fp:
            out = ks.load_markets(min_close_ts=1_780_000_000.9, max_close_ts=None)
        self.assertEqual(out, [1, 2, 3])
        fp.assert_called_once_with("/markets", {"status": "open", "limit": 1000, "min_close_ts": 1_780_000_000})


class MainTests(unittest.TestCase):
    def test_end_to_end_prints_table(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ks, "load_event_categories", return_value=CATS), \
                mock.patch.object(ks, "load_markets", return_value=[market(ticker="KXOWLS-WIN")]) as lm, \
                mock.patch.object(ks, "datetime", FrozenDatetime), redirect_stdout(out), redirect_stderr(err):
            code = ks.main(["--max-days", "90"])
        self.assertEqual(code, 0)
        self.assertEqual(lm.call_args.kwargs, {"min_close_ts": NOW.timestamp() + 30 * 86400,
                                               "max_close_ts": NOW.timestamp() + 90 * 86400})
        table = out.getvalue()
        self.assertIn("KXOWLS-WIN", table)
        self.assertIn("30-90 days to close", table)
        self.assertIn("1 market(s) passed the screen.", table)
        self.assertNotIn("Loading", table)  # progress goes to stderr only
        self.assertIn("Loading open markets", err.getvalue())

    def test_empty_result_message(self):
        out = io.StringIO()
        with mock.patch.object(ks, "load_event_categories", return_value={}), \
                mock.patch.object(ks, "load_markets", return_value=[market()]), \
                mock.patch.object(ks, "datetime", FrozenDatetime), redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(ks.main([]), 0)
        self.assertIn("No markets passed the screen", out.getvalue())
        self.assertIn(">30 days to close", out.getvalue())

    def test_network_failure_exits_nonzero(self):
        err = io.StringIO()
        with mock.patch.object(ks, "load_event_categories", side_effect=urllib.error.URLError("offline")), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(ks.main([]), 1)
        self.assertIn("Network error", err.getvalue())


if __name__ == "__main__":
    unittest.main()

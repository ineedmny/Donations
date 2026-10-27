from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import helpers

import collector
import init_strategy
import review
import trader
from market_profile import get_profile, is_exchange_holiday, market_session, normalize_symbol


class MarketProfileTests(unittest.TestCase):
    def test_profiles_have_expected_capital_and_rules(self) -> None:
        cn = get_profile("CN")
        us = get_profile("US")
        self.assertEqual(cn.initial_cash, 1_800_000.0)
        self.assertEqual(cn.lot_size, 100)
        self.assertTrue(cn.t_plus_one)
        self.assertEqual(us.initial_cash, 45_000.0)
        self.assertEqual(us.lot_size, 1)
        self.assertFalse(us.t_plus_one)
        self.assertEqual(us.currency, "USD")

    def test_provider_prefixed_us_ticker_is_normalized(self) -> None:
        self.assertEqual(normalize_symbol(get_profile("US"), "105.aapl"), "AAPL")
        self.assertEqual(normalize_symbol(get_profile("CN"), "sh600519"), "600519")

    def test_us_session_uses_new_york_dst(self) -> None:
        us = get_profile("US")
        summer = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
        winter = datetime(2026, 12, 1, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(market_session(us, summer)[0], "regular_trading")
        self.assertEqual(market_session(us, winter)[0], "regular_trading")
        self.assertEqual(
            market_session(us, datetime(2026, 12, 1, 13, 0, tzinfo=timezone.utc))[0],
            "closed",
        )

    def test_observed_independence_day_closes_us_market(self) -> None:
        self.assertTrue(is_exchange_holiday(get_profile("US"), date(2026, 7, 3)))
        self.assertFalse(is_exchange_holiday(get_profile("US"), date(2026, 7, 2)))

    def test_beijing_midnight_quote_maps_to_previous_us_trade_day(self) -> None:
        self.assertEqual(
            trader.quote_market_day("2026-10-24T02:00:00+08:00", get_profile("US")),
            "2026-10-23",
        )
        self.assertEqual(
            trader.quote_market_day("2026-10-24T22:00:00+08:00", get_profile("US")),
            "2026-10-24",
        )


class TradingRulesTests(unittest.TestCase):
    def _connection(self, market: str) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        collector.init_db(connection)
        trader.init_db(connection, profile=get_profile(market))
        return connection

    def test_us_account_starts_at_45000_dollars(self) -> None:
        connection = self._connection("US")
        try:
            account = trader.load_account(connection)
            self.assertEqual(float(account["principal"]), 45_000.0)
            self.assertEqual(float(account["cash"]), 45_000.0)
        finally:
            connection.close()

    def test_us_allows_one_share_and_same_day_sell(self) -> None:
        us = get_profile("US")
        self.assertEqual(trader.parse_quantity("1", profile=us), 1)
        quote = helpers.make_quote(trader, "AAPL", price=200.0, market="US")
        state = trader.PortfolioState(cash=45_000.0)
        decisions = [
            trader.Decision("BUY", "AAPL", 1, "test buy"),
            trader.Decision("SELL", "AAPL", 1, "test sell"),
        ]
        executions, rejects, _, holdings = trader.simulate(
            decisions, {"AAPL": quote}, state, {}, profile=us
        )
        self.assertEqual(rejects, [])
        self.assertEqual([item.action for item in executions], ["BUY", "SELL"])
        self.assertNotIn("AAPL", holdings)

    def test_cn_still_rejects_same_day_sell_and_non_lot_quantity(self) -> None:
        cn = get_profile("CN")
        with self.assertRaises(Exception):
            trader.parse_quantity("1", profile=cn)
        quote = helpers.make_quote(trader, "600519", price=100.0)
        state = trader.PortfolioState(cash=1_800_000.0)
        decisions = [
            trader.Decision("BUY", "600519", 100, "test buy"),
            trader.Decision("SELL", "600519", 100, "test sell"),
        ]
        executions, rejects, _, holdings = trader.simulate(
            decisions, {"600519": quote}, state, {}, profile=cn
        )
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].action, "BUY")
        self.assertTrue(any("T+1" in item for item in rejects))
        self.assertEqual(holdings["600519"].quantity, 100)

    def test_fee_profiles_differ(self) -> None:
        self.assertGreater(trader.calculate_fee("BUY", 10_000.0, profile=get_profile("CN")), 0)
        self.assertEqual(trader.calculate_fee("BUY", 10_000.0, profile=get_profile("US")), 0.0)

    def test_apply_state_writes_active_market(self) -> None:
        us = get_profile("US")
        connection = self._connection("US")
        try:
            quote = helpers.make_quote(trader, "MSFT", price=300.0, market="US")
            holdings = {"MSFT": trader.Holding("MSFT", "MSFT", 1, 300.0, 300.0)}
            trader.apply_state(
                connection,
                executions=[],
                cash=44_700.0,
                holdings=holdings,
                quotes={"MSFT": quote},
                day="2026-10-23",
                timestamp="2026-10-24T06:00:00+08:00",
                profile=us,
            )
            row = connection.execute("SELECT market, symbol FROM positions").fetchone()
            self.assertEqual((row["market"], row["symbol"]), ("US", "MSFT"))
        finally:
            connection.close()

    def test_daily_bar_cache_prunes_weekends_and_us_holidays(self) -> None:
        connection = self._connection("US")
        try:
            connection.executemany(
                "INSERT INTO daily_closes (market, symbol, trade_date, close) "
                "VALUES (?, ?, ?, ?)",
                [
                    ("US", "AAPL", "2026-10-22", 100.0),
                    ("US", "AAPL", "2026-10-23", 101.0),
                    ("US", "AAPL", "2026-10-24", 101.0),
                    ("US", "AAPL", "2026-10-25", 101.0),
                    ("US", "AAPL", "2026-12-25", 102.0),
                ],
            )
            connection.commit()

            trader._prune_invalid_daily_bars(connection, get_profile("US"))

            remaining = [
                str(row[0])
                for row in connection.execute(
                    "SELECT trade_date FROM daily_closes "
                    "WHERE market = 'US' ORDER BY trade_date"
                ).fetchall()
            ]
            self.assertEqual(remaining, ["2026-10-22", "2026-10-23"])
        finally:
            connection.close()


class PromptTests(unittest.TestCase):
    def test_us_prompt_has_us_rules_and_indices(self) -> None:
        prompt = trader.build_system_prompt(get_profile("US"))
        self.assertIn("T+0", prompt)
        self.assertIn("1 股", prompt)
        self.assertIn("标普500", prompt)
        self.assertIn("美股", prompt)

    def test_cn_prompt_keeps_a_share_rules(self) -> None:
        prompt = trader.build_system_prompt(get_profile("CN"))
        self.assertIn("T+1", prompt)
        self.assertIn("100 股", prompt)
        self.assertIn("上证指数", prompt)

    def test_trade_prompt_does_not_repeat_latest_note(self) -> None:
        quote = helpers.make_quote(trader, "600519")
        params = {
            "max_single_position": 1.0,
            "stop_loss_pct": -0.9,
            "custom_strategy_notes": "最新笔记只应出现在历史笔记中",
            "watchlist": [{"symbol": "600519", "reason": "观察"}],
        }
        notes = [
            {
                "trade_date": "2026-10-25",
                "notes": "历史笔记内容",
                "source": "daily_review",
                "updated_at": "2026-10-25T15:10:00+08:00",
            }
        ]

        prompt = trader.build_prompt(
            trader.PortfolioState(cash=1_800_000.0),
            [quote],
            {quote.symbol: quote},
            "2026-10-26",
            params,
            strategy_notes=notes,
            profile=get_profile("CN"),
        )
        snapshot = json.loads(prompt.split("\n", 1)[1])

        self.assertNotIn("yesterday_experience", snapshot)
        self.assertNotIn("custom_strategy_notes", snapshot["strategy_params"])
        self.assertNotIn("watchlist", snapshot["strategy_params"])
        self.assertEqual(snapshot["strategy_notes_history"], notes)
        self.assertEqual(prompt.count("历史笔记内容"), 1)

    def test_us_init_and_review_prompts_use_us_rules(self) -> None:
        us = get_profile("US")
        init_prompt = init_strategy.build_init_system_prompt(us)
        review_prompt = review.build_review_system_prompt(us)
        for expected in ("美股", "T+0", "1 股", "USD", "标普500"):
            self.assertIn(expected, init_prompt)
        for expected in ("美股", "标普500", "纳斯达克综合", "道琼斯工业"):
            self.assertIn(expected, review_prompt)


class InitializationTests(unittest.TestCase):
    def test_init_db_cli_initializes_us_account_without_quotes(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = Path(temp_dir.name) / "us_quotes.db"

        result = trader.main(
            ["--market", "US", "--db", str(db_path), "--init-db"]
        )

        self.assertEqual(result, 0)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            account = trader.load_account(connection)
            self.assertEqual(float(account["principal"]), 45_000.0)
            self.assertEqual(float(account["cash"]), 45_000.0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_us_watchlist_accepts_provider_style_lowercase_codes(self) -> None:
        us = get_profile("US")
        allowed = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA"}
        payload = {
            "updates": {},
            "new_params": {},
            "custom_strategy_notes": "标普500上涨32.45点，股票池20涨8跌，保持进攻",
            "strategy_changes": [],
            "watchlist": [
                {"symbol": "105.aapl", "reason": "观察动量"},
                {"symbol": "msft", "reason": "观察趋势"},
                {"symbol": "NVDA", "reason": "观察成交额"},
            ],
        }

        _updates, _new_params, _notes, watchlist, _changes = (
            review.parse_review_result(
                payload,
                allowed_watchlist=allowed,
                profile=us,
            )
        )

        self.assertEqual(
            [item["symbol"] for item in watchlist],
            ["AAPL", "MSFT", "NVDA"],
        )
        self.assertEqual(watchlist[0]["name"], "Apple")

    def test_long_review_notes_are_preserved_without_truncation(self) -> None:
        allowed = {"600900": "长江电力", "600519": "贵州茅台", "300750": "宁德时代"}
        payload = {
            "updates": {},
            "new_params": {},
            "custom_strategy_notes": (
                "上证涨1.00点、股票池20涨10跌，保持进攻；"
                + "优先逆势强势龙头并控制单股集中度，分散到多只强动量标的。" * 3
            ),
            "strategy_changes": [],
            "watchlist": [
                {"symbol": "600900", "reason": "观察趋势"},
                {"symbol": "600519", "reason": "观察动量"},
                {"symbol": "300750", "reason": "观察反弹"},
            ],
        }

        expected_notes = payload["custom_strategy_notes"].strip()
        _updates, _new_params, notes, _watchlist, _changes = (
            review.parse_review_result(
                payload,
                allowed_watchlist=allowed,
                profile=get_profile("CN"),
            )
        )

        self.assertGreater(len(notes), 100)
        self.assertEqual(notes, expected_notes)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import helpers

import account_store
import collector
import review
import trader
from market_profile import get_profile


def _insert_cn_quote(
    connection: sqlite3.Connection,
    symbol: str,
    *,
    price: float,
    amount: float,
    quoted_at: str,
) -> None:
    helpers.insert_quote(
        connection,
        (
            "CN",
            symbol,
            symbol,
            price,
            0.1,
            1.0,
            price,
            price,
            price,
            price,
            100_000,
            amount,
            2.0,
            2.0,
            3.0,
            4.0,
            5.0,
            quoted_at,
        ),
    )


class AccountSchemaTests(unittest.TestCase):
    def _legacy_cn_db(self) -> tuple[sqlite3.Connection, Path]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "quotes.db"
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        collector.init_db(connection)
        connection.executescript(
            """
            CREATE TABLE account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                principal REAL,
                initial_cash REAL NOT NULL,
                cash REAL NOT NULL,
                market_value REAL NOT NULL DEFAULT 0,
                total_asset REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE positions (
                market TEXT NOT NULL DEFAULT 'CN',
                symbol TEXT NOT NULL,
                name TEXT,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                avg_cost REAL NOT NULL CHECK (avg_cost >= 0),
                last_price REAL,
                market_value REAL,
                position_pct REAL,
                account_pct REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (market, symbol)
            );
            """
        )
        connection.execute(
            "INSERT INTO account VALUES (1, ?, ?, ?, ?, ?, ?)",
            (1_800_000.0, 1_800_000.0, 1_234.56, 10_000.0, 11_234.56, "2026-10-27T10:00:00+08:00"),
        )
        connection.execute(
            "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "CN",
                "600519",
                "贵州茅台",
                100,
                1_000.0,
                1_100.0,
                110_000.0,
                90.0,
                90.0,
                "2026-10-27T10:00:00+08:00",
            ),
        )
        connection.commit()
        return connection, path

    def test_migration_preserves_default_account_and_adds_small_account(self) -> None:
        connection, _path = self._legacy_cn_db()
        try:
            trader.init_db(connection, profile=get_profile("CN"))

            accounts = connection.execute(
                "SELECT * FROM accounts WHERE market = 'CN' ORDER BY id"
            ).fetchall()
            self.assertEqual([row["slug"] for row in accounts], ["main", "small-3000"])
            main, small = accounts
            self.assertEqual(float(main["principal"]), 1_800_000.0)
            self.assertEqual(float(main["cash"]), 1_234.56)
            self.assertEqual(float(small["principal"]), 3_000.0)
            self.assertEqual(float(small["cash"]), 3_000.0)
            self.assertEqual(small["pool_mode"], "affordable")
            self.assertEqual(float(small["pool_min_turnover"]), 200_000_000.0)

            position = connection.execute(
                "SELECT account_id, symbol FROM positions"
            ).fetchone()
            self.assertEqual(int(position["account_id"]), int(main["id"]))
            self.assertEqual(str(position["symbol"]), "600519")

            legacy = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account'"
            ).fetchone()
            self.assertIsNone(legacy)
        finally:
            connection.close()


class AccountIsolationTests(unittest.TestCase):
    def test_business_tables_are_isolated_by_account(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            collector.init_db(connection)
            trader.init_db(connection, profile=get_profile("CN"))
            accounts = connection.execute(
                "SELECT id, slug FROM accounts WHERE market = 'CN' ORDER BY id"
            ).fetchall()
            main_id, small_id = [int(row["id"]) for row in accounts]

            for account_id, stop_loss in ((main_id, -0.80), (small_id, -0.90)):
                trader.save_strategy_params(
                    connection,
                    {"stop_loss_pct": stop_loss},
                    profile=get_profile("CN"),
                    account_id=account_id,
                )
                trader.save_strategy_note(
                    connection,
                    "2026-10-27",
                    f"账户 {account_id} 的独立笔记",
                    profile=get_profile("CN"),
                    account_id=account_id,
                )
                trader.apply_strategy_changes(
                    connection,
                    [
                        {
                            "action": "create",
                            "name": "小额动量",
                            "description": "验证不同账户可以拥有同名策略",
                            "rules": {"entry": ["放量"], "exit": ["止损"]},
                        }
                    ],
                    account_id=account_id,
                )
                connection.execute(
                    "INSERT INTO positions ("
                    "account_id, market, symbol, name, quantity, avg_cost, "
                    "last_price, market_value, updated_at"
                    ") VALUES (?, 'CN', ?, ?, 100, 10, 10, 1000, ?)",
                    (account_id, f"60000{account_id}", f"持仓{account_id}", "2026-10-27T10:00:00+08:00"),
                )
                connection.execute(
                    "INSERT INTO trades ("
                    "account_id, traded_at, trade_date, market, symbol, name, side, "
                    "price, quantity, amount, fee, cash_after, reason, created_at"
                    ") VALUES (?, ?, '2026-10-27', 'CN', ?, ?, 'BUY', 10, 100, "
                    "1000, 5, 1000, ?, ?)",
                    (
                        account_id,
                        "2026-10-27T10:00:00+08:00",
                        f"60000{account_id}",
                        f"成交{account_id}",
                        f"原因{account_id}",
                        "2026-10-27T10:00:00+08:00",
                    ),
                )
            connection.commit()

            main_params = trader.load_strategy_params(connection, account_id=main_id)
            small_params = trader.load_strategy_params(connection, account_id=small_id)
            self.assertEqual(main_params["stop_loss_pct"], -0.80)
            self.assertEqual(small_params["stop_loss_pct"], -0.90)

            self.assertEqual(
                [row["symbol"] for row in trader.load_positions(connection, account_id=main_id)],
                [f"60000{main_id}"],
            )
            self.assertEqual(
                [row["symbol"] for row in trader.load_positions(connection, account_id=small_id)],
                [f"60000{small_id}"],
            )
            self.assertEqual(
                trader.load_today_trade_count(
                    connection, "2026-10-27", account_id=small_id
                ),
                1,
            )
            self.assertEqual(
                trader.load_strategy_notes(connection, account_id=small_id)[0]["notes"],
                f"账户 {small_id} 的独立笔记",
            )
            self.assertEqual(
                [row["name"] for row in trader.load_strategies(connection, account_id=main_id)],
                ["激进龙头动量", "小额动量"],
            )
            self.assertEqual(
                [row["name"] for row in trader.load_strategies(connection, account_id=small_id)],
                ["激进龙头动量", "小额动量"],
            )
        finally:
            connection.close()


class AccountWorkflowTests(unittest.TestCase):
    def _make_accounts_db(self) -> tuple[sqlite3.Connection, int, int]:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        collector.init_db(connection)
        trader.init_db(connection, profile=get_profile("CN"))
        rows = connection.execute(
            "SELECT id FROM accounts WHERE market = 'CN' ORDER BY id"
        ).fetchall()
        return connection, int(rows[0]["id"]), int(rows[1]["id"])

    def test_affordable_pool_rejects_quotes_newer_than_pool(self) -> None:
        connection, _main_id, small_id = self._make_accounts_db()
        profile = get_profile("CN")
        try:
            _insert_cn_quote(
                connection,
                "600001",
                price=10.0,
                amount=300_000_000.0,
                quoted_at="2026-10-27T10:05:00+08:00",
            )
            account_store.replace_stock_pool(
                connection,
                small_id,
                [
                    {
                        "symbol": "600001",
                        "name": "低价活跃股",
                        "price": 10.0,
                        "amount": 300_000_000.0,
                        "lot_cost": 1005.03,
                        "reason": "测试",
                    }
                ],
                refreshed_at="2026-10-27T10:00:00+08:00",
            )

            with self.assertRaisesRegex(
                trader.TraderError, "股票池未随最新行情刷新"
            ):
                trader.load_latest_quotes(
                    connection, 30, profile, account_id=small_id
                )
        finally:
            connection.close()

    def test_daily_review_filters_trades_chat_and_candidates_by_account(self) -> None:
        connection, main_id, small_id = self._make_accounts_db()
        profile = get_profile("CN")
        day = "2026-10-27"
        quoted_at = "2026-10-27T15:00:00+08:00"
        main_symbol = profile.stock_pool[0]
        small_symbol = "600001"
        try:
            _insert_cn_quote(
                connection,
                main_symbol,
                price=10.0,
                amount=1_000_000_000.0,
                quoted_at=quoted_at,
            )
            _insert_cn_quote(
                connection,
                small_symbol,
                price=10.0,
                amount=300_000_000.0,
                quoted_at=quoted_at,
            )
            account_store.replace_stock_pool(
                connection,
                small_id,
                [
                    {
                        "symbol": small_symbol,
                        "name": "小额成交",
                        "price": 10.0,
                        "amount": 300_000_000.0,
                        "lot_cost": 1005.03,
                        "reason": "测试",
                    }
                ],
                refreshed_at=quoted_at,
            )
            for account_id, symbol, name in (
                (main_id, main_symbol, "主账户持仓"),
                (small_id, small_symbol, "小额账户持仓"),
            ):
                connection.execute(
                    "INSERT INTO positions ("
                    "account_id, market, symbol, name, quantity, avg_cost, "
                    "last_price, market_value, updated_at"
                    ") VALUES (?, 'CN', ?, ?, 100, 10, 10, 1000, ?)",
                    (account_id, symbol, name, quoted_at),
                )
                connection.execute(
                    trader.INSERT_TRADE_SQL,
                    (
                        account_id,
                        quoted_at,
                        day,
                        "CN",
                        symbol,
                        name,
                        "BUY",
                        10.0,
                        100,
                        1000.0,
                        5.0,
                        1000.0,
                        f"{name}成交原因",
                        None,
                        quoted_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO chat_messages ("
                    "account_id, created_at, trade_date, role, content"
                    ") VALUES (?, ?, ?, 'user', ?)",
                    (account_id, quoted_at, day, f"{name}聊天"),
                )
            connection.commit()

            data = review.build_daily_review(
                connection, day, profile, account_id=small_id
            )

            self.assertEqual(
                [item["symbol"] for item in data["trades"]], [small_symbol]
            )
            self.assertEqual(
                [item["symbol"] for item in data["watchlist_candidates"]],
                [small_symbol],
            )
            self.assertEqual(
                [item["content"] for item in data["chat_records"]["messages"]],
                ["小额账户持仓聊天"],
            )
            self.assertEqual(
                [item["symbol"] for item in data["symbols"]], [small_symbol]
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

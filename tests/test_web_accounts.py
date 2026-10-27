from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

import account_store
import collector
import trader
import web
from market_profile import get_profile


class WebAccountTests(unittest.TestCase):
    def _make_db(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "quotes.db"
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            collector.init_db(connection)
            trader.init_db(connection, profile=get_profile("CN"))
            accounts = connection.execute(
                "SELECT * FROM accounts WHERE market = 'CN' ORDER BY id"
            ).fetchall()
            main_id = int(accounts[0]["id"])
            small_id = int(accounts[1]["id"])
            helpers.insert_quote(
                connection,
                (
                    "CN",
                    "600001",
                    "低价活跃股",
                    10.0,
                    0.1,
                    1.0,
                    9.9,
                    10.1,
                    9.8,
                    9.9,
                    100000,
                    300_000_000.0,
                    2.0,
                    2.0,
                    3.0,
                    4.0,
                    5.0,
                    "2026-10-27T10:00:00+08:00",
                ),
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
                        "reason": "成交额 3.00 亿元 · 一手 1005.03 元",
                    }
                ],
                refreshed_at="2026-10-27T10:00:00+08:00",
            )
            for account_id, symbol, name in (
                (main_id, "600519", "主账户持仓"),
                (small_id, "600001", "小额账户持仓"),
            ):
                connection.execute(
                    "INSERT INTO positions ("
                    "account_id, market, symbol, name, quantity, avg_cost, "
                    "last_price, market_value, updated_at"
                    ") VALUES (?, 'CN', ?, ?, 100, 10, 10, 1000, ?)",
                    (account_id, symbol, name, "2026-10-27T10:00:00+08:00"),
                )
            connection.commit()
        finally:
            connection.close()
        return path

    def test_account_dashboard_and_pool_are_isolated(self) -> None:
        path = self._make_db()
        with mock.patch.dict(web.DB_PATHS, {"CN": path}):
            main_response = web.dashboard_api()
            small_response = web.account_dashboard_api("small-3000")
            small_page = web.account_dashboard("small-3000")

        main_data = json.loads(main_response.body)
        small_data = json.loads(small_response.body)
        self.assertEqual(main_data["principal"], 1_800_000.0)
        self.assertEqual(small_data["principal"], 3_000.0)
        self.assertEqual(
            [item["name"] for item in main_data["holdings"]],
            ["主账户持仓"],
        )
        self.assertEqual(
            [item["name"] for item in small_data["holdings"]],
            ["小额账户持仓"],
        )
        self.assertEqual(small_data["stock_pool"][0]["symbol"], "600001")

        html = small_page.body.decode("utf-8")
        self.assertIn("3,000 元小额账户", html)
        self.assertIn("A股主账户", html)
        self.assertIn('value="/accounts/small-3000"', html)
        self.assertNotIn("可买股票池", html)
        self.assertNotIn("低价活跃股", html)
        self.assertNotIn('id="stock-pool-list"', html)

    def test_account_chat_page_uses_scoped_api_base(self) -> None:
        path = self._make_db()
        with mock.patch.dict(web.DB_PATHS, {"CN": path}):
            response = web.account_chat_page("small-3000")
        html = response.body.decode("utf-8")
        self.assertIn('const API_BASE = "/accounts/small-3000";', html)
        self.assertIn('const DASHBOARD_PATH = "/accounts/small-3000";', html)
        self.assertIn("3,000 元小额账户", html)

    def test_chat_history_is_isolated_by_account(self) -> None:
        path = self._make_db()
        web.save_chat_message("user", "主账户聊天", market="CN", db_path=path)
        web.save_chat_message(
            "user",
            "小额账户聊天",
            market="CN",
            db_path=path,
            account_slug="small-3000",
        )

        with mock.patch.dict(web.DB_PATHS, {"CN": path}):
            main_data = json.loads(web.chat_history().body)
            small_data = json.loads(
                web.account_chat_history("small-3000").body
            )

        self.assertEqual(
            [item["content"] for item in main_data["messages"]],
            ["主账户聊天"],
        )
        self.assertEqual(
            [item["content"] for item in small_data["messages"]],
            ["小额账户聊天"],
        )


if __name__ == "__main__":
    unittest.main()

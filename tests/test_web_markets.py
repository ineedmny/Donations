from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

import collector
import trader
import web
from market_profile import get_profile


class WebMarketTests(unittest.TestCase):
    def _make_db(self, market: str) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "quotes.db"
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            collector.init_db(connection)
            trader.init_db(connection, profile=get_profile(market))
            symbol = "AAPL" if market == "US" else "600519"
            helpers.insert_quote(
                connection,
                (
                    market,
                    symbol,
                    "Apple" if market == "US" else "贵州茅台",
                    200.0,
                    1.0,
                    0.5,
                    199.0,
                    201.0,
                    198.0,
                    199.0,
                    1000,
                    200000.0,
                    1.5,
                    None,
                    None,
                    None,
                    None,
                    "2026-10-24T06:00:00+08:00",
                ),
            )
        finally:
            connection.close()
        return path

    def test_us_dashboard_uses_usdb_capital_and_currency(self) -> None:
        data = web.load_dashboard_data("US", self._make_db("US"))
        self.assertEqual(data["principal"], 45_000.0)
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["market"], "US")
        html = web.render_dashboard(data, "US")
        self.assertIn("美股 AI 交易看板", html)
        self.assertIn("$45,000.00", html)
        self.assertIn('href="/"', html)

    def test_cn_dashboard_links_to_us(self) -> None:
        data = web.load_dashboard_data("CN", self._make_db("CN"))
        html = web.render_dashboard(data, "CN")
        self.assertIn("A股 AI 交易看板", html)
        self.assertIn('href="/us"', html)

    def test_us_chat_page_uses_us_api_and_return_paths(self) -> None:
        html = web.render_chat_page("US")
        self.assertIn("美股 AI 交易问答", html)
        self.assertIn('const API_BASE = "/us";', html)
        self.assertIn('const DASHBOARD_PATH = "/us";', html)
        self.assertIn('href="/us"', html)
        self.assertIn("切换到 A 股", html)

    def test_dashboard_apis_are_isolated_by_market(self) -> None:
        cn_path = self._make_db("CN")
        us_path = self._make_db("US")
        with mock.patch.dict(
            web.DB_PATHS,
            {"CN": cn_path, "US": us_path},
        ):
            cn_response = web.dashboard_api()
            us_response = web.us_dashboard_api()
            us_page = web.us_dashboard()

        self.assertEqual(cn_response.status_code, 200)
        self.assertEqual(us_response.status_code, 200)
        self.assertEqual(us_page.status_code, 200)
        self.assertEqual(json.loads(cn_response.body)["principal"], 1_800_000.0)
        self.assertEqual(json.loads(us_response.body)["principal"], 45_000.0)
        self.assertEqual(json.loads(us_response.body)["currency"], "USD")
        page_html = us_page.body.decode("utf-8")
        self.assertIn("$45,000.00", page_html)
        self.assertIn('href="/"', page_html)


if __name__ == "__main__":
    unittest.main()

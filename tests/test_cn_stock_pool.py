from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import helpers

import account_store
import collector
import trader
from market_profile import get_profile


class CnStockPoolTests(unittest.TestCase):
    def _quote_tuple(
        self, symbol: str, quoted_at: str, *, price: float = 10.0
    ) -> tuple:
        return (
            "CN",
            symbol,
            symbol,
            price,
            0.1,
            1.0,
            price,
            price,
            price,
            round(price - 0.1, 4),
            10_000,
            1_000_000,
            2.0,
            1.0,
            2.0,
            3.0,
            4.0,
            quoted_at,
        )

    def _init_accounts(self) -> tuple[sqlite3.Connection, int, int]:
        profile = get_profile("CN")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        collector.init_db(connection)
        trader.init_db(connection, profile=profile)
        accounts = {
            str(row["slug"]): int(row["id"])
            for row in connection.execute(
                "SELECT id, slug FROM accounts WHERE market = 'CN'"
            )
        }
        return connection, accounts["main"], accounts["small-3000"]

    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "code": "sh600001",
                    "name": "边界成交额",
                    "zxj": "10",
                    "turnover": "20000",
                    "volume": "10000",
                    "zd": "0.1",
                    "zdf": "1.0",
                    "zdf_d5": "2",
                    "zdf_d10": "3",
                    "zdf_d20": "4",
                    "zdf_d60": "5",
                    "zf": "2",
                },
                {
                    "code": "sh600002",
                    "name": "成交额不足",
                    "zxj": "10",
                    "turnover": "19999",
                    "volume": "10000",
                    "zd": "0.1",
                    "zdf": "1.0",
                    "zdf_d5": "2",
                    "zdf_d10": "3",
                    "zdf_d20": "4",
                    "zdf_d60": "5",
                    "zf": "2",
                },
                {
                    "code": "sz000001",
                    "name": "一手可买",
                    "zxj": "29.90",
                    "turnover": "50000",
                    "volume": "10000",
                    "zd": "0.1",
                    "zdf": "1.0",
                    "zdf_d5": "2",
                    "zdf_d10": "3",
                    "zdf_d20": "4",
                    "zdf_d60": "5",
                    "zf": "2",
                },
                {
                    "code": "sz000002",
                    "name": "一手超出",
                    "zxj": "30.00",
                    "turnover": "50000",
                    "volume": "10000",
                    "zd": "0.1",
                    "zdf": "1.0",
                    "zdf_d5": "2",
                    "zdf_d10": "3",
                    "zdf_d20": "4",
                    "zdf_d60": "5",
                    "zf": "2",
                },
                {
                    "code": "sz000003",
                    "name": "*ST风险",
                    "zxj": "5",
                    "turnover": "50000",
                    "volume": "10000",
                    "zd": "0.1",
                    "zdf": "1.0",
                    "zdf_d5": "2",
                    "zdf_d10": "3",
                    "zdf_d20": "4",
                    "zdf_d60": "5",
                    "zf": "2",
                },
                {
                    "code": "sh688001",
                    "name": "科创板",
                    "zxj": "5",
                    "turnover": "50000",
                    "volume": "10000",
                    "zd": "0.1",
                    "zdf": "1.0",
                    "zdf_d5": "2",
                    "zdf_d10": "3",
                    "zdf_d20": "4",
                    "zdf_d60": "5",
                    "zf": "2",
                },
                {
                    "code": "bj830001",
                    "name": "北交所",
                    "zxj": "5",
                    "turnover": "50000",
                    "volume": "10000",
                    "zd": "0.1",
                    "zdf": "1.0",
                    "zdf_d5": "2",
                    "zdf_d10": "3",
                    "zdf_d20": "4",
                    "zdf_d60": "5",
                    "zf": "2",
                },
            ]
        )

    def test_full_market_rows_convert_turnover_to_yuan(self) -> None:
        rows = collector.parse_cn_full_market_frame(
            self._frame(), "2026-10-27T10:00:00+08:00"
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertAlmostEqual(by_symbol["600001"]["amount"], 200_000_000.0)
        self.assertAlmostEqual(by_symbol["600001"]["change_pct_5d"], 2.0)

    def test_pool_uses_amount_and_lot_cost_boundaries(self) -> None:
        rows = collector.parse_cn_full_market_frame(
            self._frame(), "2026-10-27T10:00:00+08:00"
        )
        selected = collector.select_cn_affordable_pool(
            rows,
            principal=3_000.0,
            min_turnover=200_000_000.0,
            lot_size=100,
            limit=30,
            profile=get_profile("CN"),
        )
        self.assertEqual(
            [item["symbol"] for item in selected],
            ["000001", "600001"],
        )
        self.assertLessEqual(selected[0]["lot_cost"], 3_000.0)

    def test_dynamic_pool_refresh_respects_limit_and_timestamp(self) -> None:
        quoted_at = "2026-10-27T10:00:00+08:00"
        profile = get_profile("CN")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            collector.init_db(connection)
            trader.init_db(connection, profile=profile)
            small_id = int(
                connection.execute(
                    "SELECT id FROM accounts WHERE market = 'CN' AND slug = 'small-3000'"
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE accounts SET pool_limit = 1 WHERE id = ?", (small_id,)
            )
            rows = collector.parse_cn_full_market_frame(self._frame(), quoted_at)
            connection.executemany(
                collector.INSERT_QUOTE_SQL,
                [collector._full_market_quote_tuple(row) for row in rows],
            )
            connection.commit()

            counts = account_store.refresh_dynamic_stock_pools(
                connection, profile, quoted_at
            )
            pool = account_store.load_stock_pool(connection, small_id)

            self.assertEqual(counts[small_id], 1)
            self.assertEqual([row["symbol"] for row in pool], ["000001"])
            self.assertEqual(str(pool[0]["refreshed_at"]), quoted_at)
        finally:
            connection.close()

    def test_selected_symbols_include_fixed_pool_dynamic_pool_and_positions(
        self,
    ) -> None:
        connection, main_id, small_id = self._init_accounts()
        try:
            account_store.replace_stock_pool(
                connection,
                small_id,
                [{"symbol": "000001", "name": "动态池样本"}],
                refreshed_at="2026-10-27T10:00:00+08:00",
            )
            connection.execute(
                "INSERT INTO positions ("
                "account_id, market, symbol, name, quantity, avg_cost, "
                "last_price, market_value, updated_at"
                ") VALUES (?, 'CN', '000002', '持仓样本', 100, 10, 10, 1000, ?)",
                (main_id, "2026-10-27T10:00:00+08:00"),
            )
            connection.commit()

            symbols = collector.load_cn_selected_symbols(
                connection, get_profile("CN")
            )

            self.assertEqual(
                set(symbols),
                set(get_profile("CN").stock_pool) | {"000001", "000002"},
            )
        finally:
            connection.close()

    def test_selected_only_refreshes_union_without_full_market_scan(self) -> None:
        connection, _, small_id = self._init_accounts()
        quoted_at = "2026-10-27T10:10:00+08:00"
        try:
            account_store.replace_stock_pool(
                connection,
                small_id,
                [{"symbol": "000001"}, {"symbol": "000002"}],
                refreshed_at="2026-10-27T10:00:00+08:00",
            )
            requested: list[str] = []

            def fake_selected(symbols, timestamp):
                requested.extend(symbols)
                return [self._quote_tuple(symbol, timestamp) for symbol in symbols]

            with (
                mock.patch.object(
                    collector, "_fetch_cn_symbol_rows", side_effect=fake_selected
                ),
                mock.patch.object(
                    collector, "_fetch_cn_full_market_rows"
                ) as full_market,
                mock.patch.object(collector, "_fetch_cn_pool_rows") as fixed_detail,
                mock.patch.object(
                    collector, "_fetch_cn_index_rows", return_value=[]
                ),
            ):
                collected = collector.collect_market(
                    connection,
                    "CN",
                    "A股",
                    (),
                    quoted_at,
                    selected_only=True,
                )

            self.assertTrue(collected)
            full_market.assert_not_called()
            fixed_detail.assert_not_called()
            self.assertEqual(
                set(requested),
                set(get_profile("CN").stock_pool) | {"000001", "000002"},
            )
            pool = account_store.load_stock_pool(connection, small_id)
            self.assertTrue(pool)
            self.assertEqual(
                {str(row["refreshed_at"]) for row in pool}, {quoted_at}
            )
        finally:
            connection.close()

    def test_partial_selected_refresh_keeps_dynamic_pool_timestamp_stale(
        self,
    ) -> None:
        connection, _, small_id = self._init_accounts()
        old_timestamp = "2026-10-27T10:00:00+08:00"
        quoted_at = "2026-10-27T10:10:00+08:00"
        try:
            account_store.replace_stock_pool(
                connection,
                small_id,
                [{"symbol": "000001"}, {"symbol": "000002"}],
                refreshed_at=old_timestamp,
            )

            def missing_symbol(symbols, timestamp):
                selected = [symbol for symbol in symbols if symbol != "000002"]
                return [
                    self._quote_tuple(symbol, timestamp) for symbol in selected
                ]

            with (
                mock.patch.object(
                    collector, "_fetch_cn_symbol_rows", side_effect=missing_symbol
                ),
                mock.patch.object(
                    collector, "_fetch_cn_index_rows", return_value=[]
                ),
            ):
                collector.refresh_cn_selected_quotes(
                    connection, get_profile("CN"), quoted_at
                )

            pool = account_store.load_stock_pool(connection, small_id)
            self.assertEqual(
                {str(row["refreshed_at"]) for row in pool}, {old_timestamp}
            )
        finally:
            connection.close()

    def test_full_market_collection_still_rebuilds_dynamic_pool(self) -> None:
        connection, _, _ = self._init_accounts()
        quoted_at = "2026-10-27T10:05:00+08:00"
        try:
            with (
                mock.patch.object(
                    collector,
                    "_fetch_cn_full_market_rows",
                    return_value=[self._quote_tuple("000001", quoted_at)],
                ) as full_market,
                mock.patch.object(
                    collector, "_fetch_cn_pool_rows", return_value=[]
                ) as fixed_detail,
                mock.patch.object(
                    account_store, "refresh_dynamic_stock_pools", return_value={}
                ) as rebuild,
                mock.patch.object(
                    collector, "_fetch_cn_index_rows", return_value=[]
                ),
            ):
                collected = collector.collect_market(
                    connection, "CN", "A股", (), quoted_at
                )

            self.assertTrue(collected)
            full_market.assert_called_once_with(quoted_at)
            fixed_detail.assert_called_once_with(quoted_at)
            rebuild.assert_called_once()
        finally:
            connection.close()

    def test_trader_refresh_uses_selected_only_collector(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("trader.subprocess.run", return_value=completed) as run:
            refreshed = trader.refresh_quotes(
                Path("/tmp/selected-only-test.db"), get_profile("CN")
            )

        self.assertTrue(refreshed)
        command = run.call_args.args[0]
        self.assertIn("--selected-only", command)


if __name__ == "__main__":
    unittest.main()

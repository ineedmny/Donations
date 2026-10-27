from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace

import pandas as pd

import helpers
import collector

from collector import (
    _parse_tencent_us_indices,
    _parse_tencent_us_pool,
    normalize_frame,
)
from market_profile import get_profile


class UsCollectorNormalizationTests(unittest.TestCase):
    def test_provider_code_and_fields_are_normalized(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "代码": "105.AAPL",
                    "名称": "苹果",
                    "最新价": 231.5,
                    "涨跌额": 1.2,
                    "涨跌幅": 0.52,
                    "开盘价": 230.0,
                    "最高价": 232.0,
                    "最低价": 229.5,
                    "昨收价": 230.3,
                    "成交量": 1000,
                    "成交额": 231500.0,
                    "振幅": 1.09,
                }
            ]
        )
        rows = normalize_frame(frame, "US", "2026-10-24T06:00:00+08:00")
        self.assertEqual(rows[0][0], "US")
        self.assertEqual(rows[0][1], "AAPL")
        self.assertEqual(rows[0][2], "苹果")
        self.assertEqual(rows[0][3], 231.5)

    def test_tencent_us_pool_fields_are_parsed(self) -> None:
        values = ["0"] * 72
        values[1] = "Apple"
        values[2] = "AAPL.OQ"
        values[3] = "347.73"
        values[4] = "348.22"
        values[5] = "349.12"
        values[6] = "39539690"
        values[31] = "-0.49"
        values[32] = "-0.14"
        values[33] = "351.73"
        values[34] = "345.75"
        values[30] = "2026-10-23 16:00:01"
        values[37] = "13760827472"
        values[43] = "1.72"
        text = 'v_usAAPL="' + "~".join(values) + '";'
        profile = replace(get_profile("US"), stock_pool=("AAPL",))

        rows = _parse_tencent_us_pool(
            text,
            "2026-10-24T06:00:00+08:00",
            profile,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0:4], ("US", "AAPL", "Apple", 347.73))
        self.assertEqual(rows[0][4:6], (-0.49, -0.14))
        self.assertEqual(rows[0][6:10], (349.12, 351.73, 345.75, 348.22))
        self.assertEqual(rows[0][11], 13760827472.0)
        self.assertEqual(rows[0][12], 1.72)
        self.assertEqual(rows[0][17], "2026-10-24T04:00:01+08:00")

    def test_tencent_us_indices_fields_are_parsed(self) -> None:
        values = ["0"] * 72
        values[1] = "标普500"
        values[2] = ".INX"
        values[3] = "7623.68"
        values[4] = "7663.43"
        values[5] = "7637.18"
        values[31] = "-39.75"
        values[32] = "-0.52"
        values[33] = "7660.15"
        values[34] = "7612.07"
        text = 'v_usINX="' + "~".join(values) + '";'

        rows = _parse_tencent_us_indices(
            text,
            "2026-10-24T06:00:00+08:00",
            get_profile("US"),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0:4], ("US", ".INX", "标普500", 7623.68))
        self.assertEqual(rows[0][4:6], (-39.75, -0.52))


class UsCollectorCacheTests(unittest.TestCase):
    def test_stock_pool_is_the_configured_us_watchlist(self) -> None:
        self.assertEqual(
            get_profile("US").stock_pool,
            ("VOO", "QQQM", "SPCX", "GOOG", "RKLB", "IREN", "AAOI"),
        )

    def test_stale_pool_cache_is_pruned_without_touching_other_markets(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            collector.init_db(connection)
            connection.execute(
                "CREATE TABLE daily_closes ("
                "market TEXT, symbol TEXT, trade_date TEXT, close REAL)"
            )
            connection.executemany(
                "INSERT INTO quotes (market, symbol, quoted_at) "
                "VALUES (?, ?, ?)",
                [
                    ("US", "VOO", "2026-10-24T04:00:00+08:00"),
                    ("US", "AAPL", "2026-10-24T04:00:00+08:00"),
                    ("CN", "600519", "2026-10-24T04:00:00+08:00"),
                ],
            )
            connection.executemany(
                "INSERT INTO daily_closes "
                "(market, symbol, trade_date, close) VALUES (?, ?, ?, ?)",
                [
                    ("US", "VOO", "2026-10-23", 698.58),
                    ("US", "AAPL", "2026-10-23", 347.73),
                    ("CN", "600519", "2026-10-23", 1400.0),
                ],
            )
            connection.commit()

            collector._prune_stale_pool_cache(connection, get_profile("US"))

            us_quotes = connection.execute(
                "SELECT symbol FROM quotes WHERE market = 'US' ORDER BY symbol"
            ).fetchall()
            us_daily = connection.execute(
                "SELECT symbol FROM daily_closes "
                "WHERE market = 'US' ORDER BY symbol"
            ).fetchall()
            cn_quotes = connection.execute(
                "SELECT symbol FROM quotes WHERE market = 'CN'"
            ).fetchall()
            cn_daily = connection.execute(
                "SELECT symbol FROM daily_closes WHERE market = 'CN'"
            ).fetchall()
            self.assertEqual([row[0] for row in us_quotes], ["VOO"])
            self.assertEqual([row[0] for row in us_daily], ["VOO"])
            self.assertEqual([row[0] for row in cn_quotes], ["600519"])
            self.assertEqual([row[0] for row in cn_daily], ["600519"])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

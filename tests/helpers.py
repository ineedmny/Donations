from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def make_quote(module, symbol: str, *, price: float = 100.0, market: str = "CN"):
    return module.Quote(
        market=market,
        symbol=symbol,
        name=symbol,
        price=price,
        change_pct=1.0,
        open=price,
        high=price,
        low=price,
        prev_close=price,
        volume=10_000,
        amount=1_000_000,
        amplitude=2.0,
        change_pct_5d=1.0,
        change_pct_10d=2.0,
        change_pct_20d=3.0,
        change_pct_60d=4.0,
        quoted_at="2026-10-24T10:00:00+08:00",
    )


def insert_quote(connection: sqlite3.Connection, row: tuple) -> None:
    from collector import INSERT_QUOTE_SQL

    connection.execute(INSERT_QUOTE_SQL, row)
    connection.commit()

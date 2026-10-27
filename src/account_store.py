"""账户元数据、账户范围股票池与旧单账户数据库迁移。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from market_profile import MarketProfile


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
CN_SMALL_ACCOUNT_SLUG = "small-3000"
CN_SMALL_ACCOUNT_CASH = 3_000.0
CN_SMALL_ACCOUNT_MIN_TURNOVER = 200_000_000.0
CN_SMALL_ACCOUNT_POOL_LIMIT = 30

CREATE_ACCOUNTS_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    principal REAL NOT NULL CHECK (principal >= 0),
    initial_cash REAL NOT NULL CHECK (initial_cash >= 0),
    cash REAL NOT NULL CHECK (cash >= 0),
    market_value REAL NOT NULL DEFAULT 0,
    total_asset REAL NOT NULL,
    pool_mode TEXT NOT NULL DEFAULT 'fixed'
        CHECK (pool_mode IN ('fixed', 'affordable')),
    pool_min_turnover REAL NOT NULL DEFAULT 0,
    pool_max_lot_cost REAL,
    pool_limit INTEGER NOT NULL DEFAULT 30 CHECK (pool_limit > 0),
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (market, slug)
)
"""

CREATE_STOCK_POOL_SQL = """
CREATE TABLE IF NOT EXISTS account_stock_pool (
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    price REAL,
    amount REAL,
    lot_cost REAL,
    reason TEXT,
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
)
"""


def now_iso() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _insert_default_account(
    connection: sqlite3.Connection,
    profile: MarketProfile,
    *,
    timestamp: str,
) -> None:
    exists = connection.execute(
        "SELECT 1 FROM accounts WHERE market = ? AND is_default = 1",
        (profile.code,),
    ).fetchone()
    if exists is not None:
        return
    connection.execute(
        "INSERT OR IGNORE INTO accounts ("
        "market, slug, display_name, principal, initial_cash, cash, "
        "market_value, total_asset, pool_mode, pool_min_turnover, "
        "pool_max_lot_cost, pool_limit, is_default, status, created_at, updated_at"
        ") VALUES (?, 'main', ?, ?, ?, ?, 0, ?, 'fixed', 0, NULL, ?, 1, "
        "'active', ?, ?)",
        (
            profile.code,
            f"{profile.label}主账户",
            profile.initial_cash,
            profile.initial_cash,
            profile.initial_cash,
            profile.initial_cash,
            max(len(profile.stock_pool), 1),
            timestamp,
            timestamp,
        ),
    )


def _migrate_legacy_account(
    connection: sqlite3.Connection,
    profile: MarketProfile,
    *,
    timestamp: str,
) -> None:
    if not _table_exists(connection, "account"):
        return
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(account)")
    }
    if {"cash", "total_asset"}.issubset(columns):
        principal = "principal" if "principal" in columns else "initial_cash"
        market_value = "market_value" if "market_value" in columns else "0"
        updated_expression = "COALESCE(updated_at, ?)"
        if "updated_at" not in columns:
            updated_expression = "?"
        connection.execute(
            "INSERT OR IGNORE INTO accounts ("
            "id, market, slug, display_name, principal, initial_cash, cash, "
            "market_value, total_asset, pool_mode, pool_min_turnover, "
            "pool_max_lot_cost, pool_limit, is_default, status, created_at, updated_at"
            f") SELECT id, ?, 'main', ?, COALESCE({principal}, initial_cash), "
            f"initial_cash, cash, COALESCE({market_value}, 0), total_asset, "
            "'fixed', 0, NULL, ?, 1, 'active', ?, "
            f"{updated_expression} FROM account WHERE id = 1",
            (
                profile.code,
                f"{profile.label}主账户",
                max(len(profile.stock_pool), 1),
                timestamp,
                timestamp,
            ),
        )
    connection.execute("DROP TABLE account")


def _seed_cn_small_account(
    connection: sqlite3.Connection,
    profile: MarketProfile,
    *,
    timestamp: str,
) -> None:
    if profile.code != "CN":
        return
    connection.execute(
        "INSERT OR IGNORE INTO accounts ("
        "market, slug, display_name, principal, initial_cash, cash, "
        "market_value, total_asset, pool_mode, pool_min_turnover, "
        "pool_max_lot_cost, pool_limit, is_default, status, created_at, updated_at"
        ") VALUES (?, ?, '3,000 元小额账户', ?, ?, ?, 0, ?, 'affordable', ?, ?, ?, "
        "0, 'active', ?, ?)",
        (
            profile.code,
            CN_SMALL_ACCOUNT_SLUG,
            CN_SMALL_ACCOUNT_CASH,
            CN_SMALL_ACCOUNT_CASH,
            CN_SMALL_ACCOUNT_CASH,
            CN_SMALL_ACCOUNT_CASH,
            CN_SMALL_ACCOUNT_MIN_TURNOVER,
            CN_SMALL_ACCOUNT_CASH,
            CN_SMALL_ACCOUNT_POOL_LIMIT,
            timestamp,
            timestamp,
        ),
    )


def _seed_fixed_stock_pools(
    connection: sqlite3.Connection,
    profile: MarketProfile,
    *,
    timestamp: str,
) -> None:
    accounts = connection.execute(
        "SELECT id FROM accounts WHERE market = ? AND pool_mode = 'fixed'",
        (profile.code,),
    ).fetchall()
    for row in accounts:
        account_id = int(row[0])
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM account_stock_pool WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
        )
        if count:
            continue
        connection.executemany(
            "INSERT OR IGNORE INTO account_stock_pool ("
            "account_id, symbol, name, price, amount, lot_cost, reason, refreshed_at"
            ") VALUES (?, ?, NULL, NULL, NULL, NULL, '市场档案固定股票池', ?)",
            [(account_id, symbol, timestamp) for symbol in profile.stock_pool],
        )


def ensure_schema(
    connection: sqlite3.Connection,
    profile: MarketProfile,
) -> None:
    timestamp = now_iso()
    connection.execute(CREATE_ACCOUNTS_SQL)
    connection.execute(CREATE_STOCK_POOL_SQL)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_market_slug "
        "ON accounts(market, slug)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_market_default "
        "ON accounts(market) WHERE is_default = 1"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_pool_account "
        "ON account_stock_pool(account_id)"
    )
    with connection:
        _migrate_legacy_account(connection, profile, timestamp=timestamp)
        _insert_default_account(connection, profile, timestamp=timestamp)
        _seed_cn_small_account(connection, profile, timestamp=timestamp)
        _seed_fixed_stock_pools(connection, profile, timestamp=timestamp)


def list_accounts(
    connection: sqlite3.Connection,
    market: str,
    *,
    active_only: bool = True,
) -> list[sqlite3.Row]:
    where = " AND status = 'active'" if active_only else ""
    return connection.execute(
        "SELECT * FROM accounts WHERE market = ?" + where + " "
        "ORDER BY is_default DESC, id",
        (market,),
    ).fetchall()


def get_account(
    connection: sqlite3.Connection,
    market: str,
    slug: str | None = None,
) -> sqlite3.Row:
    if slug:
        row = connection.execute(
            "SELECT * FROM accounts WHERE market = ? AND slug = ?",
            (market, slug),
        ).fetchone()
        if row is None:
            raise ValueError(f"账户不存在: {slug}")
        return row
    row = connection.execute(
        "SELECT * FROM accounts WHERE market = ? AND is_default = 1 "
        "ORDER BY id LIMIT 1",
        (market,),
    ).fetchone()
    if row is None:
        raise ValueError(f"{market} 市场没有默认账户")
    return row


def load_stock_pool(
    connection: sqlite3.Connection,
    account_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM account_stock_pool WHERE account_id = ? "
        "ORDER BY COALESCE(amount, 0) DESC, symbol",
        (account_id,),
    ).fetchall()


def replace_stock_pool(
    connection: sqlite3.Connection,
    account_id: int,
    entries: list[dict[str, Any]],
    *,
    refreshed_at: str,
) -> None:
    with connection:
        connection.execute(
            "DELETE FROM account_stock_pool WHERE account_id = ?",
            (account_id,),
        )
        connection.executemany(
            "INSERT INTO account_stock_pool ("
            "account_id, symbol, name, price, amount, lot_cost, reason, refreshed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    account_id,
                    str(item["symbol"]),
                    item.get("name"),
                    item.get("price"),
                    item.get("amount"),
                    item.get("lot_cost"),
                    item.get("reason"),
                    refreshed_at,
                )
                for item in entries
            ],
        )


def mark_stock_pool_refreshed(
    connection: sqlite3.Connection,
    account_id: int,
    refreshed_at: str,
) -> int:
    """记录账户池行情已按该时间刷新；不改变池成员。"""
    with connection:
        cursor = connection.execute(
            "UPDATE account_stock_pool SET refreshed_at = ? WHERE account_id = ?",
            (refreshed_at, account_id),
        )
    return int(cursor.rowcount)


def is_supported_cn_lot_symbol(symbol: str) -> bool:
    return symbol.startswith(("00", "30", "60"))


def estimated_buy_lot_cost(
    price: float,
    lot_size: int,
    profile: MarketProfile,
) -> float:
    amount = float(price) * int(lot_size)
    commission = max(profile.min_commission, amount * profile.commission_rate)
    transfer_fee = amount * profile.transfer_fee_rate
    fee = round(commission + transfer_fee, 2)
    return round(amount + fee, 2)


def select_affordable_stock_entries(
    rows: list[Any],
    *,
    principal: float,
    min_turnover: float,
    lot_size: int,
    limit: int,
    profile: MarketProfile,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row["symbol"])
        name = str(row["name"] or "")
        price_value = row["price"]
        amount_value = row["amount"]
        if not is_supported_cn_lot_symbol(symbol):
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        if price_value is None or amount_value is None:
            continue
        price = float(price_value)
        amount = float(amount_value)
        if price <= 0 or amount < min_turnover:
            continue
        lot_cost = estimated_buy_lot_cost(price, lot_size, profile)
        if lot_cost > principal + 1e-9:
            continue
        selected.append(
            {
                "symbol": symbol,
                "name": name,
                "price": round(price, 4),
                "amount": round(amount, 2),
                "lot_cost": lot_cost,
                "reason": f"成交额 {amount / 100_000_000:.2f} 亿元 · 一手 {lot_cost:.2f} 元",
            }
        )
    selected.sort(key=lambda item: (-float(item["amount"]), str(item["symbol"])))
    return selected[: max(int(limit), 0)]


def refresh_dynamic_stock_pools(
    connection: sqlite3.Connection,
    profile: MarketProfile,
    quoted_at: str,
) -> dict[int, int]:
    if profile.code != "CN":
        return {}
    if not _table_exists(connection, "accounts") or not _table_exists(
        connection, "quotes"
    ):
        return {}
    accounts = connection.execute(
        "SELECT * FROM accounts WHERE market = ? AND status = 'active' "
        "AND pool_mode = 'affordable' ORDER BY id",
        (profile.code,),
    ).fetchall()
    if not accounts:
        return {}
    quote_rows = connection.execute(
        "SELECT symbol, name, price, amount FROM quotes "
        "WHERE market = ? AND quoted_at = ? AND price IS NOT NULL AND price > 0",
        (profile.code, quoted_at),
    ).fetchall()
    counts: dict[int, int] = {}
    for account in accounts:
        account_id = int(account["id"])
        principal = float(
            account["pool_max_lot_cost"]
            if account["pool_max_lot_cost"] is not None
            else account["principal"]
        )
        entries = select_affordable_stock_entries(
            list(quote_rows),
            principal=principal,
            min_turnover=float(account["pool_min_turnover"]),
            lot_size=profile.lot_size,
            limit=int(account["pool_limit"]),
            profile=profile,
        )
        replace_stock_pool(
            connection,
            account_id,
            entries,
            refreshed_at=quoted_at,
        )
        counts[account_id] = len(entries)
    return counts

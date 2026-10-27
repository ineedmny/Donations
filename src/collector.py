#!/usr/bin/env python3
"""采集当前市场固定股票池的实时行情并写入 SQLite。"""

from __future__ import annotations

import argparse
import fcntl
import logging
import math
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import account_store
from config import DB_PATHS, DEFAULT_DB, MARKET as CONFIG_MARKET
from market_profile import (
    BEIJING_TZ,
    MarketProfile,
    get_profile,
    normalize_symbol as normalize_market_symbol,
    resolve_market,
)


LOGGER = logging.getLogger("quote_collector")
TZ = ZoneInfo("Asia/Shanghai")

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_QUOTE_PATTERN = re.compile(
    r'v_((?:sh|sz|bj)\d{6})="([^"]*)"'
)
TENCENT_US_QUOTE_PATTERN = re.compile(r'v_(us[A-Za-z0-9.]+)="([^"]*)"')

# 各行业龙头，顺序固定；每次只采集这 30 只。
CN_STOCK_POOL = (
    "600519",  # 贵州茅台
    "300750",  # 宁德时代
    "600900",  # 长江电力
    "601318",  # 中国平安
    "600036",  # 招商银行
    "601398",  # 工商银行
    "600030",  # 中信证券
    "000333",  # 美的集团
    "000858",  # 五粮液
    "600887",  # 伊利股份
    "600276",  # 恒瑞医药
    "300760",  # 迈瑞医疗
    "603259",  # 药明康德
    "002594",  # 比亚迪
    "601012",  # 隆基绿能
    "600031",  # 三一重工
    "000651",  # 格力电器
    "601899",  # 紫金矿业
    "600028",  # 中国石化
    "601088",  # 中国神华
    "600019",  # 宝钢股份
    "600050",  # 中国联通
    "600941",  # 中国移动
    "000725",  # 京东方A
    "002415",  # 海康威视
    "688981",  # 中芯国际
    "002371",  # 北方华创
    "600309",  # 万华化学
    "601668",  # 中国建筑
    "600048",  # 保利发展
)
CN_STOCK_SET = frozenset(CN_STOCK_POOL)

CN_INDEX_POOL = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
)
CN_INDEX_CODES = frozenset(code for code, _ in CN_INDEX_POOL)

FIELD_ALIASES = {
    "symbol": ("代码", "证券代码", "symbol", "code", "ticker"),
    "name": ("名称", "证券简称", "name"),
    "price": ("最新价", "现价", "最新", "price", "last_price", "zxj"),
    "change_amount": (
        "涨跌额",
        "涨跌",
        "change_amount",
        "change",
        "zd",
        "zde",
    ),
    "change_pct": ("涨跌幅", "涨跌幅(%)", "change_pct", "pct_chg", "zdf"),
    "open": ("今开", "开盘价", "开盘", "open", "jk"),
    "high": ("最高", "最高价", "high", "zg"),
    "low": ("最低", "最低价", "low", "zdi"),
    "prev_close": (
        "昨收",
        "昨收价",
        "前收盘",
        "prev_close",
        "pre_close",
        "previous_close",
    ),
    "volume": ("成交量", "volume", "vol", "cjl"),
    "amount": ("成交额", "成交金额", "amount", "turnover", "cje"),
    "amplitude": ("振幅", "amplitude", "zf"),
    "change_pct_5d": ("5日涨跌幅", "涨跌幅5日", "change_pct_5d", "zdf_d5"),
    "change_pct_10d": (
        "10日涨跌幅",
        "涨跌幅10日",
        "change_pct_10d",
        "zdf_d10",
    ),
    "change_pct_20d": (
        "20日涨跌幅",
        "涨跌幅20日",
        "change_pct_20d",
        "zdf_d20",
    ),
    "change_pct_60d": (
        "60日涨跌幅",
        "涨跌幅60日",
        "change_pct_60d",
        "zdf_d60",
    ),
}

CREATE_QUOTES_SQL = """
CREATE TABLE IF NOT EXISTS quotes (
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    price REAL,
    change_amount REAL,
    change_pct REAL,
    open REAL,
    high REAL,
    low REAL,
    prev_close REAL,
    volume REAL,
    amount REAL,
    amplitude REAL,
    change_pct_5d REAL,
    change_pct_10d REAL,
    change_pct_20d REAL,
    change_pct_60d REAL,
    quoted_at TEXT NOT NULL,
    PRIMARY KEY (market, symbol)
)
"""

CREATE_MARKET_INDICES_SQL = """
CREATE TABLE IF NOT EXISTS market_indices (
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    price REAL NOT NULL,
    change_amount REAL,
    change_pct REAL,
    open REAL,
    high REAL,
    low REAL,
    prev_close REAL,
    quoted_at TEXT NOT NULL,
    PRIMARY KEY (market, symbol)
)
"""

QUOTE_EXTRA_COLUMNS = (
    ("amplitude", "REAL"),
    ("change_pct_5d", "REAL"),
    ("change_pct_10d", "REAL"),
    ("change_pct_20d", "REAL"),
    ("change_pct_60d", "REAL"),
    ("ma5", "REAL"),
    ("ma10", "REAL"),
    ("ma20", "REAL"),
    ("rsi14", "REAL"),
    ("macd_dif", "REAL"),
    ("macd_dea", "REAL"),
    ("macd_hist", "REAL"),
    ("atr14", "REAL"),
)

INSERT_QUOTE_SQL = """
INSERT INTO quotes (
    market, symbol, name, price, change_amount, change_pct,
    open, high, low, prev_close, volume, amount,
    amplitude, change_pct_5d, change_pct_10d, change_pct_20d,
    change_pct_60d, quoted_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(market, symbol) DO UPDATE SET
    name = excluded.name,
    price = excluded.price,
    change_amount = excluded.change_amount,
    change_pct = excluded.change_pct,
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    prev_close = excluded.prev_close,
    volume = excluded.volume,
    amount = excluded.amount,
    amplitude = excluded.amplitude,
    change_pct_5d = excluded.change_pct_5d,
    change_pct_10d = excluded.change_pct_10d,
    change_pct_20d = excluded.change_pct_20d,
    change_pct_60d = excluded.change_pct_60d,
    quoted_at = excluded.quoted_at
"""

UPSERT_MARKET_INDEX_SQL = """
INSERT INTO market_indices (
    market, symbol, name, price, change_amount, change_pct,
    open, high, low, prev_close, quoted_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(market, symbol) DO UPDATE SET
    name = excluded.name,
    price = excluded.price,
    change_amount = excluded.change_amount,
    change_pct = excluded.change_pct,
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    prev_close = excluded.prev_close,
    quoted_at = excluded.quoted_at
"""


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in {
        "",
        "-",
        "--",
        "nan",
        "none",
        "null",
        "<na>",
        "nat",
    }


def _as_text(value: Any) -> str | None:
    if _is_missing(value):
        return None
    return str(value).strip()


def _as_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def _latest_provider_timestamp(
    quote_values: Iterable[list[str]],
    profile: MarketProfile,
    fallback: str,
) -> str:
    latest: datetime | None = None
    for values in quote_values:
        if len(values) <= 30:
            continue
        raw = str(values[30]).strip()
        try:
            timestamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=profile.market_timezone
            )
        except ValueError:
            continue
        if latest is None or timestamp > latest:
            latest = timestamp
    if latest is None:
        return fallback
    return latest.astimezone(BEIJING_TZ).isoformat(timespec="seconds")


def _normalize_symbol(market: str, value: Any) -> str | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    try:
        return normalize_market_symbol(get_profile(market), text)
    except ValueError:
        return None


def _tencent_code(symbol: str) -> str | None:
    if symbol.startswith("920"):
        prefix = "bj"
    elif symbol.startswith(("6", "9")):
        prefix = "sh"
    elif symbol.startswith(("0", "2", "3")):
        prefix = "sz"
    elif symbol.startswith(("4", "8")):
        prefix = "bj"
    else:
        return None
    return prefix + symbol


def _parse_tencent_symbols(
    text: str,
    symbols: Sequence[str],
    quoted_at: str,
) -> list[tuple[Any, ...]]:
    wanted = tuple(dict.fromkeys(str(symbol) for symbol in symbols))
    wanted_set = set(wanted)
    parsed = {}
    for match in TENCENT_QUOTE_PATTERN.finditer(text):
        code, raw_values = match.groups()
        values = raw_values.split("~")
        symbol = code[2:]
        if symbol not in wanted_set:
            continue
        if len(values) <= 71:
            LOGGER.warning("腾讯行情字段不足，已跳过: %s", code)
            continue
        parsed[symbol] = values

    rows = []
    for symbol in wanted:
        values = parsed.get(symbol)
        if values is None:
            LOGGER.warning("腾讯行情未返回股票，已跳过: %s", symbol)
            continue
        price = _as_float(values[3])
        if price is None or price <= 0:
            LOGGER.warning("腾讯行情最新价无效，已跳过: %s", symbol)
            continue
        rows.append(
            (
                "CN",
                symbol,
                _as_text(values[1]),
                price,
                _as_float(values[31]),
                _as_float(values[32]),
                _as_float(values[5]),
                _as_float(values[33]),
                _as_float(values[34]),
                _as_float(values[4]),
                _as_float(values[6]),
                _as_float(values[37]),
                _as_float(values[43]),
                _as_float(values[63]),
                _as_float(values[69]),
                _as_float(values[70]),
                _as_float(values[71]),
                quoted_at,
            )
        )
    return rows


def _parse_tencent_pool(
    text: str,
    quoted_at: str,
) -> list[tuple[Any, ...]]:
    rows = _parse_tencent_symbols(text, CN_STOCK_POOL, quoted_at)

    if len(rows) != len(CN_STOCK_POOL):
        raise ValueError(
            f"固定股票池仅返回 {len(rows)}/{len(CN_STOCK_POOL)} 只股票"
        )
    return rows


def _fetch_cn_symbol_rows(
    symbols: Sequence[str],
    quoted_at: str,
) -> list[tuple[Any, ...]]:
    import requests

    codes = [_tencent_code(symbol) for symbol in symbols]
    valid_codes = [code for code in codes if code is not None]
    if not valid_codes:
        raise ValueError("选中股票中没有可生成腾讯代码的股票")
    response = requests.get(
        TENCENT_QUOTE_URL + ",".join(valid_codes),
        timeout=20,
    )
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")
    return _parse_tencent_symbols(text, symbols, quoted_at)


def _fetch_cn_pool_rows(quoted_at: str) -> list[tuple[Any, ...]]:
    rows = _fetch_cn_symbol_rows(CN_STOCK_POOL, quoted_at)
    if len(rows) != len(CN_STOCK_POOL):
        raise ValueError(
            f"固定股票池仅返回 {len(rows)}/{len(CN_STOCK_POOL)} 只股票"
        )
    return rows


def parse_cn_full_market_frame(
    frame: Any,
    quoted_at: str,
) -> list[dict[str, Any]]:
    if frame is None or len(frame) == 0:
        return []
    required = {"code", "name", "zxj", "turnover"}
    missing = required - {str(column) for column in frame.columns}
    if missing:
        raise ValueError(f"腾讯全市场行情缺少字段: {', '.join(sorted(missing))}")

    profile = get_profile("CN")
    rows: list[dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        symbol = _normalize_symbol("CN", record.get("code"))
        if symbol is None:
            continue
        price = _as_float(record.get("zxj"))
        turnover_wan = _as_float(record.get("turnover"))
        if price is None or price <= 0 or turnover_wan is None:
            continue
        change_amount = _as_float(record.get("zd"))
        prev_close = (
            round(price - change_amount, 4)
            if change_amount is not None
            else None
        )
        rows.append(
            {
                "market": "CN",
                "symbol": symbol,
                "name": _as_text(record.get("name")),
                "price": price,
                "change_amount": change_amount,
                "change_pct": _as_float(record.get("zdf")),
                "open": None,
                "high": None,
                "low": None,
                "prev_close": prev_close,
                "volume": _as_float(record.get("volume")),
                "amount": round(turnover_wan * 10_000.0, 2),
                "amplitude": _as_float(record.get("zf")),
                "change_pct_5d": _as_float(record.get("zdf_d5")),
                "change_pct_10d": _as_float(record.get("zdf_d10")),
                "change_pct_20d": _as_float(record.get("zdf_d20")),
                "change_pct_60d": _as_float(record.get("zdf_d60")),
                "quoted_at": quoted_at,
                "_profile": profile,
            }
        )
    return rows


def select_cn_affordable_pool(
    rows: list[dict[str, Any]],
    *,
    principal: float,
    min_turnover: float,
    lot_size: int,
    limit: int,
    profile: MarketProfile,
) -> list[dict[str, Any]]:
    return account_store.select_affordable_stock_entries(
        rows,
        principal=principal,
        min_turnover=min_turnover,
        lot_size=lot_size,
        limit=limit,
        profile=profile,
    )


def _full_market_quote_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["market"],
        row["symbol"],
        row["name"],
        row["price"],
        row["change_amount"],
        row["change_pct"],
        row["open"],
        row["high"],
        row["low"],
        row["prev_close"],
        row["volume"],
        row["amount"],
        row["amplitude"],
        row["change_pct_5d"],
        row["change_pct_10d"],
        row["change_pct_20d"],
        row["change_pct_60d"],
        row["quoted_at"],
    )


def _fetch_cn_full_market_rows(quoted_at: str) -> list[tuple[Any, ...]]:
    import akshare as ak

    frame = ak.stock_zh_a_spot_tx()
    rows = parse_cn_full_market_frame(frame, quoted_at)
    return [
        _full_market_quote_tuple(row)
        for row in rows
        if account_store.is_supported_cn_lot_symbol(str(row["symbol"]))
    ]


def _parse_tencent_indices(
    text: str,
    quoted_at: str,
) -> list[tuple[Any, ...]]:
    parsed = {
        match.group(1): match.group(2).split("~")
        for match in TENCENT_QUOTE_PATTERN.finditer(text)
        if match.group(1) in CN_INDEX_CODES
    }
    rows = []
    for code, fallback_name in CN_INDEX_POOL:
        values = parsed.get(code)
        if values is None or len(values) <= 34:
            LOGGER.warning("腾讯指数行情未返回，已跳过: %s", code)
            continue
        price = _as_float(values[3])
        if price is None or price <= 0:
            LOGGER.warning("腾讯指数最新点位无效，已跳过: %s", code)
            continue
        rows.append(
            (
                "CN",
                code,
                _as_text(values[1]) or fallback_name,
                price,
                _as_float(values[31]),
                _as_float(values[32]),
                _as_float(values[5]),
                _as_float(values[33]),
                _as_float(values[34]),
                _as_float(values[4]),
                quoted_at,
            )
        )
    if len(rows) != len(CN_INDEX_POOL):
        raise ValueError(
            f"A股指数仅返回 {len(rows)}/{len(CN_INDEX_POOL)} 个"
        )
    return rows


def _fetch_cn_index_rows(quoted_at: str) -> list[tuple[Any, ...]]:
    import requests

    response = requests.get(
        TENCENT_QUOTE_URL + ",".join(code for code, _ in CN_INDEX_POOL),
        timeout=20,
    )
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")
    return _parse_tencent_indices(text, quoted_at)


def _fetch_us_pool_rows(
    quoted_at: str, profile: MarketProfile
) -> list[tuple[Any, ...]]:
    try:
        rows = _fetch_tencent_us_pool_rows(quoted_at, profile)
        if rows:
            return rows
    except Exception as exc:
        LOGGER.warning("腾讯美股实时行情失败，改用东方财富备用源: %s", exc)

    import akshare as ak

    frame = ak.stock_us_spot_em()
    if frame is None or frame.empty:
        raise ValueError("东方财富美股实时行情返回空数据")
    rows = normalize_frame(frame, profile.code, quoted_at)
    by_symbol = {
        str(row[1]): row
        for row in rows
        if row[3] is not None and float(row[3]) > 0
    }
    selected = [by_symbol[symbol] for symbol in profile.stock_pool if symbol in by_symbol]
    missing = [symbol for symbol in profile.stock_pool if symbol not in by_symbol]
    if missing:
        LOGGER.warning("美股行情未返回股票，已跳过: %s", ", ".join(missing))
    if not selected:
        raise ValueError("固定美股股票池没有可用行情")
    return selected


def _fetch_tencent_us_pool_rows(
    quoted_at: str, profile: MarketProfile
) -> list[tuple[Any, ...]]:
    import requests

    codes = [f"us{symbol.upper()}" for symbol in profile.stock_pool]
    response = requests.get(
        TENCENT_QUOTE_URL + ",".join(codes),
        timeout=20,
    )
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")
    return _parse_tencent_us_pool(text, quoted_at, profile)


def _parse_tencent_us_pool(
    text: str,
    quoted_at: str,
    profile: MarketProfile,
) -> list[tuple[Any, ...]]:
    parsed = {
        match.group(1)[2:].upper(): match.group(2).split("~")
        for match in TENCENT_US_QUOTE_PATTERN.finditer(text)
    }
    effective_quoted_at = _latest_provider_timestamp(
        parsed.values(), profile, quoted_at
    )

    rows = []
    missing = []
    for symbol in profile.stock_pool:
        values = parsed.get(symbol)
        if values is None or len(values) <= 43:
            missing.append(symbol)
            continue
        price = _as_float(values[3])
        if price is None or price <= 0:
            missing.append(symbol)
            continue
        rows.append(
            (
                profile.code,
                symbol,
                _as_text(values[1]),
                price,
                _as_float(values[31]),
                _as_float(values[32]),
                _as_float(values[5]),
                _as_float(values[33]),
                _as_float(values[34]),
                _as_float(values[4]),
                _as_float(values[6]),
                _as_float(values[37]),
                _as_float(values[43]),
                None,
                None,
                None,
                None,
                effective_quoted_at,
            )
        )
    if missing:
        LOGGER.warning("腾讯美股行情未返回股票，已跳过: %s", ", ".join(missing))
    if not rows:
        raise ValueError("腾讯美股固定股票池没有可用行情")
    return rows


def _parse_tencent_us_indices(
    text: str,
    quoted_at: str,
    profile: MarketProfile,
) -> list[tuple[Any, ...]]:
    parsed = {
        match.group(1)[2:].upper(): match.group(2).split("~")
        for match in TENCENT_US_QUOTE_PATTERN.finditer(text)
    }
    effective_quoted_at = _latest_provider_timestamp(
        parsed.values(), profile, quoted_at
    )
    rows = []
    for symbol, fallback_name in profile.index_pool:
        key = symbol.lstrip(".").upper()
        values = parsed.get(key)
        if values is None or len(values) <= 34:
            LOGGER.warning("腾讯美股指数行情未返回，已跳过: %s", symbol)
            continue
        price = _as_float(values[3])
        if price is None or price <= 0:
            LOGGER.warning("腾讯美股指数点位无效，已跳过: %s", symbol)
            continue
        rows.append(
            (
                profile.code,
                symbol,
                _as_text(values[1]) or fallback_name,
                price,
                _as_float(values[31]),
                _as_float(values[32]),
                _as_float(values[5]),
                _as_float(values[33]),
                _as_float(values[34]),
                _as_float(values[4]),
                effective_quoted_at,
            )
        )
    if not rows:
        raise ValueError("腾讯美股主要指数均未获取到")
    return rows


def _fetch_tencent_us_index_rows(
    quoted_at: str,
    profile: MarketProfile,
) -> list[tuple[Any, ...]]:
    import requests

    codes = [f"us{symbol.lstrip('.')}" for symbol, _ in profile.index_pool]
    response = requests.get(
        TENCENT_QUOTE_URL + ",".join(codes),
        timeout=20,
    )
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")
    return _parse_tencent_us_indices(text, quoted_at, profile)


def _fetch_us_index_rows(
    quoted_at: str, profile: MarketProfile
) -> list[tuple[Any, ...]]:
    try:
        rows = _fetch_tencent_us_index_rows(quoted_at, profile)
        if rows:
            return rows
    except Exception as exc:
        LOGGER.warning("腾讯美股指数失败，改用新浪备用源: %s", exc)

    import akshare as ak
    import pandas as pd

    rows = []
    for symbol, fallback_name in profile.index_pool:
        try:
            frame = ak.index_us_stock_sina(symbol=symbol)
        except Exception as exc:
            LOGGER.warning("美股指数 %s 获取失败: %s", symbol, exc)
            continue
        if frame is None or frame.empty or "close" not in frame.columns:
            LOGGER.warning("美股指数 %s 返回空数据", symbol)
            continue
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["date", "close"]).sort_values("date")
        if frame.empty:
            LOGGER.warning("美股指数 %s 没有有效收盘价", symbol)
            continue
        latest = frame.iloc[-1]
        previous = frame.iloc[-2] if len(frame) >= 2 else latest
        price = float(latest["close"])
        prev_close = float(previous["close"])
        change_amount = price - prev_close
        change_pct = change_amount / prev_close * 100 if prev_close > 0 else None
        rows.append(
            (
                profile.code,
                symbol,
                _as_text(latest.get("name")) or fallback_name,
                price,
                change_amount,
                change_pct,
                _as_float(latest.get("open")),
                _as_float(latest.get("high")),
                _as_float(latest.get("low")),
                prev_close,
                quoted_at,
            )
        )
    if not rows:
        raise ValueError("美股主要指数均未获取到")
    return rows


def init_db(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(CREATE_QUOTES_SQL)
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(quotes)")
    }
    for column, column_type in QUOTE_EXTRA_COLUMNS:
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE quotes ADD COLUMN {column} {column_type}"
            )
    connection.execute(CREATE_MARKET_INDICES_SQL)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_market ON quotes(market)"
    )
    connection.commit()


def normalize_frame(
    frame: Any,
    market: str,
    quoted_at: str,
) -> list[tuple[Any, ...]]:
    if frame is None or len(frame) == 0:
        return []

    column_lookup = {str(column).strip(): column for column in frame.columns}
    field_columns = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in column_lookup:
                field_columns[field] = column_lookup[alias]
                break

    if "symbol" not in field_columns:
        raise ValueError(f"行情数据缺少代码列，实际列: {list(column_lookup)}")

    rows = []
    for record in frame.to_dict(orient="records"):
        symbol = _normalize_symbol(market, record.get(field_columns["symbol"]))
        if symbol is None:
            continue

        values = {}
        for field in FIELD_ALIASES:
            if field == "symbol":
                continue
            column = field_columns.get(field)
            if column is None:
                values[field] = None
            elif field == "name":
                values[field] = _as_text(record.get(column))
            else:
                values[field] = _as_float(record.get(column))

        rows.append(
            (
                market,
                symbol,
                values["name"],
                values["price"],
                values["change_amount"],
                values["change_pct"],
                values["open"],
                values["high"],
                values["low"],
                values["prev_close"],
                values["volume"],
                values["amount"],
                values["amplitude"],
                values["change_pct_5d"],
                values["change_pct_10d"],
                values["change_pct_20d"],
                values["change_pct_60d"],
                quoted_at,
            )
        )

    return rows


def _fetch_akshare(endpoints: Sequence[str]) -> Any:
    import akshare as ak

    last_error: Exception | None = None
    for endpoint in endpoints:
        try:
            frame = getattr(ak, endpoint)()
            if frame is None or len(frame) == 0:
                raise ValueError(f"{endpoint} 返回空数据")
            return frame
        except Exception as exc:
            last_error = exc
            LOGGER.warning("接口 %s 调用失败: %s", endpoint, exc)

    assert last_error is not None
    raise RuntimeError("所有行情接口均调用失败") from last_error


def _replace_market_quotes(
    connection: sqlite3.Connection,
    market: str,
    rows: Iterable[tuple[Any, ...]],
) -> None:
    with connection:
        connection.executemany(INSERT_QUOTE_SQL, rows)


def _replace_market_indices(
    connection: sqlite3.Connection,
    rows: Iterable[tuple[Any, ...]],
) -> None:
    with connection:
        connection.executemany(UPSERT_MARKET_INDEX_SQL, rows)


def _prune_stale_pool_cache(
    connection: sqlite3.Connection,
    profile: MarketProfile,
) -> None:
    """删除固定股票池之外的行情缓存，保留账户和交易业务数据。"""
    pool = tuple(profile.stock_pool)
    if not pool:
        return

    placeholders = ", ".join("?" for _ in pool)
    quote_params = (profile.code, *pool)
    daily_params = (profile.code, *pool)
    with connection:
        removed_quotes = connection.execute(
            f"DELETE FROM quotes WHERE market = ? "
            f"AND symbol NOT IN ({placeholders})",
            quote_params,
        ).rowcount
        removed_daily = 0
        has_daily_closes = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'daily_closes'"
        ).fetchone()
        if has_daily_closes is not None:
            removed_daily = connection.execute(
                f"DELETE FROM daily_closes WHERE market = ? "
                f"AND symbol NOT IN ({placeholders})",
                daily_params,
            ).rowcount

    if removed_quotes or removed_daily:
        LOGGER.info(
            "已清理固定股票池外的%s行情缓存：quotes=%d，daily_closes=%d",
            profile.label,
            removed_quotes,
            removed_daily,
        )


def load_cn_selected_symbols(
    connection: sqlite3.Connection,
    profile: MarketProfile,
) -> list[str]:
    """读取需要高频刷新的股票：固定池、动态池和当前持仓。"""
    symbols = set(profile.stock_pool)

    def has_table(name: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    if has_table("accounts") and has_table("account_stock_pool"):
        symbols.update(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT p.symbol FROM account_stock_pool p "
                "JOIN accounts a ON a.id = p.account_id "
                "WHERE a.market = ? AND a.status = 'active'",
                (profile.code,),
            ).fetchall()
        )
    if has_table("accounts") and has_table("positions"):
        symbols.update(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT p.symbol FROM positions p "
                "JOIN accounts a ON a.id = p.account_id "
                "WHERE a.market = ? AND a.status = 'active'",
                (profile.code,),
            ).fetchall()
        )
    return sorted(symbols)


def _mark_dynamic_pools_refreshed(
    connection: sqlite3.Connection,
    profile: MarketProfile,
    fetched_symbols: set[str],
    quoted_at: str,
) -> dict[int, int]:
    if profile.code != "CN":
        return {}
    accounts = connection.execute(
        "SELECT id FROM accounts WHERE market = ? AND status = 'active' "
        "AND pool_mode = 'affordable' ORDER BY id",
        (profile.code,),
    ).fetchall()
    refreshed: dict[int, int] = {}
    for account in accounts:
        account_id = int(account[0])
        pool_symbols = {
            str(row[0])
            for row in connection.execute(
                "SELECT symbol FROM account_stock_pool WHERE account_id = ?",
                (account_id,),
            ).fetchall()
        }
        if pool_symbols and pool_symbols.issubset(fetched_symbols):
            refreshed[account_id] = account_store.mark_stock_pool_refreshed(
                connection, account_id, quoted_at
            )
    return refreshed


def refresh_cn_selected_quotes(
    connection: sqlite3.Connection,
    profile: MarketProfile,
    quoted_at: str,
) -> list[tuple[Any, ...]]:
    symbols = load_cn_selected_symbols(connection, profile)
    if not symbols:
        raise ValueError("没有可刷新的选中股票")
    rows = _fetch_cn_symbol_rows(symbols, quoted_at)
    if not rows:
        raise ValueError("选中股票均未返回有效行情")
    _replace_market_quotes(connection, "CN", rows)
    fetched_symbols = {str(row[1]) for row in rows}
    refreshed = _mark_dynamic_pools_refreshed(
        connection, profile, fetched_symbols, quoted_at
    )
    if refreshed:
        LOGGER.info(
            "动态账户股票池时间戳已更新: %s",
            ", ".join(
                f"account_id={account_id} rows={count}"
                for account_id, count in sorted(refreshed.items())
            ),
        )
    return rows


def collect_market(
    connection: sqlite3.Connection,
    market: str,
    label: str,
    endpoints: Sequence[str],
    quoted_at: str,
    *,
    selected_only: bool = False,
) -> bool:
    try:
        profile = get_profile(market)
        if market == "CN" and selected_only:
            rows = refresh_cn_selected_quotes(connection, profile, quoted_at)
            try:
                index_rows = _fetch_cn_index_rows(quoted_at)
                _replace_market_indices(connection, index_rows)
                LOGGER.info("A股主要指数采集完成，共写入 %d 条", len(index_rows))
            except Exception as exc:
                LOGGER.warning("A股指数采集失败，继续使用股票池行情: %s", exc)
            LOGGER.info(
                "A股选中股票刷新完成，共写入 %d 条（未扫描全市场、未重建股票池）",
                len(rows),
            )
            return True
        if market == "CN":
            rows = _fetch_cn_full_market_rows(quoted_at)
        elif market == "US":
            rows = _fetch_us_pool_rows(quoted_at, profile)
        else:
            frame = _fetch_akshare(endpoints)
            rows = normalize_frame(frame, market, quoted_at)
        if not rows:
            raise ValueError("没有可写入的行情记录")
        _replace_market_quotes(connection, market, rows)
        if market == "CN":
            try:
                fixed_rows = _fetch_cn_pool_rows(quoted_at)
                _replace_market_quotes(connection, market, fixed_rows)
            except Exception as exc:
                LOGGER.warning("A股固定股票池详情采集失败，继续使用全市场快照: %s", exc)
            pool_counts = account_store.refresh_dynamic_stock_pools(
                connection, profile, quoted_at
            )
            if pool_counts:
                LOGGER.info(
                    "动态账户股票池已刷新: %s",
                    ", ".join(
                        f"account_id={account_id} count={count}"
                        for account_id, count in sorted(pool_counts.items())
                    ),
                )
        if market == "US":
            _prune_stale_pool_cache(connection, profile)
        if market == "CN":
            try:
                index_rows = _fetch_cn_index_rows(quoted_at)
                _replace_market_indices(connection, index_rows)
                LOGGER.info("A股主要指数采集完成，共写入 %d 条", len(index_rows))
            except Exception as exc:
                LOGGER.warning("A股指数采集失败，继续使用股票池行情: %s", exc)
        elif market == "US":
            try:
                index_rows = _fetch_us_index_rows(quoted_at, profile)
                _replace_market_indices(connection, index_rows)
                LOGGER.info("美股主要指数采集完成，共写入 %d 条", len(index_rows))
            except Exception as exc:
                LOGGER.warning("美股指数采集失败，继续使用股票池行情: %s", exc)
        LOGGER.info("%s采集完成，共写入 %d 条", label, len(rows))
        return True
    except Exception as exc:
        LOGGER.exception("%s采集失败，已跳过: %s", label, exc)
        return False


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集 A 股、港股、美股实时行情")
    parser.add_argument(
        "--market",
        choices=("CN", "US"),
        help="市场：CN=A股，US=美股（默认读取 MARKET 环境变量或 CN）",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="SQLite 数据库路径；不填则使用当前市场的默认数据库",
    )
    parser.add_argument(
        "--selected-only",
        action="store_true",
        help="只刷新固定池、动态池和当前持仓，不扫描全市场、不重建动态池",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    profile = get_profile(args.market or CONFIG_MARKET)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else DB_PATHS[profile.code].expanduser().resolve()
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    quoted_at = now_iso()

    lock_path = db_path.with_name(f"{db_path.name}.collector.lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        connection = sqlite3.connect(db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            init_db(connection)
            from trader import init_db as init_trader_db

            init_trader_db(connection, profile=profile)
            collected = collect_market(
                connection,
                profile.code,
                profile.label,
                ("akshare_us_fixed_pool",),
                quoted_at,
                selected_only=args.selected_only,
            )
        finally:
            connection.close()

    if not collected:
        LOGGER.warning("采集结束，%s行情失败", profile.label)
        return 1

    LOGGER.info("%s行情采集完成，数据库: %s", profile.label, db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""读取当前市场数据库的最新行情，调用 DeepSeek 生成决策并写入账户、持仓、成交表。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import requests

import account_store
from config import (
    DB_PATHS,
    DEFAULT_DB,
    MARKET as CONFIG_MARKET,
    PROMPT_DEBUG_PATH,
    PROMPT_DEBUG_PATHS,
)
from market_profile import (
    BEIJING_TZ,
    MarketProfile,
    get_profile,
    is_exchange_holiday,
    market_session,
    normalize_symbol as normalize_market_symbol,
    resolve_market,
)


LOGGER = logging.getLogger("trader")

MARKET = resolve_market(CONFIG_MARKET)
PROFILE = get_profile(MARKET)
TZ = BEIJING_TZ
DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TOP_N = 30
INITIAL_CASH = PROFILE.initial_cash
LOT_SIZE = PROFILE.lot_size
MAX_DECISIONS = 8
HISTORY_LOOKBACK_DAYS = 120
HISTORY_STALE_DAYS = 10
HISTORY_WORKERS = 5
MIN_HISTORY_BARS = 35
MAX_STRATEGY_CHANGES = 5
STRATEGY_STATUSES = {"active", "paused", "retired"}
DEFAULT_STRATEGY_NAME = "激进龙头动量"
DEFAULT_STRATEGY_DESCRIPTION = (
    "优先选择高成交额、短线转强或动量延续的行业龙头；小涨转强也可追，"
    "技术指标只作辅助，允许集中持仓和高波动。"
)
DEFAULT_STRATEGY_RULES = {
    "entry": [
        "优先高成交额、成交活跃的行业龙头",
        "涨幅转正、放量上行、均线结构改善或短中期动量位于前列时可主动买入",
        "不设置必须满足 RSI/MA 的硬性门槛，小涨但转强也允许参与",
    ],
    "exit": [
        "触发 strategy_params.stop_loss_pct 时由 Python 强制止损",
        "动量明显衰减、趋势破坏或出现更强标的时允许卖出换仓",
    ],
    "risk": [
        "单股仓位、单日交易次数和止损严格遵守 strategy_params",
        "允许满仓和集中持仓，不为单日波动盲目转防守",
    ],
}
DEFAULT_STRATEGY_PARAMS = {
    "max_single_position": 1.0,
    "stop_loss_pct": -0.90,
    "max_daily_trades": 10,
    "rsi_oversold": 45,
    "rsi_overbought": 85,
    "atr_stop_multiplier": 5.0,
    "min_turnover": 100_000_000.0,
    "market_regime_weight": 0.35,
    "custom_strategy_notes": "激进实时模式：小涨转强也可追，技术指标仅参考；允许满仓集中，只保留灾难性止损，不因单日回撤空仓。",
    "watchlist": [],
}
QUOTE_INDICATOR_COLUMNS = (
    ("ma5", "REAL"),
    ("ma10", "REAL"),
    ("ma20", "REAL"),
    ("rsi14", "REAL"),
    ("macd_dif", "REAL"),
    ("macd_dea", "REAL"),
    ("macd_hist", "REAL"),
    ("atr14", "REAL"),
)
STRATEGY_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

COMMISSION_RATE = PROFILE.commission_rate
MIN_COMMISSION = PROFILE.min_commission
TRANSFER_FEE_RATE = PROFILE.transfer_fee_rate
STAMP_DUTY_RATE = PROFILE.sell_regulatory_rate

CREATE_ACCOUNT_SQL = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    principal REAL,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    market_value REAL NOT NULL DEFAULT 0,
    total_asset REAL NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_DAILY_CLOSES_SQL = """
CREATE TABLE IF NOT EXISTS daily_closes (
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume REAL,
    PRIMARY KEY (market, symbol, trade_date)
)
"""

UPSERT_DAILY_BAR_SQL = """
INSERT INTO daily_closes (
    market, symbol, trade_date, open, high, low, close, volume
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(market, symbol, trade_date)
DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume
"""

UPDATE_QUOTE_INDICATORS_SQL = """
UPDATE quotes SET
    ma5 = ?, ma10 = ?, ma20 = ?, rsi14 = ?,
    macd_dif = ?, macd_dea = ?, macd_hist = ?, atr14 = ?
WHERE market = ? AND symbol = ?
"""

CREATE_STRATEGY_PARAMS_SQL = """
CREATE TABLE IF NOT EXISTS strategy_params (
    account_id INTEGER NOT NULL DEFAULT 1,
    param_name TEXT NOT NULL,
    param_value TEXT NOT NULL,
    PRIMARY KEY (account_id, param_name)
)
"""

CREATE_STRATEGY_NOTES_SQL = """
CREATE TABLE IF NOT EXISTS strategy_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    trade_date TEXT NOT NULL,
    notes TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'daily_review',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, trade_date)
)
"""

CREATE_TRADING_CALENDAR_SQL = """
CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date TEXT PRIMARY KEY,
    is_open INTEGER NOT NULL CHECK (is_open IN (0, 1)),
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

UPSERT_TRADING_CALENDAR_SQL = """
INSERT INTO trading_calendar (trade_date, is_open, source, updated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(trade_date) DO UPDATE SET
    is_open = excluded.is_open,
    source = excluded.source,
    updated_at = excluded.updated_at
"""

UPSERT_STRATEGY_PARAM_SQL = """
INSERT INTO strategy_params (account_id, param_name, param_value)
VALUES (?, ?, ?)
ON CONFLICT(account_id, param_name)
DO UPDATE SET param_value = excluded.param_value
"""

UPSERT_STRATEGY_NOTE_SQL = """
INSERT INTO strategy_notes (
    account_id, trade_date, notes, source, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(account_id, trade_date) DO UPDATE SET
    notes = excluded.notes,
    source = excluded.source,
    updated_at = excluded.updated_at
"""

CREATE_STRATEGIES_SQL = """
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL,
    rules_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'retired')),
    source TEXT NOT NULL DEFAULT 'manual',
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_CHAT_MESSAGES_SQL = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    context_as_of TEXT,
    quote_time TEXT,
    source TEXT
)
"""

CREATE_POSITIONS_SQL = """
CREATE TABLE IF NOT EXISTS positions (
    account_id INTEGER NOT NULL DEFAULT 1,
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
    PRIMARY KEY (account_id, symbol)
)
"""

CREATE_TRADES_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    traded_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'CN',
    symbol TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price REAL NOT NULL CHECK (price > 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    amount REAL NOT NULL CHECK (amount >= 0),
    fee REAL NOT NULL CHECK (fee >= 0),
    cash_after REAL NOT NULL,
    reason TEXT,
    strategy_id INTEGER,
    created_at TEXT NOT NULL
)
"""

INSERT_ACCOUNT_SQL = """
INSERT OR IGNORE INTO account (
    id, principal, initial_cash, cash, market_value, total_asset, updated_at
) VALUES (1, ?, ?, ?, 0, ?, ?)
"""

INSERT_TRADE_SQL = """
INSERT INTO trades (
    account_id, traded_at, trade_date, market, symbol, name, side, price,
    quantity, amount, fee, cash_after, reason, strategy_id, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

CREATE_DECISION_ANALYSIS_SQL = """
CREATE TABLE IF NOT EXISTS decision_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    trade_date TEXT NOT NULL,
    quoted_at TEXT NOT NULL,
    quote_count INTEGER NOT NULL,
    market_snapshot TEXT NOT NULL,
    market_analysis TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

INSERT_DECISION_ANALYSIS_SQL = """
INSERT INTO decision_analysis (
    account_id, trade_date, quoted_at, quote_count, market_snapshot,
    market_analysis, decisions_json, model, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SYSTEM_PROMPT_TEMPLATE = """你是一名激进型 __MARKET_LABEL__实时模拟交易助手。用户接受大幅回撤甚至本金损失，系统允许满仓、集中、追涨和高波动；目标是在硬约束内主动争取收益，不因单日亏损或大盘偏弱机械空仓。

数据与成交：
- quotes 是采集瞬间行情；price 是唯一成交定价依据，open/high/low/close/prev_close 仅作背景。
- 可用字段：price、open、high、low、close、prev_close、change_pct、amplitude、volume、amount、change_pct_5d/10d/20d/60d、ma5/10/20、rsi14、macd_dif/dea/hist、macd_dead_cross、atr14、atr_stop_price、below_ma20、oversold_rebound_candidate、distance_to_ma5。
- 所有指标和派生值均由 Python 计算；null 表示数据不足，不得猜测或重算。
- Python 会在成交前刷新行情并按当时 quote.price 成交，你不需要给出成交价。

决策规则：
- 小涨、微涨、刚转强、放量上行都可追；MA/RSI/MACD/ATR 只作辅助，不要求同向，也不设硬性买入阈值。RSI 偏高、偏离均线或短期涨幅较大不能单独作为拒绝买入的理由。
- 使用 market_snapshot 的 indices（__INDEX_NAMES__）、market_summary、advancers/decliners/flat、market_regime；market_regime_weight 越高，大盘和宽度影响越大。大盘偏弱时优先逆势强势、放量、成交额大的标的，不机械空仓。
- strategy_notes_history 是按日期升序保存的全部复盘笔记，必须逐条阅读并综合；冲突时优先较新经验，但不能忽略早期有效规律。与硬约束冲突时以硬约束为准。
- 行情可用、现金充足、未达 max_daily_trades 且无结算/持仓限制时，应主动给出至少 1 笔 BUY 或换仓 SELL；decisions 为空只能在硬约束无可行操作时出现，并在 decision_rationale 说明原因。
- 已持仓股票不得 BUY；无现金时评估卖出换仓。

硬约束：
- 只做多，不能做空或融资。
- __SETTLEMENT_RULE__
- __LOT_RULE__
- 卖出数量不得超过可卖数量；买入金额加手续费不得超过现金。
- 严格按 strategy_params 当前值执行，包括 max_single_position、stop_loss_pct、max_daily_trades 和历史自定义参数。
- Python 会在模型决策前按 stop_loss_pct 强制卖出已触发止损的可卖数量；不得取消、延后或绕过。
- 只能交易 quotes 中出现的股票；最多 5 笔。
- 每笔 BUY/SELL 必须选择 available_strategies 中 active 的策略 strategy_id，并在 reason 说明匹配原因；不得选择 paused/retired、列表外或编造的策略。无合适策略时不交易。

输出要求：
- 只返回一个 JSON 对象，不要 Markdown 或前后解释。
- market_analysis.overview 必须包含至少一个主要指数的点位变化（点数和百分比）及股票池涨跌家数，不能只描述个股。
- signals 必须包含 2 到 5 只 quotes 中的股票且不重复；price 和 change_pct 必须原样复制，observation 说明看到的客观事实。
- decision_rationale 解释本轮每项决策；decisions 为空时说明原因。
格式必须为：
{"market_analysis":{"overview":"不超过180字的当前市场分析，必须包含大盘指数点位变化和涨跌家数","signals":[{"symbol":"__EXAMPLE_SYMBOL_1__","price":__EXAMPLE_PRICE_1__,"change_pct":-1.23,"observation":"从该股行情看到的事实"},{"symbol":"__EXAMPLE_SYMBOL_2__","price":__EXAMPLE_PRICE_2__,"change_pct":0.88,"observation":"从该股行情看到的事实"}],"decision_rationale":"不超过160字，说明本轮为何这样决策"},"decisions":[{"action":"BUY","symbol":"__EXAMPLE_SYMBOL_3__","quantity":__EXAMPLE_LOT__,"strategy_id":1,"reason":"不超过80字，说明为何选用该策略"}]}
action 只能是 BUY 或 SELL；不要返回 HOLD。"""


def build_system_prompt(profile: MarketProfile) -> str:
    if profile.t_plus_one:
        settlement_rule = "A 股 T+1：当天买入的股票当天不能卖出。"
    else:
        settlement_rule = "美股 T+0：当天买入的股票可以当天卖出。"
    lot_rule = f"买入数量必须是 {profile.lot_size} 股的整数倍。"
    examples = list(profile.stock_pool[:3])
    while len(examples) < 3:
        examples.append("AAPL" if profile.code == "US" else "600519")
    return (
        SYSTEM_PROMPT_TEMPLATE.replace("__MARKET_LABEL__", profile.label)
        .replace("__INDEX_NAMES__", profile.index_names)
        .replace("__SETTLEMENT_RULE__", settlement_rule)
        .replace("__LOT_RULE__", lot_rule)
        .replace("__EXAMPLE_SYMBOL_1__", examples[0])
        .replace("__EXAMPLE_SYMBOL_2__", examples[1])
        .replace("__EXAMPLE_SYMBOL_3__", examples[2])
        .replace("__EXAMPLE_PRICE_1__", "100.123" if profile.code == "US" else "1500.123")
        .replace("__EXAMPLE_PRICE_2__", "250.456")
        .replace("__EXAMPLE_LOT__", str(profile.lot_size))
    )


SYSTEM_PROMPT = build_system_prompt(PROFILE)


class TraderError(RuntimeError):
    """交易流程中的可预期错误。"""


class DecisionError(ValueError):
    """单条模型决策不合法。"""


@dataclass(frozen=True)
class Quote:
    market: str
    symbol: str
    name: str | None
    price: float
    change_pct: float | None
    open: float | None
    high: float | None
    low: float | None
    prev_close: float | None
    volume: float | None
    amount: float | None
    amplitude: float | None
    change_pct_5d: float | None
    change_pct_10d: float | None
    change_pct_20d: float | None
    change_pct_60d: float | None
    quoted_at: str
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    rsi14: float | None = None
    macd_dif: float | None = None
    macd_dea: float | None = None
    macd_hist: float | None = None
    atr14: float | None = None
    macd_dead_cross: bool | None = None
    distance_to_ma5: float | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "open": _round(self.open, 3),
            "high": _round(self.high, 3),
            "low": _round(self.low, 3),
            "close": _round(self.price, 3),
            "price": _round(self.price, 3),
            "prev_close": _round(self.prev_close, 3),
            "change_pct": _round(self.change_pct, 2),
            "amplitude": _round(self.amplitude, 2),
            "change_pct_5d": _round(self.change_pct_5d, 2),
            "change_pct_10d": _round(self.change_pct_10d, 2),
            "change_pct_20d": _round(self.change_pct_20d, 2),
            "change_pct_60d": _round(self.change_pct_60d, 2),
            "volume": _round(self.volume, 0),
            "amount": _round(self.amount, 0),
            "ma5": _round(self.ma5, 3),
            "ma10": _round(self.ma10, 3),
            "ma20": _round(self.ma20, 3),
            "rsi14": _round(self.rsi14, 2),
            "macd_dif": _round(self.macd_dif, 4),
            "macd_dea": _round(self.macd_dea, 4),
            "macd_hist": _round(self.macd_hist, 4),
            "macd_dead_cross": self.macd_dead_cross,
            "atr14": _round(self.atr14, 3),
            "distance_to_ma5": _round(self.distance_to_ma5, 2),
        }


@dataclass
class Holding:
    symbol: str
    name: str | None
    quantity: int
    avg_cost: float
    last_price: float | None


@dataclass(frozen=True)
class Decision:
    action: str
    symbol: str
    quantity: int
    reason: str
    strategy_id: int | None = None


@dataclass(frozen=True)
class Execution:
    action: str
    symbol: str
    name: str | None
    price: float
    quantity: int
    amount: float
    fee: float
    reason: str
    cash_after: float
    strategy_id: int | None = None


@dataclass
class PortfolioState:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def trade_date(profile: MarketProfile = PROFILE) -> str:
    return datetime.now(profile.market_timezone).date().isoformat()


def _refresh_trading_calendar(connection: sqlite3.Connection) -> None:
    """从新浪 A 股交易日历刷新本地缓存，覆盖完整日期范围。"""
    import akshare as ak
    import pandas as pd

    frame = ak.tool_trade_date_hist_sina()
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        raise TraderError("交易日历接口返回空数据")
    dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
    open_days = {item.date() for item in dates}
    if not open_days:
        raise TraderError("交易日历没有有效日期")

    start_day = min(open_days)
    end_day = max(open_days)
    if (end_day - start_day).days < 365 or len(open_days) < 1_000:
        raise TraderError("交易日历数据覆盖范围异常")

    refreshed_at = now_iso()
    rows = []
    current = start_day
    while current <= end_day:
        rows.append(
            (
                current.isoformat(),
                int(current in open_days),
                "sina",
                refreshed_at,
            )
        )
        current += timedelta(days=1)

    with connection:
        connection.execute("DELETE FROM trading_calendar")
        connection.executemany(UPSERT_TRADING_CALENDAR_SQL, rows)
    LOGGER.info(
        "交易日历已更新：%s 至 %s，%d 个交易日",
        start_day,
        end_day,
        len(open_days),
    )


def _refresh_us_trading_calendar(
    connection: sqlite3.Connection,
    profile: MarketProfile = get_profile("US"),
) -> None:
    """按纽约证券交易所常规休市规则生成本地交易日历缓存。"""
    today = date.today()
    start_day = today.replace(year=today.year - 2)
    end_day = today.replace(year=today.year + 3)
    refreshed_at = now_iso()
    rows = []
    current = start_day
    while current <= end_day:
        is_open = current.weekday() < 5 and not is_exchange_holiday(profile, current)
        rows.append(
            (
                current.isoformat(),
                int(is_open),
                "nyse_rules",
                refreshed_at,
            )
        )
        current += timedelta(days=1)
    with connection:
        connection.execute("DELETE FROM trading_calendar")
        connection.executemany(UPSERT_TRADING_CALENDAR_SQL, rows)
    LOGGER.info("美股交易日历已更新：%s 至 %s", start_day, end_day)


def is_trading_day(
    connection: sqlite3.Connection,
    day: str | None = None,
    profile: MarketProfile = PROFILE,
) -> bool:
    """判断指定日期是否为当前市场交易日；接口失败时回退到工作日规则。"""
    target_text = day or trade_date(profile)
    try:
        target = date.fromisoformat(target_text)
    except ValueError as exc:
        raise TraderError(f"无效日期: {target_text}") from exc
    if target.weekday() >= 5:
        return False
    if profile.code == "US" and is_exchange_holiday(profile, target):
        return False

    connection.execute(CREATE_TRADING_CALENDAR_SQL)
    row = connection.execute(
        "SELECT is_open FROM trading_calendar WHERE trade_date = ?",
        (target.isoformat(),),
    ).fetchone()
    if row is None:
        try:
            if profile.code == "CN":
                _refresh_trading_calendar(connection)
            else:
                _refresh_us_trading_calendar(connection, profile)
        except Exception as exc:
            LOGGER.warning("交易日历更新失败，回退到工作日判断: %s", exc)
        row = connection.execute(
            "SELECT is_open FROM trading_calendar WHERE trade_date = ?",
            (target.isoformat(),),
        ).fetchone()
    if row is not None:
        return bool(row[0])
    return target.weekday() < 5 and not is_exchange_holiday(profile, target)


def is_cn_trading_day(
    connection: sqlite3.Connection,
    day: str | None = None,
) -> bool:
    return is_trading_day(connection, day, get_profile("CN"))


def is_trading_day_for_db(
    db_path: Path,
    day: str | None = None,
    profile: MarketProfile = PROFILE,
) -> bool:
    """打开数据库并确保交易日历表已初始化。"""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        init_db(connection, profile=profile)
        return is_trading_day(connection, day, profile)
    finally:
        connection.close()


def is_cn_trading_day_for_db(db_path: Path, day: str | None = None) -> bool:
    return is_trading_day_for_db(db_path, day, get_profile("CN"))


def market_trading_session(
    current: datetime | None = None,
    profile: MarketProfile = PROFILE,
) -> tuple[str, str]:
    """返回当前市场交易时段标识和中文标签。"""
    return market_session(profile, current)


def _encode_param_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _decode_param_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def normalize_watchlist(
    value: Any, profile: MarketProfile = PROFILE
) -> list[dict[str, str]]:
    """校验并规范化复盘生成的 3-5 只明日观察股。"""
    if not isinstance(value, list):
        raise TraderError("watchlist 必须是数组")
    if len(value) > 5 or 0 < len(value) < 3:
        raise TraderError("watchlist 必须包含 3 到 5 只股票")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"symbol": item}
        if not isinstance(item, dict):
            raise TraderError("watchlist 每一项必须是对象")
        raw_symbol = item.get("symbol")
        try:
            symbol = normalize_market_symbol(profile, raw_symbol)
        except ValueError as exc:
            raise TraderError(f"watchlist 股票代码不合法: {raw_symbol!r}") from exc
        if symbol in seen:
            raise TraderError(f"watchlist 股票代码重复: {symbol}")
        seen.add(symbol)
        name = str(item.get("name") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if len(name) > 40:
            raise TraderError(f"watchlist 股票名称过长: {symbol}")
        if len(reason) > 100:
            raise TraderError(f"watchlist 观察理由过长: {symbol}")
        normalized.append(
            {"symbol": symbol, "name": name, "reason": reason}
        )
    return normalized


def normalize_strategy_param(
    name: str, value: Any, profile: MarketProfile = PROFILE
) -> Any:
    if not isinstance(name, str) or not STRATEGY_PARAM_NAME_RE.fullmatch(name):
        raise TraderError(f"非法参数名: {name!r}，只能使用小写 snake_case")
    if value is None:
        raise TraderError(f"参数 {name} 不能为 null")

    if name in {
        "max_single_position",
        "stop_loss_pct",
        "rsi_oversold",
        "rsi_overbought",
        "atr_stop_multiplier",
        "min_turnover",
        "market_regime_weight",
    }:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TraderError(f"参数 {name} 必须是数字")
        number = float(value)
        if name == "max_single_position" and not 0 < number <= 1:
            raise TraderError("max_single_position 必须满足 0 < value <= 1")
        if name == "stop_loss_pct" and not -1 < number < 0:
            raise TraderError("stop_loss_pct 必须满足 -1 < value < 0")
        if name in {"rsi_oversold", "rsi_overbought"} and not 0 <= number <= 100:
            raise TraderError(f"{name} 必须满足 0 <= value <= 100")
        if name == "atr_stop_multiplier" and not 0 < number <= 10:
            raise TraderError("atr_stop_multiplier 必须满足 0 < value <= 10")
        if name == "min_turnover" and not 0 <= number <= 1_000_000_000_000:
            raise TraderError("min_turnover 必须是合理范围内的非负数")
        if name == "market_regime_weight" and not 0 <= number <= 1:
            raise TraderError("market_regime_weight 必须满足 0 <= value <= 1")
        return round(number, 4)

    if name == "max_daily_trades":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TraderError("max_daily_trades 必须是整数")
        number = float(value)
        if not number.is_integer() or not 1 <= number <= 50:
            raise TraderError("max_daily_trades 必须是 1 到 50 之间的整数")
        return int(number)

    if name == "watchlist":
        return normalize_watchlist(value, profile=profile)

    if name == "custom_strategy_notes":
        if not isinstance(value, str):
            raise TraderError("custom_strategy_notes 必须是字符串")
        return value.strip()

    if not isinstance(value, (str, int, float, bool, list, dict)):
        raise TraderError(f"参数 {name} 的值必须是合法 JSON 类型")
    try:
        encoded = _encode_param_value(value)
    except (TypeError, ValueError) as exc:
        raise TraderError(f"参数 {name} 的值无法序列化: {exc}") from exc
    if len(encoded) > 2000:
        raise TraderError(f"参数 {name} 的值过长")
    return value


def _ensure_strategy_params_table(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(strategy_params)")
    }
    if not columns:
        connection.execute(CREATE_STRATEGY_PARAMS_SQL)
        return
    if "account_id" in columns:
        return

    with connection:
        connection.execute("ALTER TABLE strategy_params RENAME TO strategy_params_legacy")
        connection.execute(CREATE_STRATEGY_PARAMS_SQL)
        connection.execute(
            "INSERT OR REPLACE INTO strategy_params "
            "(account_id, param_name, param_value) "
            "SELECT 1, param_name, param_value FROM strategy_params_legacy"
        )
        connection.execute("DROP TABLE strategy_params_legacy")
    LOGGER.info("strategy_params 已迁移为按账户隔离的键值表")


def _ensure_positions_account_column(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(positions)")
    }
    if not columns:
        connection.execute(CREATE_POSITIONS_SQL)
        return
    if "account_id" in columns:
        return

    legacy_columns = list(
        str(row[1]) for row in connection.execute("PRAGMA table_info(positions)")
    )
    common = [
        name
        for name in legacy_columns
        if name
        in {
            "market",
            "symbol",
            "name",
            "quantity",
            "avg_cost",
            "last_price",
            "market_value",
            "position_pct",
            "account_pct",
            "updated_at",
        }
    ]
    quoted = ", ".join(common)
    with connection:
        connection.execute("ALTER TABLE positions RENAME TO positions_legacy")
        connection.execute(CREATE_POSITIONS_SQL)
        connection.execute(
            f"INSERT INTO positions (account_id, {quoted}) "
            f"SELECT 1, {quoted} FROM positions_legacy"
        )
        connection.execute("DROP TABLE positions_legacy")
    LOGGER.info("positions 已迁移为按账户隔离")


def _ensure_strategies_account_column(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(strategies)")
    }
    if not columns:
        connection.execute(CREATE_STRATEGIES_SQL)
        return
    table_sql = str(
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'strategies'"
        ).fetchone()[0]
    )
    if "account_id" in columns and "UNIQUE" not in table_sql.upper():
        return

    with connection:
        connection.execute("ALTER TABLE strategies RENAME TO strategies_legacy")
        connection.execute(CREATE_STRATEGIES_SQL)
        connection.execute(
            "INSERT INTO strategies ("
            "id, account_id, name, description, rules_json, status, source, "
            "usage_count, last_used_at, created_at, updated_at"
            ") SELECT id, 1, name, description, rules_json, status, source, "
            "usage_count, last_used_at, created_at, updated_at FROM strategies_legacy"
        )
        connection.execute("DROP TABLE strategies_legacy")
    LOGGER.info("strategies 已迁移为按账户隔离")


def _ensure_strategy_notes_account_column(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(strategy_notes)")
    }
    if not columns:
        connection.execute(CREATE_STRATEGY_NOTES_SQL)
        return
    if "account_id" in columns:
        return

    with connection:
        connection.execute("ALTER TABLE strategy_notes RENAME TO strategy_notes_legacy")
        connection.execute(CREATE_STRATEGY_NOTES_SQL)
        connection.execute(
            "INSERT INTO strategy_notes ("
            "id, account_id, trade_date, notes, source, created_at, updated_at"
            ") SELECT id, 1, trade_date, notes, source, created_at, updated_at "
            "FROM strategy_notes_legacy"
        )
        connection.execute("DROP TABLE strategy_notes_legacy")
    LOGGER.info("strategy_notes 已迁移为按账户隔离")


def _ensure_account_column(connection: sqlite3.Connection, table: str) -> None:
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if "account_id" not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1"
        )


def _ensure_position_metric_columns(
    connection: sqlite3.Connection, profile: MarketProfile = PROFILE
) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(positions)")
    }
    for column in ("position_pct", "account_pct"):
        if column not in columns:
            connection.execute(f"ALTER TABLE positions ADD COLUMN {column} REAL")

    rows = connection.execute(
        "SELECT account_id, market, symbol, market_value FROM positions "
        "WHERE market = ?",
        (profile.code,),
    ).fetchall()
    market_values: dict[int, float] = {}
    for row in rows:
        account_id = int(row[0])
        market_values[account_id] = market_values.get(account_id, 0.0) + float(
            row[3] or 0
        )
    with connection:
        for row in rows:
            account_id = int(row[0])
            market_value = float(row[3] or 0)
            total_market_value = market_values[account_id]
            account_row = connection.execute(
                "SELECT total_asset FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            total_asset = (
                float(account_row[0])
                if account_row is not None
                else total_market_value
            )
            position_pct = (
                market_value / total_market_value * 100
                if total_market_value > 0
                else 0.0
            )
            account_pct = (
                market_value / total_asset * 100 if total_asset > 0 else 0.0
            )
            connection.execute(
                "UPDATE positions SET position_pct = ?, account_pct = ? "
                "WHERE account_id = ? AND symbol = ?",
                (
                    round(position_pct, 4),
                    round(account_pct, 4),
                    account_id,
                    row[2],
                ),
            )


def _ensure_trade_strategy_column(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(trades)")
    }
    if "strategy_id" not in columns:
        connection.execute("ALTER TABLE trades ADD COLUMN strategy_id INTEGER")


def _ensure_daily_closes_table(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_DAILY_CLOSES_SQL)
    existing_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(daily_closes)")
    }
    for column in ("open", "high", "low", "volume"):
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE daily_closes ADD COLUMN {column} REAL"
            )


def _ensure_quote_indicator_columns(connection: sqlite3.Connection) -> None:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'quotes'"
    ).fetchone()
    if table_exists is None:
        return
    existing_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(quotes)")
    }
    for column, column_type in QUOTE_INDICATOR_COLUMNS:
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE quotes ADD COLUMN {column} {column_type}"
            )


def init_db(
    connection: sqlite3.Connection, profile: MarketProfile = PROFILE
) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    account_store.ensure_schema(connection, profile)
    connection.execute(CREATE_POSITIONS_SQL)
    _ensure_positions_account_column(connection)
    _ensure_position_metric_columns(connection, profile)
    connection.execute(CREATE_TRADES_SQL)
    _ensure_account_column(connection, "trades")
    _ensure_trade_strategy_column(connection)
    connection.execute(CREATE_STRATEGIES_SQL)
    _ensure_strategies_account_column(connection)
    connection.execute(CREATE_DECISION_ANALYSIS_SQL)
    _ensure_account_column(connection, "decision_analysis")
    connection.execute(CREATE_CHAT_MESSAGES_SQL)
    _ensure_account_column(connection, "chat_messages")
    _ensure_daily_closes_table(connection)
    _ensure_quote_indicator_columns(connection)
    connection.execute(CREATE_TRADING_CALENDAR_SQL)
    _ensure_strategy_params_table(connection)
    connection.execute(CREATE_STRATEGY_NOTES_SQL)
    _ensure_strategy_notes_account_column(connection)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_symbol_date "
        "ON trades(symbol, trade_date)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_traded_at ON trades(traded_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_analysis_created "
        "ON decision_analysis(created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_date_id "
        "ON chat_messages(trade_date, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_strategies_account_name "
        "ON strategies(account_id, name COLLATE NOCASE)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_account_date "
        "ON trades(account_id, trade_date)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_positions_account "
        "ON positions(account_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_account_date "
        "ON chat_messages(account_id, trade_date, id)"
    )
    with connection:
        timestamp = now_iso()
        accounts = connection.execute(
            "SELECT id FROM accounts WHERE market = ? ORDER BY id",
            (profile.code,),
        ).fetchall()
        for account in accounts:
            account_id = int(account[0])
            for name, value in DEFAULT_STRATEGY_PARAMS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO strategy_params "
                    "(account_id, param_name, param_value) VALUES (?, ?, ?)",
                    (account_id, name, _encode_param_value(value)),
                )
            # chat_insights 是旧的临时参数，聊天学习改为写入 strategies 表。
            connection.execute(
                "DELETE FROM strategy_params "
                "WHERE account_id = ? AND param_name = 'chat_insights'",
                (account_id,),
            )
            note_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM strategy_notes WHERE account_id = ?",
                    (account_id,),
                ).fetchone()[0]
            )
            if note_count == 0:
                legacy_note_row = connection.execute(
                    "SELECT param_value FROM strategy_params "
                    "WHERE account_id = ? AND param_name = 'custom_strategy_notes'",
                    (account_id,),
                ).fetchone()
                if legacy_note_row is not None:
                    legacy_note = str(
                        _decode_param_value(str(legacy_note_row[0])) or ""
                    ).strip()
                    if legacy_note:
                        connection.execute(
                            UPSERT_STRATEGY_NOTE_SQL,
                            (
                                account_id,
                                trade_date(profile),
                                legacy_note,
                                "legacy_migration",
                                timestamp,
                                timestamp,
                            ),
                        )
            strategy_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM strategies WHERE account_id = ?",
                    (account_id,),
                ).fetchone()[0]
            )
            if strategy_count == 0:
                connection.execute(
                    "INSERT INTO strategies "
                    "(account_id, name, description, rules_json, status, source, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', 'seed', ?, ?)",
                    (
                        account_id,
                        DEFAULT_STRATEGY_NAME,
                        DEFAULT_STRATEGY_DESCRIPTION,
                        _encode_param_value(DEFAULT_STRATEGY_RULES),
                        timestamp,
                        timestamp,
                    ),
                )


def refresh_quotes(
    db_path: Path, profile: MarketProfile = PROFILE
) -> bool:
    collector_path = Path(__file__).with_name("collector.py")
    command = [
        sys.executable,
        str(collector_path),
        "--db",
        str(db_path),
        "--market",
        profile.code,
        "--selected-only",
    ]
    try:
        result = subprocess.run(command, check=False)
    except OSError as exc:
        LOGGER.warning("%s行情更新失败: %s", profile.label, exc)
        return False
    return result.returncode == 0


def refresh_cn_quotes(db_path: Path) -> bool:
    return refresh_quotes(db_path, get_profile("CN"))


def resolve_account_id(
    connection: sqlite3.Connection,
    market: str,
    slug: str | None = None,
) -> int:
    return int(account_store.get_account(connection, market, slug)["id"])


def load_account(
    connection: sqlite3.Connection, account_id: int = 1
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise TraderError(f"账户不存在: {account_id}")
    return row


def load_positions(
    connection: sqlite3.Connection,
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM positions WHERE account_id = ? AND market = ? "
        "ORDER BY symbol",
        (account_id, profile.code),
    ).fetchall()


def load_strategy_params(
    connection: sqlite3.Connection, *, account_id: int = 1
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT param_name, param_value FROM strategy_params "
        "WHERE account_id = ? ORDER BY param_name",
        (account_id,),
    ).fetchall()
    if not rows:
        raise TraderError("strategy_params 表没有参数")
    return {
        str(row[0]): _decode_param_value(str(row[1]))
        for row in rows
    }


def save_strategy_params(
    connection: sqlite3.Connection,
    params: dict[str, Any],
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> None:
    normalized = {
        name: normalize_strategy_param(name, value, profile=profile)
        for name, value in params.items()
    }
    with connection:
        for name, value in normalized.items():
            connection.execute(
                UPSERT_STRATEGY_PARAM_SQL,
                (account_id, name, _encode_param_value(value)),
            )


def save_strategy_note(
    connection: sqlite3.Connection,
    note_day: str,
    notes: str,
    *,
    source: str = "daily_review",
    profile: MarketProfile = PROFILE,
    account_id: int = 1,
) -> None:
    normalized = normalize_strategy_param(
        "custom_strategy_notes", notes, profile=profile
    )
    timestamp = now_iso()
    with connection:
        connection.execute(
            UPSERT_STRATEGY_NOTE_SQL,
            (account_id, note_day, normalized, source, timestamp, timestamp),
        )


def load_strategy_notes(
    connection: sqlite3.Connection, *, account_id: int = 1
) -> list[dict[str, str]]:
    rows = connection.execute(
        "SELECT trade_date, notes, source, updated_at "
        "FROM strategy_notes WHERE account_id = ? ORDER BY trade_date, id",
        (account_id,),
    ).fetchall()
    return [
        {
            "trade_date": str(row[0]),
            "notes": str(row[1]),
            "source": str(row[2]),
            "updated_at": str(row[3]),
        }
        for row in rows
    ]


def _normalize_strategy_name(value: Any) -> str:
    if not isinstance(value, str):
        raise TraderError("策略名称必须是字符串")
    name = value.strip()
    if not 2 <= len(name) <= 40:
        raise TraderError("策略名称长度必须是 2 到 40 个字符")
    return name


def _normalize_strategy_description(value: Any) -> str:
    if not isinstance(value, str):
        raise TraderError("策略说明必须是字符串")
    description = value.strip()
    if not 5 <= len(description) <= 300:
        raise TraderError("策略说明长度必须是 5 到 300 个字符")
    return description


def _normalize_strategy_status(value: Any) -> str:
    status = str(value or "active").strip().lower()
    if status not in STRATEGY_STATUSES:
        raise TraderError("策略状态必须是 active、paused 或 retired")
    return status


def normalize_strategy_rules(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise TraderError("策略规则必须是非空 JSON 对象")
    try:
        encoded = _encode_param_value(value)
    except (TypeError, ValueError) as exc:
        raise TraderError(f"策略规则无法序列化: {exc}") from exc
    if len(encoded) > 4000:
        raise TraderError("策略规则内容过长")
    return value


def _strategy_row_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        rules = json.loads(row["rules_json"])
    except (json.JSONDecodeError, TypeError):
        rules = {}
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"]),
        "rules": rules,
        "status": str(row["status"]),
        "source": str(row["source"]),
        "usage_count": int(row["usage_count"]),
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def load_strategies(
    connection: sqlite3.Connection,
    *,
    active_only: bool = False,
    account_id: int = 1,
) -> list[dict[str, Any]]:
    where = " AND status = 'active'" if active_only else ""
    rows = connection.execute(
        "SELECT id, name, description, rules_json, status, source, usage_count, "
        "last_used_at, created_at, updated_at FROM strategies WHERE account_id = ?"
        + where
        + " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, id",
        (account_id,),
    ).fetchall()
    return [_strategy_row_dict(row) for row in rows]


def _parse_strategy_id(value: Any) -> int:
    if isinstance(value, bool):
        raise TraderError("strategy_id 必须是整数")
    if isinstance(value, int):
        strategy_id = value
    elif isinstance(value, str) and value.strip().isdigit():
        strategy_id = int(value.strip())
    else:
        raise TraderError("strategy_id 必须是整数")
    if strategy_id <= 0:
        raise TraderError("strategy_id 必须大于 0")
    return strategy_id


def apply_strategy_changes(
    connection: sqlite3.Connection,
    changes: list[dict[str, Any]],
    *,
    source: str = "daily_review",
    timestamp: str | None = None,
    account_id: int = 1,
) -> list[dict[str, Any]]:
    """在一个事务中应用复盘生成的策略增删改。"""
    if not isinstance(changes, list):
        raise TraderError("strategy_changes 必须是数组")
    if len(changes) > MAX_STRATEGY_CHANGES:
        raise TraderError(f"单次最多修改 {MAX_STRATEGY_CHANGES} 个策略")
    changed_at = timestamp or now_iso()
    source_text = str(source or "daily_review")[:40]
    active_before = int(
        connection.execute(
            "SELECT COUNT(*) FROM strategies "
            "WHERE account_id = ? AND status = 'active'",
            (account_id,),
        ).fetchone()[0]
    )
    results: list[dict[str, Any]] = []
    with connection:
        for index, change in enumerate(changes, start=1):
            if not isinstance(change, dict):
                raise TraderError(f"strategy_changes[{index}] 必须是对象")
            action = str(change.get("action") or "").strip().lower()
            if action == "create":
                name = _normalize_strategy_name(change.get("name"))
                description = _normalize_strategy_description(
                    change.get("description")
                )
                rules = normalize_strategy_rules(change.get("rules"))
                status = _normalize_strategy_status(change.get("status", "active"))
                duplicate = connection.execute(
                    "SELECT id FROM strategies "
                    "WHERE account_id = ? AND name = ? COLLATE NOCASE",
                    (account_id, name),
                ).fetchone()
                if duplicate is not None:
                    raise TraderError(f"策略名称已存在: {name}")
                cursor = connection.execute(
                    "INSERT INTO strategies "
                    "(account_id, name, description, rules_json, status, source, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        account_id,
                        name,
                        description,
                        _encode_param_value(rules),
                        status,
                        source_text,
                        changed_at,
                        changed_at,
                    ),
                )
                if status == "active":
                    active_before += 1
                results.append({"action": "create", "id": int(cursor.lastrowid), "name": name})
                continue

            strategy_id = _parse_strategy_id(change.get("id"))
            row = connection.execute(
                "SELECT * FROM strategies WHERE id = ? AND account_id = ?",
                (strategy_id, account_id),
            ).fetchone()
            if row is None:
                raise TraderError(f"策略不存在: {strategy_id}")

            if action == "retire":
                if row["status"] == "active" and active_before <= 1:
                    raise TraderError("至少必须保留一个 active 策略")
                connection.execute(
                    "UPDATE strategies SET status = 'retired', updated_at = ? "
                    "WHERE id = ? AND account_id = ?",
                    (changed_at, strategy_id, account_id),
                )
                if row["status"] == "active":
                    active_before -= 1
                results.append({"action": "retire", "id": strategy_id})
                continue

            if action != "update":
                raise TraderError(f"不支持的策略操作: {action!r}")

            name = (
                _normalize_strategy_name(change["name"])
                if "name" in change
                else str(row["name"])
            )
            description = (
                _normalize_strategy_description(change["description"])
                if "description" in change
                else str(row["description"])
            )
            rules = (
                normalize_strategy_rules(change["rules"])
                if "rules" in change
                else json.loads(row["rules_json"])
            )
            status = (
                _normalize_strategy_status(change["status"])
                if "status" in change
                else str(row["status"])
            )
            if status != row["status"]:
                if status == "active":
                    active_before += 1
                elif row["status"] == "active":
                    if active_before <= 1:
                        raise TraderError("至少必须保留一个 active 策略")
                    active_before -= 1
            if name.lower() != str(row["name"]).lower():
                duplicate = connection.execute(
                    "SELECT id FROM strategies WHERE account_id = ? "
                    "AND name = ? COLLATE NOCASE AND id != ?",
                    (account_id, name, strategy_id),
                ).fetchone()
                if duplicate is not None:
                    raise TraderError(f"策略名称已存在: {name}")
            connection.execute(
                "UPDATE strategies SET name = ?, description = ?, rules_json = ?, "
                "status = ?, source = ?, updated_at = ? "
                "WHERE id = ? AND account_id = ?",
                (
                    name,
                    description,
                    _encode_param_value(rules),
                    status,
                    source_text,
                    changed_at,
                    strategy_id,
                    account_id,
                ),
            )
            results.append({"action": "update", "id": strategy_id, "name": name})

        if active_before <= 0:
            raise TraderError("至少必须保留一个 active 策略")
    return results


def load_today_trade_count(
    connection: sqlite3.Connection,
    day: str,
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM trades WHERE account_id = ? "
        "AND trade_date = ? AND market = ?",
        (account_id, day, profile.code),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def load_bought_today(
    connection: sqlite3.Connection,
    day: str,
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> dict[str, int]:
    rows = connection.execute(
        "SELECT symbol, COALESCE(SUM(quantity), 0) AS quantity "
        "FROM trades WHERE account_id = ? AND trade_date = ? "
        "AND market = ? AND side = 'BUY' "
        "GROUP BY symbol",
        (account_id, day, profile.code),
    ).fetchall()
    return {str(row["symbol"]): int(row["quantity"]) for row in rows}


def generate_stop_loss_decisions(
    state: PortfolioState,
    quotes: dict[str, Quote],
    stop_loss_pct: float,
    bought_today: dict[str, int] | None = None,
    profile: MarketProfile = PROFILE,
) -> tuple[list[Decision], list[str]]:
    """按成本价和最新价生成 Python 强制止损单，不经过大模型。"""
    bought_today = bought_today or {}
    decisions: list[Decision] = []
    warnings: list[str] = []

    for holding in sorted(state.holdings.values(), key=lambda item: item.symbol):
        if holding.avg_cost <= 0:
            warnings.append(
                f"止损检查跳过 {holding.symbol}: 持仓成本无效"
            )
            continue
        quote = quotes.get(holding.symbol)
        if quote is None or quote.price <= 0:
            warnings.append(
                f"止损检查跳过 {holding.symbol}: 缺少有效最新价"
            )
            continue

        loss_pct = quote.price / holding.avg_cost - 1
        if loss_pct > stop_loss_pct + 1e-12:
            continue

        unsellable_today = (
            bought_today.get(holding.symbol, 0) if profile.t_plus_one else 0
        )
        sellable = holding.quantity - unsellable_today
        if sellable <= 0:
            warnings.append(
                f"强制止损 {holding.symbol} 暂不可执行: 今日买入受结算限制"
            )
            continue
        if sellable < holding.quantity:
            warnings.append(
                f"强制止损 {holding.symbol} 仅卖出可卖 {sellable} 股，"
                f"{holding.quantity - sellable} 股今日买入受结算限制"
            )

        decisions.append(
            Decision(
                action="SELL",
                symbol=holding.symbol,
                quantity=sellable,
                reason=(
                    f"Python强制止损：浮亏 {loss_pct * 100:.2f}% 已触发 "
                    f"stop_loss_pct {stop_loss_pct * 100:.2f}%"
                ),
            )
        )

    return decisions, warnings


def quote_market_day(
    quoted_at: str | None, profile: MarketProfile = PROFILE
) -> str | None:
    if not quoted_at:
        return None
    try:
        timestamp = datetime.fromisoformat(quoted_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(profile.market_timezone).date().isoformat()


def _tencent_symbol(symbol: str) -> str | None:
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


def _fetch_daily_bars(
    symbol: str, day: str, profile: MarketProfile = PROFILE
) -> list[tuple[str, float | None, float | None, float | None, float, float | None]]:
    import akshare as ak
    import pandas as pd

    end_date = date.fromisoformat(day)
    start_date = end_date - timedelta(days=HISTORY_LOOKBACK_DAYS)
    if profile.code == "CN":
        tencent_symbol = _tencent_symbol(symbol)
        if tencent_symbol is None:
            return []
        frame = ak.stock_zh_a_hist_tx(
            symbol=tencent_symbol,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adjust="qfq",
            timeout=15,
        )
    else:
        frame = ak.stock_us_daily(symbol=symbol, adjust="qfq")
        if frame is not None and not frame.empty:
            frame = frame.reset_index()
    if frame is None or frame.empty:
        return []

    dates = pd.to_datetime(frame["date"], errors="coerce")
    opens = pd.to_numeric(frame["open"], errors="coerce")
    highs = pd.to_numeric(frame["high"], errors="coerce")
    lows = pd.to_numeric(frame["low"], errors="coerce")
    closes = pd.to_numeric(frame["close"], errors="coerce")
    volumes = pd.to_numeric(frame["volume"], errors="coerce")
    rows = []
    for row_date, open_price, high, low, close, volume in zip(
        dates, opens, highs, lows, closes, volumes
    ):
        if pd.isna(row_date) or pd.isna(close) or float(close) <= 0:
            continue
        row_day = row_date.date()
        if not start_date <= row_day <= end_date:
            continue
        if not _is_market_trading_date(profile, row_day):
            continue
        rows.append(
            (
                row_day.isoformat(),
                float(open_price) if not pd.isna(open_price) else None,
                float(high) if not pd.isna(high) else None,
                float(low) if not pd.isna(low) else None,
                float(close),
                float(volume) if not pd.isna(volume) else None,
            )
        )
    return rows


def _is_market_trading_date(profile: MarketProfile, day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if profile.code == "US" and is_exchange_holiday(profile, day):
        return False
    return True


def _prune_invalid_daily_bars(
    connection: sqlite3.Connection,
    profile: MarketProfile = PROFILE,
) -> None:
    rows = connection.execute(
        "SELECT DISTINCT trade_date FROM daily_closes WHERE market = ?",
        (profile.code,),
    ).fetchall()
    invalid_dates: list[str] = []
    for row in rows:
        raw_day = str(row[0])
        try:
            parsed_day = date.fromisoformat(raw_day)
        except ValueError:
            invalid_dates.append(raw_day)
            continue
        if not _is_market_trading_date(profile, parsed_day):
            invalid_dates.append(raw_day)
    if not invalid_dates:
        return
    with connection:
        connection.executemany(
            "DELETE FROM daily_closes WHERE market = ? AND trade_date = ?",
            [(profile.code, item) for item in invalid_dates],
        )
    LOGGER.info("已清理 %d 个非交易日日线日期", len(invalid_dates))


def _refresh_daily_closes(
    connection: sqlite3.Connection,
    quotes: list[Quote],
    day: str,
    profile: MarketProfile = PROFILE,
) -> None:
    if not quotes:
        return
    _prune_invalid_daily_bars(connection, profile)
    symbols = sorted({quote.symbol for quote in quotes})
    cached_rows = connection.execute(
        "SELECT symbol, COUNT(*) AS count, MAX(trade_date) AS latest "
        "FROM daily_closes WHERE market = ? AND trade_date < ? "
        "AND open IS NOT NULL AND high IS NOT NULL "
        "AND low IS NOT NULL AND close IS NOT NULL "
        "GROUP BY symbol",
        (profile.code, day),
    ).fetchall()
    cached = {
        str(row[0]): (int(row[1]), str(row[2]) if row[2] else None)
        for row in cached_rows
    }
    stale_before = (
        date.fromisoformat(day) - timedelta(days=HISTORY_STALE_DAYS)
    ).isoformat()
    missing = [
        symbol
        for symbol in symbols
        if cached.get(symbol, (0, None))[0] < MIN_HISTORY_BARS
        or cached.get(symbol, (0, None))[1] is None
        or cached[symbol][1] < stale_before
    ]
    if not missing:
        return

    LOGGER.info("补拉 %d 只股票的 OHLCV 日线历史用于量化指标", len(missing))
    fetched: dict[
        str, list[tuple[str, float | None, float | None, float | None, float, float | None]]
    ] = {}
    with ThreadPoolExecutor(max_workers=min(HISTORY_WORKERS, len(missing))) as executor:
        futures = {
            executor.submit(_fetch_daily_bars, symbol, day, profile): symbol
            for symbol in missing
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                fetched[symbol] = future.result()
            except Exception as exc:
                LOGGER.warning("%s 日线历史获取失败，量化指标将缺失: %s", symbol, exc)

    rows = [
        (profile.code, symbol, trade_day, open_price, high, low, close, volume)
        for symbol, values in fetched.items()
        for trade_day, open_price, high, low, close, volume in values
    ]
    if rows:
        with connection:
            connection.executemany(UPSERT_DAILY_BAR_SQL, rows)


def _latest_float(series: Any) -> float | None:
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def compute_technical_indicators(
    connection: sqlite3.Connection,
    quotes: list[Quote],
    day: str,
    profile: MarketProfile = PROFILE,
) -> dict[str, dict[str, Any]]:
    import pandas as pd

    _refresh_daily_closes(connection, quotes, day, profile)
    indicators: dict[str, dict[str, Any]] = {}
    for quote in quotes:
        history = connection.execute(
            "SELECT trade_date, open, high, low, close, volume "
            "FROM daily_closes "
            "WHERE market = ? AND symbol = ? AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 120",
            (profile.code, quote.symbol, day),
        ).fetchall()
        history_rows = [
            {
                "trade_date": str(row[0]),
                "open": float(row[1]) if row[1] is not None else None,
                "high": float(row[2]) if row[2] is not None else None,
                "low": float(row[3]) if row[3] is not None else None,
                "close": float(row[4]) if row[4] is not None else None,
                "volume": float(row[5]) if row[5] is not None else None,
            }
            for row in history
            if row[4] is not None and float(row[4]) > 0 and str(row[0]) != day
        ]

        today_open = quote.open if quote.open is not None else quote.price
        today_close = quote.price
        today_high = (
            quote.high
            if quote.high is not None
            else max(today_open, today_close)
        )
        today_low = (
            quote.low
            if quote.low is not None
            else min(today_open, today_close)
        )
        today_high = max(today_high, today_open, today_close)
        today_low = min(today_low, today_open, today_close)
        history_rows.append(
            {
                "trade_date": day,
                "open": today_open,
                "high": today_high,
                "low": today_low,
                "close": today_close,
                "volume": quote.volume,
            }
        )

        frame = pd.DataFrame(history_rows)
        frame = frame.sort_values("trade_date").drop_duplicates(
            "trade_date", keep="last"
        )
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame[frame["close"] > 0].reset_index(drop=True)
        close = frame["close"].astype("float64")
        high = frame["high"].astype("float64")
        low = frame["low"].astype("float64")

        ma5 = _latest_float(close.rolling(5).mean()) if len(close) >= 5 else None
        ma10 = _latest_float(close.rolling(10).mean()) if len(close) >= 10 else None
        ma20 = _latest_float(close.rolling(20).mean()) if len(close) >= 20 else None
        distance_to_ma5 = (
            (quote.price / ma5 - 1) * 100 if ma5 is not None and ma5 > 0 else None
        )

        delta = close.diff()
        average_gain = (
            delta.clip(lower=0)
            .ewm(alpha=1 / 14, adjust=False, min_periods=14)
            .mean()
        )
        average_loss = (
            (-delta.clip(upper=0))
            .ewm(alpha=1 / 14, adjust=False, min_periods=14)
            .mean()
        )
        rsi14 = None
        if len(close) >= 15:
            latest_gain = _latest_float(average_gain)
            latest_loss = _latest_float(average_loss)
            if latest_gain is not None and latest_loss is not None:
                if latest_loss == 0:
                    rsi14 = 100.0 if latest_gain > 0 else 50.0
                else:
                    relative_strength = latest_gain / latest_loss
                    rsi14 = 100 - 100 / (1 + relative_strength)

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif_series = ema12 - ema26
        dea_series = dif_series.ewm(span=9, adjust=False).mean()
        hist_series = (dif_series - dea_series) * 2
        macd_dif = macd_dea = macd_hist = None
        macd_dead_cross = None
        if len(close) >= MIN_HISTORY_BARS:
            macd_dif = _latest_float(dif_series)
            macd_dea = _latest_float(dea_series)
            macd_hist = _latest_float(hist_series)
            previous_dif = float(dif_series.iloc[-2])
            previous_dea = float(dea_series.iloc[-2])
            macd_dead_cross = bool(
                previous_dif >= previous_dea and macd_dif < macd_dea
            )

        atr14 = None
        if len(close) >= 15:
            previous_close = close.shift(1)
            true_range = pd.concat(
                [
                    high - low,
                    (high - previous_close).abs(),
                    (low - previous_close).abs(),
                ],
                axis=1,
            ).max(axis=1)
            atr_series = true_range.ewm(
                alpha=1 / 14, adjust=False, min_periods=14
            ).mean()
            atr14 = _latest_float(atr_series)

        indicator = {
            "ma5": round(ma5, 3) if ma5 is not None else None,
            "ma10": round(ma10, 3) if ma10 is not None else None,
            "ma20": round(ma20, 3) if ma20 is not None else None,
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "macd_dif": round(macd_dif, 4) if macd_dif is not None else None,
            "macd_dea": round(macd_dea, 4) if macd_dea is not None else None,
            "macd_hist": round(macd_hist, 4) if macd_hist is not None else None,
            "atr14": round(atr14, 3) if atr14 is not None else None,
            "macd_dead_cross": macd_dead_cross,
            "distance_to_ma5": (
                round(distance_to_ma5, 2)
                if distance_to_ma5 is not None
                else None
            ),
        }
        indicators[quote.symbol] = indicator
        connection.execute(
            UPDATE_QUOTE_INDICATORS_SQL,
            (
                indicator["ma5"],
                indicator["ma10"],
                indicator["ma20"],
                indicator["rsi14"],
                indicator["macd_dif"],
                indicator["macd_dea"],
                indicator["macd_hist"],
                indicator["atr14"],
                quote.market,
                quote.symbol,
            ),
        )

    if quotes:
        with connection:
            connection.executemany(
                UPSERT_DAILY_BAR_SQL,
                [
                    (
                        profile.code,
                        quote.symbol,
                        day,
                        (
                            quote.open
                            if quote.open is not None
                            else quote.price
                        ),
                        (
                            quote.high
                            if quote.high is not None
                            else max(quote.price, quote.open or quote.price)
                        ),
                        (
                            quote.low
                            if quote.low is not None
                            else min(quote.price, quote.open or quote.price)
                        ),
                        quote.price,
                        quote.volume,
                    )
                    for quote in quotes
                ],
            )
            connection.commit()
    return indicators


def load_latest_quotes(
    connection: sqlite3.Connection,
    top_n: int,
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> tuple[list[Quote], dict[str, Quote]]:
    account = load_account(connection, account_id)
    pool_rows = account_store.load_stock_pool(connection, account_id)
    pool_symbols = {str(row["symbol"]) for row in pool_rows}
    pool_mode = str(account["pool_mode"])
    if pool_mode == "affordable":
        if not pool_rows:
            raise TraderError(
                f"账户 {account['display_name']} 的动态股票池为空，请先运行 collector.py"
            )
        refreshed_at = max(str(row["refreshed_at"]) for row in pool_rows)
    else:
        refreshed_at = None
        if not pool_symbols:
            pool_symbols = set(profile.stock_pool)

    latest = connection.execute(
        "SELECT MAX(quoted_at) FROM quotes WHERE market = ? AND price > 0",
        (profile.code,),
    ).fetchone()[0]
    if latest is None:
        raise TraderError(
            f"quotes 表没有可用的{profile.label}行情，请先运行 collector.py"
        )
    if pool_mode == "affordable" and refreshed_at != latest:
        raise TraderError(
            f"账户 {account['display_name']} 的股票池未随最新行情刷新，跳过交易"
        )

    rows = connection.execute(
        "SELECT market, symbol, name, price, change_pct, open, high, low, "
        "prev_close, volume, amount, amplitude, change_pct_5d, "
        "change_pct_10d, change_pct_20d, change_pct_60d, quoted_at "
        "FROM quotes WHERE market = ? AND quoted_at = ? "
        "AND price IS NOT NULL AND price > 0 "
        "ORDER BY COALESCE(amount, 0) DESC, symbol",
        (profile.code, latest),
    ).fetchall()
    if not rows:
        raise TraderError(f"最新{profile.label}行情为空")

    raw_quotes = [
        Quote(
            market=row["market"],
            symbol=row["symbol"],
            name=row["name"],
            price=float(row["price"]),
            change_pct=row["change_pct"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            prev_close=row["prev_close"],
            volume=row["volume"],
            amount=row["amount"],
            amplitude=row["amplitude"],
            change_pct_5d=row["change_pct_5d"],
            change_pct_10d=row["change_pct_10d"],
            change_pct_20d=row["change_pct_20d"],
            change_pct_60d=row["change_pct_60d"],
            quoted_at=row["quoted_at"],
        )
        for row in rows
    ]

    raw_by_symbol = {quote.symbol: quote for quote in raw_quotes}
    pool_quotes = [
        quote for quote in raw_quotes if quote.symbol in pool_symbols
    ]
    if not pool_quotes:
        raise TraderError(f"账户 {account['display_name']} 的股票池没有最新行情")
    positions = {
        row["symbol"]
        for row in load_positions(connection, profile, account_id=account_id)
    }
    selected = pool_quotes[:top_n]
    selected_symbols = {quote.symbol for quote in selected}
    for symbol in sorted(positions):
        quote = raw_by_symbol.get(symbol)
        if quote is not None and symbol not in selected_symbols:
            selected.append(quote)
            selected_symbols.add(symbol)
    quotes_by_symbol = {
        quote.symbol: quote
        for quote in pool_quotes
        if quote.symbol in selected_symbols or quote.symbol in pool_symbols
    }
    for symbol in positions:
        quote = raw_by_symbol.get(symbol)
        if quote is not None:
            quotes_by_symbol[symbol] = quote
    return selected, quotes_by_symbol


def load_market_indices(
    connection: sqlite3.Connection, profile: MarketProfile = PROFILE
) -> list[dict[str, Any]]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'market_indices'"
    ).fetchone()
    if table is None:
        return []
    rows = connection.execute(
        "SELECT market, symbol, name, price, change_amount, change_pct, "
        "open, high, low, prev_close, quoted_at FROM market_indices "
        "WHERE market = ?",
        (profile.code,),
    ).fetchall()
    indices = [
        {
            "market": str(row["market"]),
            "symbol": str(row["symbol"]),
            "name": row["name"],
            "price": row["price"],
            "change_amount": row["change_amount"],
            "change_pct": row["change_pct"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "prev_close": row["prev_close"],
            "quoted_at": row["quoted_at"],
        }
        for row in rows
    ]
    indices.sort(key=lambda item: profile.index_order.get(item["symbol"], 99))
    return indices


def refresh_quotes_for_execution(
    connection: sqlite3.Connection,
    db_path: Path,
    quotes_by_symbol: dict[str, Quote],
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> dict[str, Quote] | None:
    """成交前重新采集一次行情，确保成交价是执行瞬间的最新价。"""
    if not refresh_quotes(db_path, profile):
        return None
    _, latest_by_symbol = load_latest_quotes(
        connection, 10_000, profile, account_id=account_id
    )
    refreshed: dict[str, Quote] = {}
    for symbol, quote in quotes_by_symbol.items():
        latest = latest_by_symbol.get(symbol)
        if latest is None:
            LOGGER.warning("成交前刷新缺少 %s 的最新行情，已从本次可成交列表移除", symbol)
            continue
        refreshed[symbol] = replace(
            quote,
            price=latest.price,
            change_pct=latest.change_pct,
            open=latest.open,
            high=latest.high,
            low=latest.low,
            prev_close=latest.prev_close,
            volume=latest.volume,
            amount=latest.amount,
            amplitude=latest.amplitude,
            change_pct_5d=latest.change_pct_5d,
            change_pct_10d=latest.change_pct_10d,
            change_pct_20d=latest.change_pct_20d,
            change_pct_60d=latest.change_pct_60d,
            quoted_at=latest.quoted_at,
        )
    return refreshed or None


def load_state(
    connection: sqlite3.Connection,
    quotes: dict[str, Quote],
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> PortfolioState:
    account = load_account(connection, account_id)
    holdings: dict[str, Holding] = {}
    for row in load_positions(connection, profile, account_id=account_id):
        symbol = str(row["symbol"])
        quote = quotes.get(symbol)
        holdings[symbol] = Holding(
            symbol=symbol,
            name=row["name"],
            quantity=int(row["quantity"]),
            avg_cost=float(row["avg_cost"]),
            last_price=(
                quote.price
                if quote is not None
                else (float(row["last_price"]) if row["last_price"] is not None else None)
            ),
        )
    return PortfolioState(cash=float(account["cash"]), holdings=holdings)


def portfolio_values(state: PortfolioState, quotes: dict[str, Quote]) -> tuple[float, float]:
    market_value = 0.0
    for holding in state.holdings.values():
        quote = quotes.get(holding.symbol)
        price = (
            quote.price
            if quote is not None
            else (holding.last_price or holding.avg_cost)
        )
        market_value += price * holding.quantity
    return market_value, state.cash + market_value


def build_market_snapshot(
    quotes: list[Quote],
    market_indices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    changes = [
        float(quote.change_pct)
        for quote in quotes
        if quote.change_pct is not None
    ]
    average_change = sum(changes) / len(changes) if changes else None
    advancers = sum(value > 0 for value in changes)
    decliners = sum(value < 0 for value in changes)
    flat = sum(value == 0 for value in changes)

    indices = []
    index_changes = []
    for item in market_indices or []:
        price = float(item.get("price") or 0)
        if price <= 0:
            continue
        change_amount = (
            float(item["change_amount"])
            if item.get("change_amount") is not None
            else None
        )
        change_pct = (
            float(item["change_pct"])
            if item.get("change_pct") is not None
            else None
        )
        if change_pct is not None:
            index_changes.append(change_pct)
        indices.append(
            {
                "symbol": str(item.get("symbol") or ""),
                "name": str(item.get("name") or ""),
                "price": round(price, 2),
                "change_amount": _round(change_amount, 2),
                "change_pct": _round(change_pct, 2),
                "quoted_at": item.get("quoted_at"),
            }
        )

    average_index_change = (
        sum(index_changes) / len(index_changes) if index_changes else None
    )
    breadth_up = advancers / len(changes) if changes else 0.0
    breadth_down = decliners / len(changes) if changes else 0.0
    if average_index_change is None:
        market_regime = "数据不足"
    elif average_index_change >= 1.0 and breadth_up >= 0.65:
        market_regime = "普涨"
    elif average_index_change >= 0.3:
        market_regime = "偏强"
    elif average_index_change <= -1.0 and breadth_down >= 0.65:
        market_regime = "普跌"
    elif average_index_change <= -0.3:
        market_regime = "偏弱"
    else:
        market_regime = "震荡"

    index_parts = []
    for item in indices:
        points = item["change_amount"]
        pct = item["change_pct"]
        if points is None and pct is None:
            continue
        if points is None:
            movement = f"{pct:+.2f}%"
        elif points > 0:
            movement = f"上涨{points:.2f}点（{pct:+.2f}%）" if pct is not None else f"上涨{points:.2f}点"
        elif points < 0:
            movement = f"下跌{abs(points):.2f}点（{pct:+.2f}%）" if pct is not None else f"下跌{abs(points):.2f}点"
        else:
            movement = f"持平（{pct:+.2f}%）" if pct is not None else "持平"
        index_parts.append(f"{item['name']}{item['price']:.2f}点，{movement}")
    breadth_summary = (
        f"股票池{len(changes)}只：{advancers}涨{decliners}跌{flat}平"
    )
    if average_change is not None:
        breadth_summary += f"，平均{average_change:+.2f}%"
    market_summary = "；".join(index_parts + [breadth_summary])

    def quote_brief(quote: Quote) -> dict[str, Any]:
        return {
            "symbol": quote.symbol,
            "name": quote.name,
            "price": _round(quote.price, 3),
            "change_pct": _round(quote.change_pct, 2),
        }

    ranked = sorted(
        (quote for quote in quotes if quote.change_pct is not None),
        key=lambda quote: float(quote.change_pct),
        reverse=True,
    )
    return {
        "as_of": quotes[0].quoted_at if quotes else None,
        "quote_count": len(quotes),
        "advancers": advancers,
        "decliners": decliners,
        "flat": flat,
        "average_change_pct": _round(average_change, 2),
        "indices": indices,
        "index_average_change_pct": _round(average_index_change, 2),
        "market_regime": market_regime,
        "market_summary": market_summary,
        "top_gainers": [quote_brief(quote) for quote in ranked[:3]],
        "top_losers": [quote_brief(quote) for quote in ranked[-3:][::-1]],
    }


def build_prompt(
    state: PortfolioState,
    quotes: list[Quote],
    quotes_by_symbol: dict[str, Quote],
    day: str,
    strategy_params: dict[str, Any],
    strategies: list[dict[str, Any]] | None = None,
    strategy_notes: list[dict[str, str]] | None = None,
    force_test: bool = False,
    today_trade_count: int = 0,
    market_snapshot: dict[str, Any] | None = None,
    analysis_session: str | None = None,
    profile: MarketProfile = PROFILE,
) -> str:
    market_value, total_asset = portfolio_values(state, quotes_by_symbol)
    max_daily_trades = strategy_params.get("max_daily_trades")
    remaining_daily_trades = (
        max(0, int(max_daily_trades) - today_trade_count)
        if isinstance(max_daily_trades, (int, float))
        and not isinstance(max_daily_trades, bool)
        else None
    )
    try:
        rsi_oversold = float(strategy_params.get("rsi_oversold", 45))
    except (TypeError, ValueError):
        rsi_oversold = 45.0
    try:
        atr_stop_multiplier = float(
            strategy_params.get("atr_stop_multiplier", 5.0)
        )
    except (TypeError, ValueError):
        atr_stop_multiplier = 5.0

    quote_features = []
    for quote in quotes:
        feature = quote.as_prompt_dict()
        rsi_below_oversold = (
            quote.rsi14 < rsi_oversold if quote.rsi14 is not None else None
        )
        above_ma20 = (
            quote.price > quote.ma20 if quote.ma20 is not None else None
        )
        below_ma20 = (
            quote.price < quote.ma20 if quote.ma20 is not None else None
        )
        feature["rsi_below_oversold"] = rsi_below_oversold
        feature["above_ma20"] = above_ma20
        feature["below_ma20"] = below_ma20
        feature["oversold_rebound_candidate"] = (
            bool(rsi_below_oversold and above_ma20)
            if rsi_below_oversold is not None and above_ma20 is not None
            else None
        )
        feature["atr_stop_price"] = (
            round(quote.price - quote.atr14 * atr_stop_multiplier, 3)
            if quote.atr14 is not None
            else None
        )
        quote_features.append(feature)

    prompt_strategy_params = {
        name: value
        for name, value in strategy_params.items()
        if name not in {"custom_strategy_notes", "watchlist"}
    }
    snapshot = {
        "trade_date": day,
        "as_of": quotes[0].quoted_at if quotes else None,
        "analysis_session": analysis_session or market_trading_session(profile=profile)[1],
        "risk_mode": "aggressive",
        "execution_mode": "live_intraday_latest_price",
        "market_snapshot": market_snapshot or build_market_snapshot(quotes),
        "strategy_params": prompt_strategy_params,
        "available_strategies": [
            {
                "id": int(strategy["id"]),
                "name": strategy["name"],
                "description": strategy["description"],
                "rules": strategy.get("rules", {}),
            }
            for strategy in (strategies or [])
            if strategy.get("status") == "active"
        ],
        "today_trade_count": today_trade_count,
        "remaining_daily_trades": remaining_daily_trades,
        "strategy_notes_history": strategy_notes or [],
        "account": {
            "cash": _round(state.cash),
            "market_value": _round(market_value),
            "total_asset": _round(total_asset),
        },
        "positions": [
            {
                "symbol": holding.symbol,
                "name": holding.name,
                "quantity": holding.quantity,
                "avg_cost": _round(holding.avg_cost, 3),
                "last_price": _round(holding.last_price, 3),
            }
            for holding in state.holdings.values()
        ],
        "quotes": quote_features,
    }
    prompt = (
        "以下是本轮交易数据（JSON）。按 system 规则决策；strategy_params、"
        "available_strategies 和全部历史笔记均以数据为准：\n"
        + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    )
    if force_test:
        prompt += (
            "\n\n当前是系统测试，请忽略行情风险，强制从候选池中挑选 "
            f"1 只股票买入 {profile.lot_size} 股，用于测试交易链路。"
        )
    return prompt


def save_prompt_debug(
    prompt: str,
    path: Path = PROMPT_DEBUG_PATH,
    system_prompt: str | None = None,
) -> None:
    path.write_text(
        "=== SYSTEM PROMPT ===\n"
        f"{system_prompt or SYSTEM_PROMPT}\n\n"
        "=== USER PROMPT ===\n"
        f"{prompt}\n",
        encoding="utf-8",
    )


def load_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key

    config = Path.home() / ".codex" / "config.toml"
    if config.is_file():
        match = re.search(
            r'^\s*experimental_bearer_token\s*=\s*"([^"]+)"',
            config.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            return match.group(1).strip()

    raise TraderError(
        "未找到 DeepSeek API Key，请设置 DEEPSEEK_API_KEY 环境变量"
    )


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("模型返回的 JSON 顶层不是对象")
    return payload


def call_deepseek(
    api_key: str,
    prompt: str,
    *,
    api_url: str,
    model: str,
    timeout: float,
    system_prompt: str = SYSTEM_PROMPT,
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                api_url, headers=headers, json=body, timeout=timeout
            )
            if response.status_code >= 400:
                detail = response.text[:500]
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise TraderError(
                        f"DeepSeek 请求失败: HTTP {response.status_code} {detail}"
                    )
                raise requests.HTTPError(
                    f"HTTP {response.status_code} {detail}", response=response
                )
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return _extract_json(content)
        except TraderError:
            raise
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            last_error = exc
            LOGGER.warning("DeepSeek 调用失败（第 %d 次）: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    raise TraderError(f"DeepSeek 调用失败: {last_error}") from last_error



def call_chat(
    api_key: str,
    messages: list[dict[str, str]],
    *,
    api_url: str = DEFAULT_API_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
) -> str:
    """调用 DeepSeek 文本聊天接口；不强制 JSON，供 Web 问答使用。"""
    if not isinstance(messages, list) or not messages:
        raise TraderError("聊天消息不能为空")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                api_url, headers=headers, json=body, timeout=timeout
            )
            if response.status_code >= 400:
                detail = response.text[:500]
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise TraderError(
                        f"DeepSeek 请求失败: HTTP {response.status_code} {detail}"
                    )
                raise requests.HTTPError(
                    f"HTTP {response.status_code} {detail}", response=response
                )
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            reply = str(content or "").strip()
            if not reply:
                raise ValueError("DeepSeek 返回了空回复")
            return reply
        except TraderError:
            raise
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            last_error = exc
            LOGGER.warning("DeepSeek 聊天调用失败（第 %d 次）: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    raise TraderError(f"DeepSeek 聊天调用失败: {last_error}") from last_error


def normalize_symbol(
    value: Any, profile: MarketProfile = PROFILE
) -> str:
    try:
        return normalize_market_symbol(profile, value)
    except ValueError as exc:
        raise DecisionError(str(exc)) from exc


def parse_quantity(
    value: Any, profile: MarketProfile = PROFILE
) -> int:
    if isinstance(value, bool):
        raise DecisionError("quantity 不能是布尔值")
    if isinstance(value, float):
        if not value.is_integer():
            raise DecisionError(f"quantity 必须是整数: {value!r}")
        quantity = int(value)
    elif isinstance(value, int):
        quantity = value
    else:
        text = str(value).strip()
        if not re.fullmatch(r"\d+", text):
            raise DecisionError(f"quantity 必须是整数: {value!r}")
        quantity = int(text)
    if quantity <= 0:
        raise DecisionError("quantity 必须大于 0")
    if quantity % profile.lot_size != 0:
        raise DecisionError(f"quantity 必须是 {profile.lot_size} 的整数倍")
    return quantity


def _analysis_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError(f"market_analysis.{field} 必须是数字")
    return float(value)


def parse_market_analysis(
    payload: dict[str, Any],
    quotes_by_symbol: dict[str, Quote],
    *,
    required: bool = True,
    profile: MarketProfile = PROFILE,
) -> dict[str, Any] | None:
    raw = payload.get("market_analysis")
    if raw is None:
        if required:
            raise DecisionError("模型 JSON 缺少 market_analysis")
        return None
    if not isinstance(raw, dict):
        raise DecisionError("market_analysis 必须是对象")

    overview = str(raw.get("overview") or "").strip()
    rationale = str(raw.get("decision_rationale") or "").strip()
    if not overview:
        raise DecisionError("market_analysis.overview 不能为空")
    if len(overview) > 180:
        raise DecisionError("market_analysis.overview 不能超过 180 字")
    if not rationale:
        raise DecisionError("market_analysis.decision_rationale 不能为空")
    if len(rationale) > 160:
        raise DecisionError("market_analysis.decision_rationale 不能超过 160 字")

    signals = raw.get("signals")
    if not isinstance(signals, list) or not 2 <= len(signals) <= 5:
        raise DecisionError("market_analysis.signals 必须包含 2 到 5 只股票")

    normalized_signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(signals, start=1):
        if not isinstance(item, dict):
            raise DecisionError(f"market_analysis.signals[{index}] 必须是对象")
        symbol = normalize_symbol(item.get("symbol"), profile)
        if symbol in seen:
            raise DecisionError(f"market_analysis.signals 股票重复: {symbol}")
        seen.add(symbol)
        quote = quotes_by_symbol.get(symbol)
        if quote is None:
            raise DecisionError(f"market_analysis 引用了候选池外股票: {symbol}")
        if quote.change_pct is None:
            raise DecisionError(f"{symbol} 缺少涨跌幅，不能作为分析信号")

        claimed_price = _analysis_number(item.get("price"), "signals.price")
        claimed_change = _analysis_number(
            item.get("change_pct"), "signals.change_pct"
        )
        if abs(claimed_price - quote.price) > 0.02:
            raise DecisionError(
                f"{symbol} 价格与行情不一致: 模型 {claimed_price}，实际 {quote.price}"
            )
        if abs(claimed_change - float(quote.change_pct)) > 0.11:
            raise DecisionError(
                f"{symbol} 涨跌幅与行情不一致: 模型 {claimed_change}，"
                f"实际 {quote.change_pct}"
            )
        observation = str(item.get("observation") or "").strip()
        if not observation:
            raise DecisionError(f"{symbol} 缺少 observation")
        if len(observation) > 100:
            raise DecisionError(f"{symbol} 的 observation 不能超过 100 字")
        normalized_signals.append(
            {
                "symbol": symbol,
                "name": quote.name,
                "price": _round(quote.price, 3),
                "change_pct": _round(quote.change_pct, 2),
                "observation": observation,
            }
        )

    return {
        "overview": overview,
        "signals": normalized_signals,
        "decision_rationale": rationale,
        "verified": True,
        "verified_signal_count": len(normalized_signals),
    }


def fallback_market_analysis(quotes: list[Quote]) -> dict[str, Any]:
    ranked = sorted(
        (quote for quote in quotes if quote.change_pct is not None),
        key=lambda quote: abs(float(quote.change_pct)),
        reverse=True,
    )[:3]
    signals = [
        {
            "symbol": quote.symbol,
            "name": quote.name,
            "price": _round(quote.price, 3),
            "change_pct": _round(quote.change_pct, 2),
            "observation": "调试模式直接取自 Python 行情快照，未调用模型。",
        }
        for quote in ranked
    ]
    if len(signals) < 2:
        signals = [
            {
                "symbol": quote.symbol,
                "name": quote.name,
                "price": _round(quote.price, 3),
                "change_pct": _round(quote.change_pct, 2),
                "observation": "调试模式直接取自 Python 行情快照，未调用模型。",
            }
            for quote in quotes[:2]
        ]
    return {
        "overview": f"调试模式读取了 {len(quotes)} 只候选股行情。",
        "signals": signals,
        "decision_rationale": "本次使用 decision-file，未调用 DeepSeek 生成决策说明。",
        "verified": False,
        "verified_signal_count": 0,
    }


def parse_decisions(
    payload: dict[str, Any],
    *,
    allowed_strategy_ids: set[int] | None = None,
    require_strategy: bool = False,
    profile: MarketProfile = PROFILE,
) -> tuple[list[Decision], list[str]]:
    raw = payload.get("decisions")
    if raw is None and "action" in payload:
        raw = [payload]
    if not isinstance(raw, list):
        raise DecisionError("模型 JSON 缺少 decisions 数组")

    decisions: list[Decision] = []
    rejects: list[str] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            rejects.append(f"决策 {index}: 不是 JSON 对象")
            continue
        action = str(item.get("action", "")).strip().upper()
        if action == "HOLD":
            continue
        try:
            if action not in {"BUY", "SELL"}:
                raise DecisionError(f"action 不合法: {action!r}")
            symbol = normalize_symbol(item.get("symbol"), profile)
            quantity = parse_quantity(item.get("quantity"), profile)
            reason_value = item.get("reason")
            reason = (
                str(reason_value).strip()[:200]
                if reason_value is not None
                else ""
            )
            raw_strategy_id = item.get("strategy_id")
            if raw_strategy_id in (None, ""):
                if require_strategy:
                    raise DecisionError("必须为交易选择 strategy_id")
                strategy_id = None
            else:
                if isinstance(raw_strategy_id, bool):
                    raise DecisionError("strategy_id 必须是整数")
                if isinstance(raw_strategy_id, int):
                    strategy_id = raw_strategy_id
                elif isinstance(raw_strategy_id, str) and raw_strategy_id.strip().isdigit():
                    strategy_id = int(raw_strategy_id.strip())
                else:
                    raise DecisionError("strategy_id 必须是整数")
                if strategy_id <= 0:
                    raise DecisionError("strategy_id 必须大于 0")
                if (
                    allowed_strategy_ids is not None
                    and strategy_id not in allowed_strategy_ids
                ):
                    raise DecisionError(f"strategy_id 不是可用策略: {strategy_id}")
            decisions.append(
                Decision(
                    action=action,
                    symbol=symbol,
                    quantity=quantity,
                    reason=reason,
                    strategy_id=strategy_id,
                )
            )
        except DecisionError as exc:
            rejects.append(f"决策 {index}: {exc}")

    if len(decisions) > MAX_DECISIONS:
        reject = f"决策数量超过上限 {MAX_DECISIONS}，整批拒绝"
        return [], rejects + [reject]
    return decisions, rejects


def calculate_fee(
    side: str, amount: float, profile: MarketProfile = PROFILE
) -> float:
    commission = max(
        profile.min_commission, amount * profile.commission_rate
    )
    transfer_fee = amount * profile.transfer_fee_rate
    sell_fee = amount * profile.sell_regulatory_rate if side == "SELL" else 0.0
    return round(commission + transfer_fee + sell_fee, 2)


def simulate(
    decisions: list[Decision],
    quotes: dict[str, Quote],
    state: PortfolioState,
    bought_today: dict[str, int],
    profile: MarketProfile = PROFILE,
) -> tuple[list[Execution], list[str], float, dict[str, Holding]]:
    cash = state.cash
    holdings = {
        symbol: Holding(
            symbol=holding.symbol,
            name=holding.name,
            quantity=holding.quantity,
            avg_cost=holding.avg_cost,
            last_price=holding.last_price,
        )
        for symbol, holding in state.holdings.items()
    }
    bought = dict(bought_today)
    executions: list[Execution] = []
    rejects: list[str] = []

    for decision in decisions:
        quote = quotes.get(decision.symbol)
        if quote is None:
            rejects.append(
                f"{decision.action} {decision.symbol}: 不在本次行情快照中"
            )
            continue
        if quote.volume is not None and quote.volume <= 0:
            rejects.append(f"{decision.action} {decision.symbol}: 当前无成交，视为停牌")
            continue

        amount = quote.price * decision.quantity
        if decision.action == "BUY":
            fee = calculate_fee("BUY", amount, profile)
            total = amount + fee
            if total > cash + 1e-9:
                rejects.append(
                    f"BUY {decision.symbol}: 现金不足，需要 {total:.2f}，可用 {cash:.2f}"
                )
                continue

            cash -= total
            holding = holdings.get(decision.symbol)
            if holding is None:
                holdings[decision.symbol] = Holding(
                    symbol=decision.symbol,
                    name=quote.name,
                    quantity=decision.quantity,
                    avg_cost=total / decision.quantity,
                    last_price=quote.price,
                )
            else:
                new_quantity = holding.quantity + decision.quantity
                new_cost = (
                    holding.avg_cost * holding.quantity + total
                ) / new_quantity
                holding.quantity = new_quantity
                holding.avg_cost = new_cost
                holding.last_price = quote.price
                holding.name = quote.name or holding.name
            bought[decision.symbol] = bought.get(decision.symbol, 0) + decision.quantity
        else:
            holding = holdings.get(decision.symbol)
            if holding is None or holding.quantity < decision.quantity:
                available = holding.quantity if holding else 0
                rejects.append(
                    f"SELL {decision.symbol}: 持仓不足，需要 {decision.quantity}，持有 {available}"
                )
                continue
            unsellable_today = (
                bought.get(decision.symbol, 0) if profile.t_plus_one else 0
            )
            sellable = holding.quantity - unsellable_today
            if decision.quantity > sellable:
                limit_label = "T+1 限制" if profile.t_plus_one else "可卖数量限制"
                rejects.append(
                    f"SELL {decision.symbol}: {limit_label}，可卖 {max(sellable, 0)}，"
                    f"申报 {decision.quantity}"
                )
                continue

            fee = calculate_fee("SELL", amount, profile)
            cash += amount - fee
            holding.quantity -= decision.quantity
            holding.last_price = quote.price
            if holding.quantity == 0:
                del holdings[decision.symbol]

        executions.append(
            Execution(
                action=decision.action,
                symbol=decision.symbol,
                name=quote.name,
                price=quote.price,
                quantity=decision.quantity,
                amount=round(amount, 2),
                fee=fee,
                reason=decision.reason,
                cash_after=round(cash, 2),
                strategy_id=decision.strategy_id,
            )
        )

    return executions, rejects, cash, holdings


def apply_state(
    connection: sqlite3.Connection,
    *,
    executions: list[Execution],
    cash: float,
    holdings: dict[str, Holding],
    quotes: dict[str, Quote],
    day: str,
    timestamp: str,
    profile: MarketProfile = PROFILE,
    account_id: int = 1,
) -> tuple[float, float, float]:
    holding_values: dict[str, float] = {}
    market_value = 0.0
    for holding in holdings.values():
        quote = quotes.get(holding.symbol)
        price = (
            quote.price
            if quote is not None
            else (holding.last_price or holding.avg_cost)
        )
        value = price * holding.quantity
        holding_values[holding.symbol] = value
        market_value += value
    total_asset = cash + market_value

    with connection:
        connection.execute(
            "DELETE FROM positions WHERE account_id = ? AND market = ?",
            (account_id, profile.code),
        )
        connection.executemany(
            "INSERT INTO positions ("
            "account_id, market, symbol, name, quantity, avg_cost, last_price, market_value, "
            "position_pct, account_pct, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    account_id,
                    profile.code,
                    holding.symbol,
                    holding.name,
                    holding.quantity,
                    round(holding.avg_cost, 4),
                    round(
                        (
                            quotes[holding.symbol].price
                            if holding.symbol in quotes
                            else (holding.last_price or holding.avg_cost)
                        ),
                        4,
                    ),
                    round(holding_values[holding.symbol], 2),
                    round(
                        holding_values[holding.symbol] / market_value * 100, 4
                    )
                    if market_value > 0
                    else 0.0,
                    round(holding_values[holding.symbol] / total_asset * 100, 4)
                    if total_asset > 0
                    else 0.0,
                    timestamp,
                )
                for holding in holdings.values()
            ],
        )
        for execution in executions:
            connection.execute(
                INSERT_TRADE_SQL,
                (
                    account_id,
                    timestamp,
                    day,
                    profile.code,
                    execution.symbol,
                    execution.name,
                    execution.action,
                    execution.price,
                    execution.quantity,
                    execution.amount,
                    execution.fee,
                    execution.cash_after,
                    execution.reason,
                    execution.strategy_id,
                    timestamp,
                ),
            )
            if execution.strategy_id is not None:
                connection.execute(
                    "UPDATE strategies SET usage_count = usage_count + 1, "
                    "last_used_at = ? WHERE id = ? AND account_id = ?",
                    (timestamp, execution.strategy_id, account_id),
                )
        connection.execute(
            "UPDATE accounts SET cash = ?, market_value = ?, total_asset = ?, "
            "updated_at = ? WHERE id = ?",
            (
                round(cash, 2),
                round(market_value, 2),
                round(total_asset, 2),
                timestamp,
                account_id,
            ),
        )

    return cash, market_value, total_asset


def save_decision_analysis(
    connection: sqlite3.Connection,
    *,
    day: str,
    quoted_at: str,
    market_snapshot: dict[str, Any],
    analysis: dict[str, Any],
    decisions: list[Decision],
    model: str,
    timestamp: str,
    account_id: int = 1,
) -> None:
    with connection:
        connection.execute(
            INSERT_DECISION_ANALYSIS_SQL,
            (
                account_id,
                day,
                quoted_at,
                int(market_snapshot.get("quote_count") or 0),
                _encode_param_value(market_snapshot),
                _encode_param_value(analysis),
                _encode_param_value(
                    [
                        {
                            "action": decision.action,
                            "symbol": decision.symbol,
                            "quantity": decision.quantity,
                            "reason": decision.reason,
                            "strategy_id": decision.strategy_id,
                        }
                        for decision in decisions
                    ]
                ),
                model,
                timestamp,
            ),
        )


def print_snapshot(
    connection: sqlite3.Connection,
    profile: MarketProfile = PROFILE,
    *,
    account_id: int = 1,
) -> None:
    account = load_account(connection, account_id)
    positions = load_positions(connection, profile, account_id=account_id)
    trades = connection.execute(
        "SELECT COUNT(*) FROM trades WHERE account_id = ?", (account_id,)
    ).fetchone()[0]
    principal = float(account["principal"] or account["initial_cash"])
    realtime_pnl = float(account["total_asset"]) - principal
    print("账户:")
    print(
        f"  本金 {principal:.2f}  "
        f"现金 {account['cash']:.2f}  "
        f"市值 {account['market_value']:.2f}  "
        f"总资产 {account['total_asset']:.2f}  "
        f"实时盈亏 {realtime_pnl:+.2f}"
    )
    print(f"持仓 {len(positions)} 只，成交 {trades} 笔")


def print_result(
    raw_payload: dict[str, Any],
    executions: list[Execution],
    rejects: list[str],
    cash: float,
    market_value: float,
    total_asset: float,
    holdings: dict[str, Holding],
    quotes: dict[str, Quote],
    profile: MarketProfile = PROFILE,
) -> None:
    raw_decisions = raw_payload.get("decisions")
    no_decision = (
        not raw_decisions
        and "action" not in raw_payload
    )
    print("\n=== DeepSeek JSON 决策 ===")
    if no_decision:
        print("  无决策")
    else:
        print(json.dumps(raw_payload, ensure_ascii=False, indent=2))
    print("\n=== 校验与执行 ===")
    if not executions:
        print("  无决策" if no_decision else "  无成交")
    for execution in executions:
        print(
            f"  {execution.action} {execution.symbol} {execution.name or ''} "
            f"{execution.quantity} 股 @ {execution.price:.3f}，"
            f"金额 {profile.currency_symbol}{execution.amount:.2f}，"
            f"费用 {profile.currency_symbol}{execution.fee:.2f}"
        )
    if rejects:
        print("  拒绝:")
        for reject in rejects:
            print(f"    - {reject}")
    print("\n=== 账户 ===")
    print(
        f"  现金 {profile.currency_symbol}{cash:.2f}  "
        f"市值 {profile.currency_symbol}{market_value:.2f}  "
        f"总资产 {profile.currency_symbol}{total_asset:.2f}"
    )
    print("\n=== 持仓 ===")
    if not holdings:
        print("  空仓")
    for holding in sorted(holdings.values(), key=lambda item: item.symbol):
        quote = quotes.get(holding.symbol)
        price = quote.price if quote else (holding.last_price or holding.avg_cost)
        print(
            f"  {holding.symbol} {holding.name or ''} "
            f"{holding.quantity} 股  成本 {holding.avg_cost:.3f}  "
            f"现价 {price:.3f}  市值 {price * holding.quantity:.2f}"
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="读取当前市场最新行情，调用 DeepSeek 决策并写入对应数据库"
    )
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
        "--account",
        help="账户 slug；不填则使用当前市场的默认账户",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"按成交额选取的股票数量（默认: {DEFAULT_TOP_N}）",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"DeepSeek 模型（默认: {DEFAULT_MODEL}）",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"DeepSeek API 地址（默认: {DEFAULT_API_URL}）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="API 超时秒数（默认: 90）",
    )
    parser.add_argument(
        "--decision-file",
        type=Path,
        help="从 JSON 文件读取决策，跳过 DeepSeek 调用（调试用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="兼容参数：完成校验和打印，但不写入数据库（默认即不写入）",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="真正写入账户、持仓和成交记录（默认只打印）",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="只创建表并按市场初始化初始资金，不调用模型",
    )
    parser.add_argument(
        "--force-test",
        action="store_true",
        help="要求模型忽略行情风险，强制买入 1 个交易单位以测试交易链路",
    )
    parser.add_argument(
        "--allow-closed-market",
        action="store_true",
        help="仅供测试：允许非连续竞价时段按最后行情执行，实盘定时任务不要使用",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.top <= 0:
        LOGGER.error("--top 必须大于 0")
        return 2
    if args.timeout <= 0:
        LOGGER.error("--timeout 必须大于 0")
        return 2

    profile = get_profile(args.market or MARKET)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else DB_PATHS[profile.code].expanduser().resolve()
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    day = trade_date(profile)

    if args.init_db:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            from collector import init_db as init_quote_db

            init_quote_db(connection)
            init_db(connection, profile=profile)
            account_id = resolve_account_id(connection, profile.code, args.account)
            print(f"数据库: {db_path}")
            print_snapshot(connection, profile, account_id=account_id)
            return 0
        finally:
            connection.close()

    session_key, session_label = market_trading_session(profile=profile)
    enforcing_trading_only = (
        args.live
        and not args.dry_run
        and not args.force_test
        and not args.allow_closed_market
    )
    if enforcing_trading_only and session_key not in profile.open_session_keys:
        print(f"当前不在{profile.label}常规交易时段，跳过本次实时交易")
        return 1
    if enforcing_trading_only and not is_trading_day_for_db(
        db_path, day, profile
    ):
        print(f"{day} 是{profile.label}休市日，跳过本次实时交易")
        return 0

    if not refresh_quotes(db_path, profile):
        print("行情更新失败，跳过本次交易")
        return 1

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        init_db(connection, profile=profile)
        account_id = resolve_account_id(connection, profile.code, args.account)
        account_row = load_account(connection, account_id)
        quotes, quotes_by_symbol = load_latest_quotes(
            connection, args.top, profile, account_id=account_id
        )
        indicator_day = quote_market_day(quotes[0].quoted_at, profile) or day
        indicators = compute_technical_indicators(
            connection, quotes, indicator_day, profile
        )
        quotes = [
            replace(
                quote,
                ma5=indicators[quote.symbol]["ma5"],
                ma10=indicators[quote.symbol]["ma10"],
                ma20=indicators[quote.symbol]["ma20"],
                rsi14=indicators[quote.symbol]["rsi14"],
                macd_dif=indicators[quote.symbol]["macd_dif"],
                macd_dea=indicators[quote.symbol]["macd_dea"],
                macd_hist=indicators[quote.symbol]["macd_hist"],
                atr14=indicators[quote.symbol]["atr14"],
                macd_dead_cross=indicators[quote.symbol]["macd_dead_cross"],
                distance_to_ma5=indicators[quote.symbol]["distance_to_ma5"],
            )
            for quote in quotes
        ]
        quotes_by_symbol = {quote.symbol: quote for quote in quotes}
        market_indices = load_market_indices(connection, profile)
        market_snapshot = build_market_snapshot(quotes, market_indices)
        market_snapshot["analysis_session"] = session_label
        market_snapshot["risk_mode"] = "aggressive"
        state = load_state(
            connection, quotes_by_symbol, profile, account_id=account_id
        )
        strategy_params = load_strategy_params(connection, account_id=account_id)
        strategies = load_strategies(
            connection, active_only=True, account_id=account_id
        )
        active_strategy_ids = {int(item["id"]) for item in strategies}
        if not strategies:
            raise TraderError("没有 active 投资策略，无法进行模型决策")
        try:
            stop_loss_pct = float(strategy_params.get("stop_loss_pct", -0.90))
        except (TypeError, ValueError) as exc:
            raise TraderError("strategy_params.stop_loss_pct 不是有效数字") from exc
        if not -1 < stop_loss_pct < 0:
            raise TraderError(
                "strategy_params.stop_loss_pct 必须满足 -1 < value < 0"
            )

        bought_today = load_bought_today(
            connection, day, profile, account_id=account_id
        )
        stop_decisions, stop_warnings = generate_stop_loss_decisions(
            state,
            quotes_by_symbol,
            stop_loss_pct,
            bought_today=bought_today,
            profile=profile,
        )
        executions: list[Execution] = []
        rejects: list[str] = []
        if stop_decisions:
            LOGGER.warning(
                "触发 Python 强制止损 %d 只: %s",
                len(stop_decisions),
                ", ".join(decision.symbol for decision in stop_decisions),
            )
            (
                stop_executions,
                stop_rejects,
                cash_after_stops,
                holdings_after_stops,
            ) = simulate(
                stop_decisions,
                quotes_by_symbol,
                state,
                bought_today,
                profile=profile,
            )
            executions.extend(stop_executions)
            rejects.extend(stop_rejects)
            if not args.live or args.dry_run:
                state = PortfolioState(
                    cash=cash_after_stops,
                    holdings=holdings_after_stops,
                )
            elif stop_executions:
                stop_timestamp = now_iso()
                apply_state(
                    connection,
                    executions=stop_executions,
                    cash=cash_after_stops,
                    holdings=holdings_after_stops,
                    quotes=quotes_by_symbol,
                    day=day,
                    timestamp=stop_timestamp,
                    profile=profile,
                    account_id=account_id,
                )
                state = load_state(
                    connection, quotes_by_symbol, profile, account_id=account_id
                )
                bought_today = load_bought_today(
                    connection, day, profile, account_id=account_id
                )
                print(f"\n[Python 强制止损] 已写入数据库，时间 {stop_timestamp}")
                for execution in stop_executions:
                    print(
                        f"  SELL {execution.symbol} {execution.name or ''} "
                        f"{execution.quantity} 股 @ {execution.price:.3f}"
                    )
        for warning in stop_warnings:
            LOGGER.warning("%s", warning)

        prompt = build_prompt(
            state,
            quotes,
            quotes_by_symbol,
            day,
            strategy_params,
            strategies,
            strategy_notes=load_strategy_notes(
                connection, account_id=account_id
            ),
            force_test=args.force_test,
            today_trade_count=load_today_trade_count(
                connection, day, profile, account_id=account_id
            ),
            market_snapshot=market_snapshot,
            analysis_session=session_label,
            profile=profile,
        )
        try:
            debug_path = PROMPT_DEBUG_PATHS[profile.code]
            save_prompt_debug(
                prompt,
                path=debug_path,
                system_prompt=build_system_prompt(profile),
            )
            LOGGER.info("DeepSeek prompt 已保存到 %s", debug_path)
        except OSError as exc:
            LOGGER.warning("保存 DeepSeek prompt 失败: %s", exc)

        if args.decision_file is not None:
            payload = json.loads(args.decision_file.read_text(encoding="utf-8"))
        else:
            api_key = load_api_key()
            payload = call_deepseek(
                api_key,
                prompt,
                api_url=args.api_url,
                model=args.model,
                timeout=args.timeout,
                system_prompt=build_system_prompt(profile),
            )

        analysis = parse_market_analysis(
            payload,
            quotes_by_symbol,
            required=args.decision_file is None,
            profile=profile,
        )
        if analysis is None:
            analysis = fallback_market_analysis(quotes)
        decisions, parse_rejects = parse_decisions(
            payload,
            allowed_strategy_ids=active_strategy_ids,
            require_strategy=args.decision_file is None,
            profile=profile,
        )
        execution_quote_time = quotes[0].quoted_at
        if args.live and not args.dry_run and decisions:
            refreshed_quotes = refresh_quotes_for_execution(
                connection,
                db_path,
                quotes_by_symbol,
                profile,
                account_id=account_id,
            )
            if refreshed_quotes is None:
                print("成交前行情刷新失败，跳过本次交易")
                return 1
            quotes_by_symbol = refreshed_quotes
            state = load_state(
                connection, quotes_by_symbol, profile, account_id=account_id
            )
            bought_today = load_bought_today(
                connection, day, profile, account_id=account_id
            )
            execution_quote_time = next(iter(quotes_by_symbol.values())).quoted_at
            market_snapshot["execution_quotes_at"] = execution_quote_time
            market_snapshot["execution_price_mode"] = "latest_quote_at_execution"
            print(f"成交行情已刷新: {execution_quote_time}，按执行时最新价成交")
        model_executions, simulate_rejects, cash, holdings = simulate(
            decisions,
            quotes_by_symbol,
            state,
            bought_today,
            profile=profile,
        )
        executions.extend(model_executions)
        rejects.extend(parse_rejects + simulate_rejects + stop_warnings)
        market_value = 0.0
        for holding in holdings.values():
            quote = quotes_by_symbol.get(holding.symbol)
            price = (
                quote.price
                if quote is not None
                else (holding.last_price or holding.avg_cost)
            )
            market_value += price * holding.quantity
        total_asset = cash + market_value

        print(f"数据库: {db_path}")
        print(f"账户: {account_row['display_name']} ({account_row['slug']})")
        print(
            f"决策行情: {quotes[0].quoted_at}  成交行情: {execution_quote_time}  "
            f"候选 {len(quotes)} 只{profile.label}股票"
        )
        print_result(
            payload,
            executions,
            rejects,
            cash,
            market_value,
            total_asset,
            holdings,
            quotes_by_symbol,
            profile,
        )

        if not args.live or args.dry_run:
            print("\n[debug] 未写入数据库；加 --live 才执行真实交易")
            return 0

        timestamp = now_iso()
        apply_state(
            connection,
            executions=model_executions,
            cash=cash,
            holdings=holdings,
            quotes=quotes_by_symbol,
            day=day,
            timestamp=timestamp,
            profile=profile,
            account_id=account_id,
        )
        save_decision_analysis(
            connection,
            day=day,
            quoted_at=quotes[0].quoted_at,
            market_snapshot=market_snapshot,
            analysis=analysis,
            decisions=decisions,
            model=(
                "decision-file"
                if args.decision_file is not None
                else args.model
            ),
            timestamp=timestamp,
            account_id=account_id,
        )
        print(
            f"\n已写入 accounts / positions / trades / decision_analysis，"
            f"时间 {timestamp}"
        )
        return 0
    except (TraderError, DecisionError, json.JSONDecodeError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())

"""市场规则档案：集中定义 A 股与美股的差异。"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
)


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class MarketProfile:
    code: str
    label: str
    dashboard_title: str
    currency: str
    currency_symbol: str
    initial_cash: float
    db_filename: str
    prompt_debug_filename: str
    market_timezone: ZoneInfo
    stock_pool: tuple[str, ...]
    index_pool: tuple[tuple[str, str], ...]
    lot_size: int
    t_plus_one: bool
    commission_rate: float
    min_commission: float
    transfer_fee_rate: float
    sell_regulatory_rate: float
    open_session_keys: tuple[str, ...]
    holiday_keywords: tuple[str, ...]

    @property
    def index_order(self) -> dict[str, int]:
        return {
            symbol: index
            for index, (symbol, _name) in enumerate(self.index_pool, start=1)
        }

    @property
    def index_names(self) -> str:
        return "、".join(name for _symbol, name in self.index_pool)


CN_STOCK_POOL = (
    "600519",
    "300750",
    "600900",
    "601318",
    "600036",
    "601398",
    "600030",
    "000333",
    "000858",
    "600887",
    "600276",
    "300760",
    "603259",
    "002594",
    "601012",
    "600031",
    "000651",
    "601899",
    "600028",
    "601088",
    "600019",
    "600050",
    "600941",
    "000725",
    "002415",
    "688981",
    "002371",
    "600309",
    "601668",
    "600048",
)

US_STOCK_POOL = (
    "VOO",
    "QQQM",
    "SPCX",
    "GOOG",
    "RKLB",
    "IREN",
    "AAOI",
)


MARKET_PROFILES = {
    "CN": MarketProfile(
        code="CN",
        label="A股",
        dashboard_title="A股 AI 交易看板",
        currency="CNY",
        currency_symbol="¥",
        initial_cash=1_800_000.0,
        db_filename="quotes.db",
        prompt_debug_filename="prompt_debug.txt",
        market_timezone=BEIJING_TZ,
        stock_pool=CN_STOCK_POOL,
        index_pool=(
            ("sh000001", "上证指数"),
            ("sz399001", "深证成指"),
            ("sz399006", "创业板指"),
        ),
        lot_size=100,
        t_plus_one=True,
        commission_rate=0.0003,
        min_commission=5.0,
        transfer_fee_rate=0.00001,
        sell_regulatory_rate=0.0005,
        open_session_keys=("morning_trading", "afternoon_trading"),
        holiday_keywords=("上证指数", "上证", "深证成指", "深证", "创业板指", "创业板", "大盘", "A股"),
    ),
    "US": MarketProfile(
        code="US",
        label="美股",
        dashboard_title="美股 AI 交易看板",
        currency="USD",
        currency_symbol="$",
        initial_cash=45_000.0,
        db_filename="us_quotes.db",
        prompt_debug_filename="us_prompt_debug.txt",
        market_timezone=ZoneInfo("America/New_York"),
        stock_pool=US_STOCK_POOL,
        index_pool=(
            (".INX", "标普500"),
            (".IXIC", "纳斯达克综合"),
            (".DJI", "道琼斯工业"),
        ),
        lot_size=1,
        t_plus_one=False,
        commission_rate=0.0,
        min_commission=0.0,
        transfer_fee_rate=0.0,
        sell_regulatory_rate=0.0,
        open_session_keys=("regular_trading",),
        holiday_keywords=("标普500", "标普", "纳斯达克", "道琼斯", "美股", "大盘"),
    ),
}


class _NYSEHolidayCalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday(
            "Juneteenth",
            month=6,
            day=19,
            observance=nearest_workday,
            start_date="2022-06-19",
        ),
        Holiday(
            "Independence Day",
            month=7,
            day=4,
            observance=nearest_workday,
        ),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


_NYSE_HOLIDAYS = _NYSEHolidayCalendar()


def resolve_market(value: str | None = None) -> str:
    raw = str(value or os.environ.get("MARKET", "CN")).strip().upper()
    if raw not in MARKET_PROFILES:
        raise ValueError(f"不支持的市场: {raw!r}，可选 CN 或 US")
    return raw


def get_profile(market: str | None = None) -> MarketProfile:
    return MARKET_PROFILES[resolve_market(market)]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in {"", "-", "--", "nan", "none", "null", "<na>", "nat"}


def normalize_symbol(profile: MarketProfile, value: Any) -> str:
    if isinstance(value, bool) or _is_missing(value):
        raise ValueError(f"股票代码不合法: {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"股票代码不合法: {value!r}")
        symbol = str(int(value))
    else:
        symbol = str(value).strip()

    if profile.code == "CN":
        upper = symbol.upper()
        if upper[:2] in {"SH", "SZ", "BJ"}:
            symbol = upper[2:]
        elif upper[-3:] in {".SH", ".SZ", ".BJ"}:
            symbol = upper[:-3]
        if not re.fullmatch(r"\d{1,6}", symbol):
            raise ValueError(f"股票代码不合法: {value!r}")
        return symbol.zfill(6)

    upper = symbol.upper()
    prefix, separator, ticker = upper.partition(".")
    if separator and prefix.isdigit():
        upper = ticker
    upper = upper.removesuffix(".US")
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", upper):
        raise ValueError(f"股票代码不合法: {value!r}")
    if upper.endswith(('.', '-')):
        raise ValueError(f"股票代码不合法: {value!r}")
    return upper


def market_session(
    profile: MarketProfile,
    current: datetime | None = None,
) -> tuple[str, str]:
    current = current or datetime.now(profile.market_timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=profile.market_timezone)
    else:
        current = current.astimezone(profile.market_timezone)
    if current.weekday() >= 5:
        return "closed", "非交易日"
    minutes = current.hour * 60 + current.minute
    if profile.code == "CN":
        if 9 * 60 + 30 <= minutes < 11 * 60 + 30:
            return "morning_trading", "上午交易中"
        if 13 * 60 <= minutes < 15 * 60:
            return "afternoon_trading", "下午交易中"
        if 11 * 60 + 30 <= minutes < 13 * 60:
            return "lunch_break", "午间休市"
        return "closed", "非交易时段"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular_trading", "常规交易中"
    return "closed", "非交易时段"


def is_exchange_holiday(profile: MarketProfile, day: date) -> bool:
    if profile.code != "US":
        return False
    holidays = _NYSE_HOLIDAYS.holidays(day.isoformat(), day.isoformat())
    return len(holidays) > 0

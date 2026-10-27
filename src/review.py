#!/usr/bin/env python3
"""根据当日交易、行情和聊天记录复盘，更新策略参数与投资策略库。"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

import trader
from market_profile import (
    MarketProfile,
    get_profile,
    normalize_symbol as normalize_market_symbol,
)


LOGGER = logging.getLogger("strategy_review")

MAX_NEW_PARAMS = 10
MAX_REVIEW_CHAT_MESSAGES = 30
MAX_REVIEW_CHAT_MESSAGE_CHARS = 1000

CLASSIC_STRATEGY_REFERENCE = """- 均线突破：价格站上并突破 20 日等关键均线时视为趋势转强，跌破关键均线时降低趋势仓位。
- 动量轮动：优先关注近期相对强势的标的，动量衰减或排名下降时及时轮换。
- 超跌反弹：短期跌幅过大且出现止跌信号时可能存在修复机会，但必须防止持续下跌。
- 网格交易：震荡市场中高抛低吸，趋势行情中避免机械逆势加仓。
- 风控原则：控制单股集中度、设置明确止损，并避免在弱势市场中过度交易。"""

REVIEW_SYSTEM_PROMPT_TEMPLATE = (
    "你是一名激进型 __MARKET_LABEL__量化策略评审助手。用户接受大幅回撤甚至本金损失；不得因单日亏损、回撤或市场下跌自动转向保守。你的任务是基于今日事实调整参数和策略库，提高收益弹性。\n\n"
    "复盘证据：\n"
    "- performance/trades/symbols 是今日盈亏与交易事实；market_context 是 __INDEX_NAMES__ 的点位、涨跌点数、涨跌幅和股票池宽度。\n"
    "- strategy_notes_history 是按日期升序保存的全部历史笔记，必须逐条阅读；保留有效规律，被证伪或被新经验修正的旧结论要明确淘汰。\n"
    "- chat_records 只用于发现关注主题、风险偏好和策略盲点；聊天不是行情事实，也不能直接写入参数。只有与交易结果共同支持时才固化进 strategy_changes。\n"
    "- strategies 是当前策略库；策略只能用现有行情和技术指标可判断的规则。\n\n"
    "经典量化策略参考：\n"
    + CLASSIC_STRATEGY_REFERENCE
    + "\n\n策略库变更规则：\n"
    "- 只能通过顶层 strategy_changes 操作策略：create、update 或 retire，最多 5 项。\n"
    "- create 提供 name、description、rules；update 提供 id 及要改的字段；retire 提供 id。status 只能是 active、paused、retired，且至少保留一个 active 策略。\n"
    "- name 2 到 40 字，description 5 到 300 字，rules 是非空 JSON 对象，建议包含 entry、exit、risk；不要创建同名或规则等价的策略，优先更新已有策略。\n"
    "- 没有值得固化的新经验时返回空数组。\n\n"
    "参数规则：\n"
    "- 边界：max_single_position (0,1]；stop_loss_pct (-1,0)；max_daily_trades 1 到 50；rsi_oversold/rsi_overbought 0 到 100 且前者更小；atr_stop_multiplier (0,10]；min_turnover >= 0；market_regime_weight 0 到 1。\n"
    "- updates 只能改现有参数；new_params 用于新增 snake_case 参数，最多 10 个，不得与现有参数重名或同义，值不能为 null，且必须能由现有行情数据判断。无需修改时返回空对象。\n\n"
    "输出要求：\n"
    "- 只返回一个严格 JSON 对象，不要 Markdown 或前后解释；顶层只能包含 updates、new_params、custom_strategy_notes、strategy_changes、watchlist。\n"
    "- custom_strategy_notes 必须包含今日主要指数涨跌点数、股票池涨跌家数和完整战术总结，不限制字数。\n"
    "- watchlist 必须包含 3 到 5 只不重复股票，且只能来自 watchlist_candidates。\n"
    "- 示例：{\"updates\":{\"max_single_position\":1.0,\"stop_loss_pct\":-0.9,\"market_regime_weight\":0.35},\"new_params\":{\"momentum_threshold\":5},\"custom_strategy_notes\":\"上证涨32.45点、股票池28涨2跌，普涨延续；维持激进进攻，优先强势动量和高成交额龙头，允许满仓集中\",\"strategy_changes\":[{\"action\":\"create\",\"name\":\"超跌转强反弹\",\"description\":\"在短期跌幅较大且当天出现放量转强时小仓位参与修复\",\"rules\":{\"entry\":[\"5日跌幅较大且当日涨幅转正\",\"成交额明显放大\"],\"exit\":[\"跌破当日低点或触发止损\"],\"risk\":[\"遵守max_single_position和stop_loss_pct\"]},\"status\":\"active\"}],\"watchlist\":[{\"symbol\":\"600900\",\"name\":\"长江电力\",\"reason\":\"高成交额防守龙头，观察趋势延续\"},{\"symbol\":\"600519\",\"name\":\"贵州茅台\",\"reason\":\"观察强势动量与均线突破\"},{\"symbol\":\"300750\",\"name\":\"宁德时代\",\"reason\":\"高波动弹性标的，等待放量确认\"}]}\n"
)


def build_review_system_prompt(profile: MarketProfile) -> str:
    first_index = profile.index_pool[0][1] if profile.index_pool else "主要指数"
    watch = list(profile.stock_pool[:3])
    while len(watch) < 3:
        watch.append("AAPL" if profile.code == "US" else "600519")
    prompt = (
        REVIEW_SYSTEM_PROMPT_TEMPLATE.replace("__MARKET_LABEL__", profile.label)
        .replace("__INDEX_NAMES__", profile.index_names)
        .replace("上证涨32.45点", f"{first_index}涨32.45点")
        .replace("600900", watch[0])
        .replace("600519", watch[1])
        .replace("300750", watch[2])
    )
    return prompt


REVIEW_SYSTEM_PROMPT = build_review_system_prompt(get_profile("CN"))


class ReviewError(RuntimeError):
    """参数评审流程中的可预期错误。"""


def load_chat_records(
    connection: sqlite3.Connection,
    day: str,
    *,
    account_id: int = 1,
) -> dict[str, Any]:
    """读取复盘日聊天记录；限制条数和单条长度，避免提示词失控。"""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'chat_messages'"
    ).fetchone()
    if table is None:
        return {"message_count": 0, "returned_count": 0, "messages": []}

    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE account_id = ? AND trade_date = ?",
            (account_id, day),
        ).fetchone()[0]
    )
    rows = connection.execute(
        "SELECT created_at, role, content FROM chat_messages "
        "WHERE account_id = ? AND trade_date = ? ORDER BY id DESC LIMIT ?",
        (account_id, day, MAX_REVIEW_CHAT_MESSAGES),
    ).fetchall()
    messages = [
        {
            "created_at": row["created_at"],
            "role": str(row["role"]),
            "content": str(row["content"])[:MAX_REVIEW_CHAT_MESSAGE_CHARS],
        }
        for row in reversed(rows)
    ]
    return {
        "message_count": total,
        "returned_count": len(messages),
        "messages": messages,
    }


def build_daily_review(
    connection: sqlite3.Connection,
    day: str,
    profile: MarketProfile = trader.PROFILE,
    *,
    account_id: int = 1,
) -> dict[str, Any]:
    account = trader.load_account(connection, account_id)
    position_rows = trader.load_positions(connection, profile, account_id=account_id)
    latest_quotes, quotes_by_symbol = trader.load_latest_quotes(
        connection, 10_000, profile, account_id=account_id
    )
    market_indices = trader.load_market_indices(connection, profile)
    market_context = trader.build_market_snapshot(latest_quotes, market_indices)
    trade_rows = connection.execute(
        "SELECT t.traded_at, t.symbol, t.name, t.side, t.price, t.quantity, "
        "t.amount, t.fee, t.reason, t.strategy_id, s.name AS strategy_name "
        "FROM trades t LEFT JOIN strategies s ON s.id = t.strategy_id "
        "WHERE t.account_id = ? AND t.trade_date = ? AND t.market = ? ORDER BY t.id",
        (account_id, day, profile.code),
    ).fetchall()

    current_quantity = {
        str(row["symbol"]): int(row["quantity"]) for row in position_rows
    }
    current_cost = {
        str(row["symbol"]): float(row["avg_cost"]) for row in position_rows
    }
    names = {
        str(row["symbol"]): row["name"] for row in position_rows if row["name"]
    }
    bought_quantity: dict[str, int] = {}
    sold_quantity: dict[str, int] = {}
    buy_cost: dict[str, float] = {}
    sell_proceeds: dict[str, float] = {}

    trades = []
    for row in trade_rows:
        symbol = str(row["symbol"])
        quantity = int(row["quantity"])
        amount = float(row["amount"])
        fee = float(row["fee"])
        if row["name"]:
            names[symbol] = row["name"]
        if row["side"] == "BUY":
            bought_quantity[symbol] = bought_quantity.get(symbol, 0) + quantity
            buy_cost[symbol] = buy_cost.get(symbol, 0.0) + amount + fee
        else:
            sold_quantity[symbol] = sold_quantity.get(symbol, 0) + quantity
            sell_proceeds[symbol] = sell_proceeds.get(symbol, 0.0) + amount - fee
        trades.append(
            {
                "traded_at": row["traded_at"],
                "symbol": symbol,
                "name": row["name"],
                "side": row["side"],
                "price": round(float(row["price"]), 3),
                "quantity": quantity,
                "amount": round(amount, 2),
                "fee": round(fee, 2),
                "reason": row["reason"],
                "strategy_id": row["strategy_id"],
                "strategy_name": row["strategy_name"],
            }
        )

    symbols = sorted(
        set(current_quantity)
        | set(bought_quantity)
        | set(sold_quantity)
    )
    symbol_reviews = []
    start_market_value = 0.0
    current_market_value = 0.0

    for symbol in symbols:
        quote = quotes_by_symbol.get(symbol)
        current_qty = current_quantity.get(symbol, 0)
        bought_qty = bought_quantity.get(symbol, 0)
        sold_qty = sold_quantity.get(symbol, 0)
        start_qty = current_qty - bought_qty + sold_qty
        if start_qty < 0:
            raise ReviewError(f"{symbol} 的期初持仓重建为负数，交易数据不一致")

        avg_cost = current_cost.get(symbol)
        if quote is not None:
            latest_price = quote.price
            prev_close = quote.prev_close
        else:
            latest_price = avg_cost or 0.0
            prev_close = latest_price
        if latest_price <= 0 or prev_close is None:
            raise ReviewError(f"{symbol} 缺少有效的最新价或昨收")

        start_value = start_qty * float(prev_close)
        current_value = current_qty * float(latest_price)
        symbol_buy_cost = buy_cost.get(symbol, 0.0)
        symbol_sell_proceeds = sell_proceeds.get(symbol, 0.0)
        pnl = (
            current_value
            + symbol_sell_proceeds
            - symbol_buy_cost
            - start_value
        )
        pnl_base = start_value + symbol_buy_cost
        pnl_pct = pnl / pnl_base * 100 if pnl_base > 0 else None

        start_market_value += start_value
        current_market_value += current_value
        symbol_reviews.append(
            {
                "symbol": symbol,
                "name": names.get(symbol),
                "start_quantity": start_qty,
                "current_quantity": current_qty,
                "bought_quantity": bought_qty,
                "sold_quantity": sold_qty,
                "avg_cost": round(avg_cost, 3) if avg_cost is not None else None,
                "prev_close": round(float(prev_close), 3),
                "latest_price": round(float(latest_price), 3),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            }
        )

    total_buy_cost = sum(buy_cost.values())
    total_sell_proceeds = sum(sell_proceeds.values())
    start_cash = float(account["cash"]) + total_buy_cost - total_sell_proceeds
    start_total_asset = start_cash + start_market_value
    current_total_asset = float(account["cash"]) + current_market_value
    daily_pnl = current_total_asset - start_total_asset
    daily_pnl_pct = (
        daily_pnl / start_total_asset * 100 if start_total_asset > 0 else None
    )

    strategy_performance = [
        {
            "strategy_id": int(row["strategy_id"]),
            "strategy_name": str(row["strategy_name"]),
            "trade_count": int(row["trade_count"]),
            "buy_amount": round(float(row["buy_amount"] or 0), 2),
            "sell_amount": round(float(row["sell_amount"] or 0), 2),
        }
        for row in connection.execute(
            "SELECT COALESCE(t.strategy_id, 0) AS strategy_id, "
            "COALESCE(s.name, '未关联策略') AS strategy_name, "
            "COUNT(*) AS trade_count, "
            "SUM(CASE WHEN t.side = 'BUY' THEN t.amount ELSE 0 END) AS buy_amount, "
            "SUM(CASE WHEN t.side = 'SELL' THEN t.amount ELSE 0 END) AS sell_amount "
            "FROM trades t LEFT JOIN strategies s ON s.id = t.strategy_id "
            "WHERE t.account_id = ? AND t.trade_date = ? AND t.market = ? "
            "GROUP BY COALESCE(t.strategy_id, 0), COALESCE(s.name, '未关联策略') "
            "ORDER BY strategy_id",
            (account_id, day, profile.code),
        ).fetchall()
    ]
    strategy_params = trader.load_strategy_params(connection, account_id=account_id)
    prompt_strategy_params = {
        name: value
        for name, value in strategy_params.items()
        if name != "custom_strategy_notes"
    }
    return {
        "trade_date": day,
        "as_of": latest_quotes[0].quoted_at if latest_quotes else None,
        "strategy_params": prompt_strategy_params,
        "strategy_notes_history": trader.load_strategy_notes(
            connection, account_id=account_id
        ),
        "strategies": trader.load_strategies(connection, account_id=account_id),
        "market_context": market_context,
        "strategy_performance": strategy_performance,
        "watchlist_candidates": [
            quote.as_prompt_dict() for quote in latest_quotes
        ],
        "performance": {
            "start_cash": round(start_cash, 2),
            "current_cash": round(float(account["cash"]), 2),
            "start_market_value": round(start_market_value, 2),
            "current_market_value": round(current_market_value, 2),
            "start_total_asset": round(start_total_asset, 2),
            "current_total_asset": round(current_total_asset, 2),
            "daily_pnl": round(daily_pnl, 2),
            "daily_pnl_pct": (
                round(daily_pnl_pct, 2) if daily_pnl_pct is not None else None
            ),
        },
        "symbols": symbol_reviews,
        "trades": trades,
        "previous_positions": len(position_rows),
        "chat_records": load_chat_records(
            connection, day, account_id=account_id
        ),
    }


def build_review_prompt(
    review: dict[str, Any], profile: MarketProfile = trader.PROFILE
) -> str:
    return (
        "请按 system 规则完成复盘并返回严格 JSON。以下是今日复盘数据：\n"
        + json.dumps(review, ensure_ascii=False, separators=(",", ":"))
    )


def validate_market_summary_notes(
    notes: str, profile: MarketProfile = trader.PROFILE
) -> None:
    keywords = profile.holiday_keywords
    if not any(keyword in notes for keyword in keywords):
        raise ReviewError("custom_strategy_notes 必须包含今日大盘或主要指数表现")
    if not re.search(r"\d+(?:\.\d+)?\s*点", notes):
        raise ReviewError(
            f"custom_strategy_notes 必须包含指数涨跌点数，例如“{profile.index_pool[0][1]}涨32.45点”"
        )


def _normalize_model_param(
    name: Any,
    value: Any,
    profile: MarketProfile = trader.PROFILE,
) -> tuple[str, Any]:
    if not isinstance(name, str):
        raise ReviewError("参数名必须是字符串")
    try:
        return name, trader.normalize_strategy_param(name, value, profile=profile)
    except trader.TraderError as exc:
        raise ReviewError(str(exc)) from exc


def _normalize_watchlist_payload(
    raw_watchlist: Any,
    *,
    require_watchlist: bool,
    allowed_watchlist: dict[str, str] | None,
    profile: MarketProfile = trader.PROFILE,
) -> list[dict[str, str]]:
    if raw_watchlist is None:
        if require_watchlist:
            raise ReviewError("模型没有返回 watchlist")
        return []
    if not isinstance(raw_watchlist, list):
        raise ReviewError("watchlist 必须是数组")
    if require_watchlist and not 3 <= len(raw_watchlist) <= 5:
        raise ReviewError("watchlist 必须包含 3 到 5 只股票")

    normalized: list[dict[str, str]] = []
    for item in raw_watchlist:
        if isinstance(item, str):
            item = {"symbol": item}
        if not isinstance(item, dict):
            raise ReviewError("watchlist 每一项必须是对象")
        raw_symbol = item.get("symbol")
        try:
            symbol = normalize_market_symbol(profile, raw_symbol)
        except ValueError as exc:
            raise ReviewError(f"watchlist 股票代码不合法: {raw_symbol!r}") from exc
        if allowed_watchlist is not None and symbol not in allowed_watchlist:
            raise ReviewError(f"watchlist 股票不在候选池中: {symbol}")
        name = str(item.get("name") or "").strip()
        if not name and allowed_watchlist is not None:
            name = str(allowed_watchlist.get(symbol) or "")
        normalized.append(
            {
                "symbol": symbol,
                "name": name,
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    try:
        return trader.normalize_watchlist(normalized, profile=profile)
    except trader.TraderError as exc:
        raise ReviewError(str(exc)) from exc


def _normalize_strategy_changes(raw_changes: Any) -> list[dict[str, Any]]:
    if raw_changes is None:
        return []
    if not isinstance(raw_changes, list):
        raise ReviewError("strategy_changes 必须是数组")
    if len(raw_changes) > trader.MAX_STRATEGY_CHANGES:
        raise ReviewError(
            f"strategy_changes 最多 {trader.MAX_STRATEGY_CHANGES} 项"
        )

    normalized: list[dict[str, Any]] = []
    for index, raw_change in enumerate(raw_changes, start=1):
        if not isinstance(raw_change, dict):
            raise ReviewError(f"strategy_changes[{index}] 必须是对象")
        action = str(raw_change.get("action") or "").strip().lower()
        if action not in {"create", "update", "retire"}:
            raise ReviewError(
                f"strategy_changes[{index}].action 必须是 create/update/retire"
            )
        change = dict(raw_change)
        change["action"] = action
        if action == "create":
            missing = [
                key for key in ("name", "description", "rules")
                if key not in change
            ]
            if missing:
                raise ReviewError(
                    f"strategy_changes[{index}] 缺少: {', '.join(missing)}"
                )
        else:
            try:
                change["id"] = trader._parse_strategy_id(change.get("id"))
            except trader.TraderError as exc:
                raise ReviewError(str(exc)) from exc
            if action == "update" and not any(
                key in change for key in ("name", "description", "rules", "status")
            ):
                raise ReviewError(
                    f"strategy_changes[{index}] 没有可更新的字段"
                )
        normalized.append(change)
    return normalized


def parse_review_result(
    payload: dict[str, Any],
    *,
    require_watchlist: bool = True,
    allowed_watchlist: dict[str, str] | None = None,
    profile: MarketProfile = trader.PROFILE,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    str,
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    if not isinstance(payload, dict):
        raise ReviewError("模型返回的 JSON 顶层不是对象")

    if "updates" in payload or "new_params" in payload:
        unknown = set(payload) - {
            "updates",
            "new_params",
            "custom_strategy_notes",
            "strategy_changes",
            "watchlist",
        }
        if unknown:
            raise ReviewError(f"返回了未知顶层字段: {', '.join(sorted(unknown))}")
        raw_updates = payload.get("updates", {})
        raw_new_params = payload.get("new_params", {})
        raw_notes = payload.get("custom_strategy_notes", "")
        raw_watchlist = payload.get("watchlist")
        raw_strategy_changes = payload.get("strategy_changes")
    else:
        raw_updates = {
            key: value
            for key, value in payload.items()
            if key not in {
                "custom_strategy_notes",
                "strategy_changes",
                "watchlist",
            }
        }
        raw_new_params = {}
        raw_notes = payload.get("custom_strategy_notes", "")
        raw_watchlist = payload.get("watchlist")
        raw_strategy_changes = payload.get("strategy_changes")

    if not isinstance(raw_updates, dict):
        raise ReviewError("updates 必须是对象")
    if not isinstance(raw_new_params, dict):
        raise ReviewError("new_params 必须是对象")
    if len(raw_new_params) > MAX_NEW_PARAMS:
        raise ReviewError(f"单次最多新增 {MAX_NEW_PARAMS} 个参数")
    if set(raw_updates) & set(raw_new_params):
        raise ReviewError("updates 和 new_params 存在重复参数")

    updates: dict[str, Any] = {}
    new_params: dict[str, Any] = {}
    for raw_updates_dict, target in (
        (raw_updates, updates),
        (raw_new_params, new_params),
    ):
        for key, value in raw_updates_dict.items():
            normalized_name, normalized_value = _normalize_model_param(
                key, value, profile
            )
            target[normalized_name] = normalized_value

    if "custom_strategy_notes" in updates:
        if raw_notes:
            raise ReviewError("custom_strategy_notes 重复出现")
        raw_notes = updates.pop("custom_strategy_notes")
    if "custom_strategy_notes" in new_params:
        raise ReviewError("custom_strategy_notes 不能放在 new_params 中")
    if not isinstance(raw_notes, str):
        raise ReviewError("custom_strategy_notes 必须是字符串")
    notes = raw_notes.strip()
    if not notes:
        raise ReviewError("模型没有返回有效的 custom_strategy_notes")
    strategy_changes = _normalize_strategy_changes(raw_strategy_changes)
    watchlist = _normalize_watchlist_payload(
        raw_watchlist,
        require_watchlist=require_watchlist,
        allowed_watchlist=allowed_watchlist,
        profile=profile,
    )
    return updates, new_params, notes, watchlist, strategy_changes


def update_strategy_params(
    connection: sqlite3.Connection,
    updates: dict[str, Any],
    notes: str,
    new_params: dict[str, Any] | None = None,
    watchlist: list[dict[str, str]] | None = None,
    note_day: str | None = None,
    profile: MarketProfile = trader.PROFILE,
    account_id: int = 1,
) -> dict[str, Any]:
    current = trader.load_strategy_params(connection, account_id=account_id)
    new_params = new_params or {}

    unknown_updates = set(updates) - set(current)
    if unknown_updates:
        raise ReviewError(
            f"updates 包含不存在的参数: {', '.join(sorted(unknown_updates))}"
        )
    duplicate_new = set(new_params) & set(current)
    if duplicate_new:
        raise ReviewError(
            f"new_params 包含已存在的参数: {', '.join(sorted(duplicate_new))}"
        )

    normalized_updates = {
        name: trader.normalize_strategy_param(name, value, profile=profile)
        for name, value in updates.items()
    }
    normalized_new = {
        name: trader.normalize_strategy_param(name, value, profile=profile)
        for name, value in new_params.items()
    }
    notes = trader.normalize_strategy_param(
        "custom_strategy_notes", notes, profile=profile
    )
    normalized_updates.update(normalized_new)
    normalized_updates["custom_strategy_notes"] = notes
    if watchlist is not None:
        normalized_updates["watchlist"] = trader.normalize_watchlist(
            watchlist, profile=profile
        )

    merged = dict(current)
    merged.update(normalized_updates)
    if float(merged["rsi_oversold"]) >= float(merged["rsi_overbought"]):
        raise ReviewError("rsi_oversold 必须小于 rsi_overbought")

    trader.save_strategy_params(
        connection,
        normalized_updates,
        profile=profile,
        account_id=account_id,
    )
    trader.save_strategy_note(
        connection,
        note_day or trader.trade_date(profile),
        notes,
        source="daily_review",
        profile=profile,
        account_id=account_id,
    )
    return trader.load_strategy_params(connection, account_id=account_id)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="评审当日交易表现并更新 strategy_params"
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
        "--date",
        help="评审日期 YYYY-MM-DD（默认: 当天）",
    )
    parser.add_argument(
        "--model",
        default=trader.DEFAULT_MODEL,
        help=f"DeepSeek 模型（默认: {trader.DEFAULT_MODEL}）",
    )
    parser.add_argument(
        "--api-url",
        default=trader.DEFAULT_API_URL,
        help=f"DeepSeek API 地址（默认: {trader.DEFAULT_API_URL}）",
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
        help="从 JSON 文件读取参数调整结果，跳过 DeepSeek 调用（调试用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="打印评审结果和调整方案，但不更新 strategy_params",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.timeout <= 0:
        LOGGER.error("--timeout 必须大于 0")
        return 2

    profile = get_profile(args.market or trader.MARKET)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else trader.DB_PATHS[profile.code].expanduser().resolve()
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    day = args.date or trader.trade_date(profile)

    if args.decision_file is None and not trader.is_trading_day_for_db(
        db_path, day, profile
    ):
        print(f"{day} 是{profile.label}休市日，跳过本次复盘")
        return 0

    if not trader.refresh_quotes(db_path, profile):
        print("行情更新失败，跳过本次复盘")
        return 1

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        trader.init_db(connection, profile=profile)
        account_id = trader.resolve_account_id(connection, profile.code, args.account)
        review = build_daily_review(connection, day, profile, account_id=account_id)
        prompt = build_review_prompt(review, profile)
        system_prompt = build_review_system_prompt(profile)

        LOGGER.info("=== REVIEW SYSTEM PROMPT ===\n%s", system_prompt)
        LOGGER.info("=== REVIEW USER PROMPT ===\n%s", prompt)

        if args.decision_file is not None:
            payload = json.loads(args.decision_file.read_text(encoding="utf-8"))
        else:
            api_key = trader.load_api_key()
            payload = trader.call_deepseek(
                api_key,
                prompt,
                api_url=args.api_url,
                model=args.model,
                timeout=args.timeout,
                system_prompt=system_prompt,
            )

        LOGGER.info(
            "=== REVIEW RAW RESPONSE ===\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

        allowed_watchlist = {
            str(item["symbol"]): str(item.get("name") or "")
            for item in review["watchlist_candidates"]
        }
        updates, new_params, notes, watchlist, strategy_changes = (
            parse_review_result(
                payload,
                allowed_watchlist=allowed_watchlist,
                profile=profile,
            )
        )
        if args.decision_file is None:
            validate_market_summary_notes(notes, profile)
        print("=== 当日表现 ===")
        print(json.dumps(review["performance"], ensure_ascii=False, indent=2))
        print("\n=== 参数调整方案 ===")
        print(json.dumps(updates, ensure_ascii=False, indent=2))
        print("\n=== 新增参数 ===")
        print(json.dumps(new_params, ensure_ascii=False, indent=2))
        print("\n=== 新战术总结 ===")
        print(notes)
        print("\n=== 策略库变更 ===")
        print(json.dumps(strategy_changes, ensure_ascii=False, indent=2))
        print("\n=== 明日观察名单 ===")
        print(json.dumps(watchlist, ensure_ascii=False, indent=2))

        if args.dry_run:
            print("\n[dry-run] 未更新 strategy_params")
            return 0

        new_params = update_strategy_params(
            connection,
            updates,
            notes,
            new_params=new_params,
            watchlist=watchlist,
            note_day=day,
            profile=profile,
            account_id=account_id,
        )
        applied_strategies = trader.apply_strategy_changes(
            connection,
            strategy_changes,
            source="daily_review",
            account_id=account_id,
        )
        print("\n=== 已更新参数 ===")
        print(json.dumps(new_params, ensure_ascii=False, indent=2))
        print("\n=== 已应用策略变更 ===")
        print(json.dumps(applied_strategies, ensure_ascii=False, indent=2))
        return 0
    except (
        ReviewError,
        trader.TraderError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        LOGGER.error("%s", exc)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())

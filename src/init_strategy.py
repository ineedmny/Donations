#!/usr/bin/env python3
"""基于经典量化策略和当前行情初始化 strategy_params。"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

import review
import trader
from market_profile import MarketProfile, get_profile


LOGGER = logging.getLogger("strategy_init")

INIT_SYSTEM_PROMPT_TEMPLATE = """你是一名 __MARKET_LABEL__量化策略研究员，需要为交易系统生成一套初始策略。

经典策略背景知识：
- 均线突破：价格站上并突破关键均线（如 20 日线）通常视为趋势转强；跌破均线则可能是趋势转弱。
- 动量轮动：买入近期相对强势、涨幅领先的股票，持有动量继续增强的标的，动量衰减时轮换。
- 超跌反弹：短期跌幅过大且出现止跌或反弹迹象时，可能存在修复机会，但不能忽视基本面风险和持续下跌风险。
- 网格交易：在震荡区间内高抛低吸，价格下跌分批买入、上涨分批卖出，适合波动较大但趋势不强的市场。

任务：
- 结合当前 __MARKET_LABEL__ 市场宽度、涨跌幅分布、振幅、5/10/20/60 日动量以及龙头和弱势股表现，判断当前更适合偏进攻、均衡还是防守。
- 生成一套初始参数和一条可执行的战术总结。
- 当前用户偏好高风险激进模拟：允许满仓、集中持仓，能接受大幅甚至全部本金损失。
- __MARKET_RULE__，但不得因害怕回撤而把参数调得过度保守。
- 买入数量必须是 __LOT_SIZE__ 股的整数倍。

只返回一个严格 JSON 对象，不要 Markdown，不要前后解释。必须使用：
{"updates":{"max_single_position":1.0,"stop_loss_pct":-0.90,"max_daily_trades":10,"rsi_oversold":45,"rsi_overbought":85,"atr_stop_multiplier":5,"min_turnover":100000000,"market_regime_weight":0.35},"new_params":{},"custom_strategy_notes":"__INDEX_EXAMPLE__涨32.45点、股票池28涨2跌，普涨环境；完整的战术总结"}

约束：
- updates 必须包含 max_single_position、stop_loss_pct、max_daily_trades、rsi_oversold、rsi_overbought、atr_stop_multiplier、min_turnover、market_regime_weight。
- max_single_position 必须满足 0 < value <= 1。
- stop_loss_pct 必须满足 -1 < value < 0。
- max_daily_trades 必须是 1 到 50 之间的整数。
- rsi_oversold 和 rsi_overbought 必须满足 0 <= value <= 100，且 rsi_oversold 必须小于 rsi_overbought。
- atr_stop_multiplier 必须满足 0 < value <= 10。
- min_turnover 必须是非负数，单位为 __CURRENCY__。
- market_regime_weight 必须满足 0 <= value <= 1，表示整体大盘环境对决策的影响权重。
- new_params 可新增策略参数；没有新增时返回空对象。
- custom_strategy_notes 不限制字数，必须完整、具体、可执行。
"""


def build_init_system_prompt(profile: MarketProfile) -> str:
    if profile.t_plus_one:
        market_rule = "必须考虑 A 股 T+1、涨跌停和题材轮动较快的特点"
    else:
        market_rule = (
            "必须考虑美股 T+0、无涨跌停、科技龙头权重较高且常规交易时段外的行情不参与成交"
        )
    index_example = profile.index_pool[0][1] if profile.index_pool else "主要指数"
    return (
        INIT_SYSTEM_PROMPT_TEMPLATE.replace("__MARKET_LABEL__", profile.label)
        .replace("__MARKET_RULE__", market_rule)
        .replace("__INDEX_EXAMPLE__", index_example)
        .replace("__CURRENCY__", profile.currency)
        .replace("__LOT_SIZE__", str(profile.lot_size))
    )


INIT_SYSTEM_PROMPT = build_init_system_prompt(get_profile("CN"))


class InitStrategyError(RuntimeError):
    """初始化策略流程中的可预期错误。"""


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_market_snapshot(
    connection: sqlite3.Connection,
    profile: MarketProfile = trader.PROFILE,
    *,
    account_id: int = 1,
) -> dict[str, Any]:
    quotes, _ = trader.load_latest_quotes(
        connection, 10_000, profile, account_id=account_id
    )
    if not quotes:
        raise InitStrategyError(f"没有可用的最新{profile.label}行情")

    changes = [quote.change_pct for quote in quotes if quote.change_pct is not None]
    amplitudes = [
        quote.amplitude for quote in quotes if quote.amplitude is not None
    ]
    momentum_5d = [
        quote.change_pct_5d for quote in quotes if quote.change_pct_5d is not None
    ]
    momentum_20d = [
        quote.change_pct_20d for quote in quotes if quote.change_pct_20d is not None
    ]
    sorted_quotes = sorted(
        quotes,
        key=lambda quote: quote.change_pct
        if quote.change_pct is not None
        else float("-inf"),
        reverse=True,
    )

    def quote_summary(quote: trader.Quote) -> dict[str, Any]:
        return {
            "symbol": quote.symbol,
            "name": quote.name,
            "price": trader._round(quote.price, 3),
            "change_pct": trader._round(quote.change_pct, 2),
            "amplitude": trader._round(quote.amplitude, 2),
            "change_pct_5d": trader._round(quote.change_pct_5d, 2),
            "change_pct_20d": trader._round(quote.change_pct_20d, 2),
            "change_pct_60d": trader._round(quote.change_pct_60d, 2),
        }

    return {
        "as_of": quotes[0].quoted_at,
        "universe_size": len(quotes),
        "market_context": trader.build_market_snapshot(
            quotes, trader.load_market_indices(connection, profile)
        ),
        "breadth": {
            "advancers": sum(value > 0 for value in changes),
            "decliners": sum(value < 0 for value in changes),
            "flat": sum(value == 0 for value in changes),
            "average_change_pct": trader._round(_average(changes), 2),
            "average_amplitude": trader._round(_average(amplitudes), 2),
            "average_change_pct_5d": trader._round(_average(momentum_5d), 2),
            "average_change_pct_20d": trader._round(_average(momentum_20d), 2),
        },
        "leaders": [quote_summary(quote) for quote in sorted_quotes[:5]],
        "laggards": [quote_summary(quote) for quote in sorted_quotes[-5:]],
        "quotes": [quote.as_prompt_dict() for quote in quotes],
    }


def build_init_prompt(
    snapshot: dict[str, Any],
    profile: MarketProfile = trader.PROFILE,
) -> str:
    current_params = {
        name: value
        for name, value in trader.DEFAULT_STRATEGY_PARAMS.items()
        if name != "custom_strategy_notes"
    }
    return (
        f"当前{profile.label}行情特征如下，请据此生成初始策略参数、可选新参数和战术总结：\n"
        + json.dumps(
            {
                "market": snapshot,
                "reference_defaults": current_params,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def parse_init_result(
    payload: dict[str, Any],
    profile: MarketProfile = trader.PROFILE,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    updates, new_params, notes, _watchlist, _strategy_changes = (
        review.parse_review_result(
            payload,
            require_watchlist=False,
            profile=profile,
        )
    )
    required = set(trader.DEFAULT_STRATEGY_PARAMS) - {
        "custom_strategy_notes",
        "watchlist",
    }
    missing = required - set(updates)
    if missing:
        raise InitStrategyError(
            f"初始参数缺少: {', '.join(sorted(missing))}"
        )
    return updates, new_params, notes


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用经典量化策略和当前行情初始化 strategy_params"
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
        help="从 JSON 文件读取初始化结果，跳过 DeepSeek 调用（调试用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="打印行情特征和初始化结果，但不写入数据库",
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
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        trader.init_db(connection, profile=profile)
        account_id = trader.resolve_account_id(connection, profile.code, args.account)
        snapshot = build_market_snapshot(
            connection, profile, account_id=account_id
        )
        prompt = build_init_prompt(snapshot, profile)
        system_prompt = build_init_system_prompt(profile)

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

        updates, new_params, notes = parse_init_result(payload, profile)
        print("=== 当前市场特征 ===")
        print(json.dumps(snapshot["breadth"], ensure_ascii=False, indent=2))
        print("\n=== 初始参数 ===")
        print(json.dumps(updates, ensure_ascii=False, indent=2))
        print("\n=== 新增参数 ===")
        print(json.dumps(new_params, ensure_ascii=False, indent=2))
        print("\n=== 初始战术总结 ===")
        print(notes)

        if args.dry_run:
            print("\n[dry-run] 未更新 strategy_params")
            return 0

        new_params = review.update_strategy_params(
            connection,
            updates,
            notes,
            new_params=new_params,
            profile=profile,
            account_id=account_id,
        )
        print("\n=== 已写入 strategy_params ===")
        print(json.dumps(new_params, ensure_ascii=False, indent=2))
        return 0
    except (
        InitStrategyError,
        review.ReviewError,
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

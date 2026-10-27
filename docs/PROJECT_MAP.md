# 项目地图

最后核对：2026-10-27

> 开源副本初始不包含行情、账户交易、聊天、日志和 Prompt 调试数据。首次运行请先执行 `./scripts/init_fresh.sh`，详见 `docs/GETTING_STARTED.md`。

## 1. 项目目标

这是一个 A 股与美股双市场实时模拟交易闭环：A 股主账户使用固定龙头池，A 股小额账户使用全市场动态可买池，美股使用固定池；采集行情和技术指标后调用 DeepSeek 生成买卖决策，在本地账户中模拟成交并记录持仓；收盘后根据各账户盈亏、大盘、聊天记录和历史笔记复盘，自动调整参数、更新策略库和次日观察名单；最后通过同一个 FastAPI 站点展示各账户。

两个市场共用代码；A 股两个账户共享同一数据库的行情、指数和交易日历，但账户、持仓、成交、策略参数、策略库、复盘笔记、观察名单、聊天记录和页面 API 按 `account_id` 隔离；美股使用独立数据库。它不连接券商、不执行真实委托。`--live` 只表示把模拟交易写入本地数据库，不表示真实下单。

## 2. 固定事实

| 项目 | A 股 `CN` | 美股 `US` |
| --- | --- | --- |
| 账户 | `main`、`small-3000` | `main` |
| 页面 | 主账户 `/`、`/chat`；小额账户 `/accounts/small-3000`、`/accounts/small-3000/chat` | `/us`、`/us/chat` |
| 数据库 | `data/quotes.db` | `data/us_quotes.db` |
| 本金 | 主账户 `1,800,000.00 CNY`；小额账户 `3,000.00 CNY` | `45,000.00 USD` |
| 结算 | T+1 | T+0 |
| 最小单位 | 100 股整数倍 | 1 股整数倍 |
| 交易时段 | 09:30-11:30、13:00-15:00 北京时间 | 09:30-16:00 美东时间，含夏令时 |
| 行情刷新 | 交易时段每 10 秒只刷新已选股票；每 5 分钟全市场扫描并重建动态池；11:30 额外采集一次 | 盘中随交易刷新 |
| 股票池 | 主账户固定约 30 只龙头；小额账户全市场动态池（成交额 ≥ 2 亿元、一手含费 ≤ 3,000 元、最多 30 只） | `VOO`、`QQQM`、`SPCX`、`GOOG`、`RKLB`、`IREN`、`AAOI` |
| 指数 | 上证指数、深证成指、创业板指 | 标普500、纳斯达克综合、道琼斯工业 |
| 行情源 | 腾讯 | 实时与指数优先腾讯，历史日线 akshare |
| 复盘计划 | 主账户 15:10；小额账户 15:12 北京时间 | 周二至周六 06:10 北京时间 |

共同事实：

- 项目根目录：仓库根目录。
- 数据库时间戳统一保存带时区的北京时间；美股 `trade_date` 使用 `America/New_York` 日期。
- 决策与复盘模型：DeepSeek `deepseek-chat`，Key 只来自 `DEEPSEEK_API_KEY`。
- 只做多，不使用融资融券，不做碎股。
- Web：FastAPI，同一个进程同时提供两个市场页面与 API。

## 3. 目录结构

```text
mny
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── docs/
│   ├── GETTING_STARTED.md
│   └── PROJECT_MAP.md
├── src/
│   ├── market_profile.py
│   ├── account_store.py
│   ├── collector.py
│   ├── trader.py
│   ├── review.py
│   ├── init_strategy.py
│   ├── web.py
│   └── config.py
├── scripts/
│   ├── init_fresh.sh
│   ├── run_with_env.sh
│   ├── restart_web.sh
│   ├── refresh_selected_quotes.sh
│   └── collect_cn_full_market.sh
├── tests/
│   ├── helpers.py
│   ├── test_accounts.py
│   ├── test_cn_stock_pool.py
│   ├── test_market_rules.py
│   ├── test_us_collector.py
│   ├── test_web_accounts.py
│   └── test_web_markets.py
├── data/                 # 首次初始化后生成数据库，提交时为空
├── logs/                 # 运行时生成日志，提交时为空
├── debug/                # 运行时生成 Prompt 调试文件，提交时为空
├── backups/              # 手工备份目录，提交时为空
└── run/                  # PID 和锁文件目录，提交时为空
```

## 4. 模块职责

| 文件 | 职责 | 常见改动入口 |
| --- | --- | --- |
| `src/market_profile.py` | 集中定义 CN/US 的股票池、指数、币种、本金、手数、T+1/T+0、费用、时区、时段和节假日 | 市场规则、股票池、本金、时段 |
| `src/account_store.py` | 账户 schema、旧单账户迁移、账户股票池读写、全市场小账户动态筛选 | 账户表、迁移、动态池规则 |
| `src/config.py` | 默认市场、双数据库路径、Prompt 调试文件路径 | `CN_QUOTES_DB`、`US_QUOTES_DB`、`MARKET` |
| `src/collector.py` | 按市场拉取全市场或已选股票行情和指数；刷新动态账户股票池；规范化行情；写入行情表 | 行情源、代码规范化、指数适配、动态池刷新、`--selected-only` |
| `src/trader.py` | 交易日历、账户解析、账户状态、策略参数、全部历史笔记、Prompt、DeepSeek 决策、止损、模拟撮合、技术指标与写库 | 交易规则、账户隔离、Prompt、风控、成交价、数据库迁移 |
| `src/review.py` | 计算指定账户当日盈亏和市场环境，读取账户聊天/参数/策略库/全部历史笔记，调用 DeepSeek 复盘并更新参数、笔记、策略和观察名单 | 复盘规则、账户隔离、参数校验、策略进化、笔记落库 |
| `src/init_strategy.py` | 用经典量化策略和市场特征初始化参数与战术笔记 | 首次部署、清空后的初始化 |
| `src/web.py` | 双市场、多账户 FastAPI 看板、JSON API、AI 问答页、市场和账户切换 | 页面布局、API 字段、格式化、聊天、账户路由 |
| `scripts/init_fresh.sh` | 在空目录中创建 CN/US 数据库、默认账户和默认策略 | 首次部署、空库初始化 |
| `scripts/run_with_env.sh` | 加载 `.env`、切换到项目根目录后执行命令 | 本地运行、cron 环境包装 |
| `scripts/restart_web.sh` | 停止旧 Web、启动 uvicorn、健康检查、写 PID 和日志 | 部署和运维脚本 |
| `scripts/refresh_selected_quotes.sh` | 交易时段每分钟启动，内部每 10 秒刷新固定池、动态池与持仓；不扫描全市场 | 高频报价 |
| `scripts/collect_cn_full_market.sh` | 交易时段每 5 分钟扫描全市场并重建动态池 | 全市场选池 |

## 5. 数据库表

两个数据库使用同一套表结构，但各自只保存本市场业务数据。

| 表 | 用途 | 关键点 |
| --- | --- | --- |
| `accounts` | 账户元数据 | `(market, slug)` 唯一；`principal` 独立记录本金；`pool_mode` 区分固定池和可买池；`pool_min_turnover`、`pool_max_lot_cost`、`pool_limit` |
| `account_stock_pool` | 账户级股票池 | 以 `(account_id, symbol)` 为主键；保存价格、成交额、一手成本、筛选理由和刷新时间 |
| `positions` | 当前持仓 | 以 `(account_id, symbol)` 为主键；数量、成本、最新价、市值、仓位占比 |
| `trades` | 成交记录 | `account_id`、买卖方向、成交价、数量、金额、费用、原因、`strategy_id`、`market` |
| `quotes` | 最新股票行情与指标 | 以 `(market, symbol)` 为主键；含 OHLC、量额、5/10/20/60 日涨跌、MA/RSI/MACD/ATR |
| `market_indices` | 市场指数快照 | 点位、涨跌点数、涨跌幅；按市场档案排序展示 |
| `daily_closes` | 历史日线 | 计算均线、RSI、MACD、ATR；美股通过 akshare 拉取，并过滤周末与 NYSE 休市日 |
| `strategy_params` | 动态策略参数 | 以 `(account_id, param_name)` 为主键；最新笔记保留在 `custom_strategy_notes` |
| `strategy_notes` | 每日复盘笔记历史 | 按 `(account_id, trade_date)` 唯一；交易和复盘读取该账户全部记录 |
| `strategies` | 投资策略库 | 按账户隔离；`active` 才可被交易选择；记录使用次数 |
| `decision_analysis` | 每轮模型决策快照 | 按账户保存行情、市场分析、决策 JSON |
| `chat_messages` | 看板问答历史 | 按 `account_id` 隔离；复盘学习材料 |
| `trading_calendar` | 交易日缓存 | CN 使用新浪日历；US 使用 NYSE 常规休市规则 |

## 6. 关键数据流

### 6.1 行情采集

`collector.py --market CN|US` 按市场档案拉取行情和指数，写入对应数据库的 `quotes`、`market_indices`。CN 有两个频率：交易时段每 10 秒的 `--selected-only` 只读取固定池、所有 active 账户动态池与当前持仓的并集，更新行情但不扫描全市场、不改变池成员；每 5 分钟的全市场采集先获取全市场快照，再补充主账户固定池详情，随后刷新所有 `pool_mode='affordable'` 账户的 `account_stock_pool`。动态池只保留成交额 ≥ 2 亿元、一手含费不超过账户上限、最多 30 只的普通股票；只有池内股票全部刷新成功，`refreshed_at` 才更新为本次 `quoted_at`。所有 collector 进程在写库期间通过数据库旁的 `.collector.lock` 文件锁串行，避免高频报价与全市场重建交错导致时间戳失真。美股实时行情先请求腾讯 `qt.gtimg.cn`，失败时回退东方财富；历史日线由 trader 补拉。美股采集成功后，会清理 `quotes` 和 `daily_closes` 中不在当前固定股票池内的缓存，账户、持仓、成交、策略、笔记和聊天数据不受影响。采集失败时记录 warning，不覆盖为无效行情。

### 6.2 交易

`trader.py --market CN|US --account SLUG` 先检查对应市场的交易日历和交易时段，再以 `--selected-only` 调用采集器刷新行情；解析默认账户或指定 slug，加载该账户的持仓、策略参数、策略库和全部历史笔记；Python 先执行止损，再让 DeepSeek 返回严格 JSON；成交前再次刷新已选股票并按执行瞬间最新价模拟成交；最后写该账户的 `trades`、`positions`、`accounts` 和 `decision_analysis`。动态账户若股票池刷新时间与最新行情不一致，会拒绝交易。

### 6.3 复盘

`review.py --market CN|US --account SLUG` 计算指定账户的当日盈亏和整体大盘环境，读取该账户聊天、参数、策略库、全部历史笔记；DeepSeek 返回参数更新、新参数、新笔记、策略变更和观察名单；代码校验后写入该账户的 `strategy_params`、`strategy_notes`、`strategies`。

### 6.4 Web

`web.py` 根据路由选择市场和数据库，不依赖全局可变的当前市场：

| 路由 | 市场 | 数据库 |
| --- | --- | --- |
| `/` | CN | `data/quotes.db` |
| `/chat` | CN | `data/quotes.db` |
| `/api/dashboard` | CN | `data/quotes.db` |
| `/api/chat`、`/api/chat/history` | CN | `data/quotes.db` |
| `/accounts/small-3000`、`/accounts/small-3000/chat` | CN 小额账户 | `data/quotes.db` |
| `/accounts/small-3000/api/dashboard` | CN 小额账户 | `data/quotes.db` |
| `/accounts/small-3000/api/chat`、`/accounts/small-3000/api/chat/history` | CN 小额账户 | `data/quotes.db` |
| `/us` | US | `data/us_quotes.db` |
| `/us/chat` | US | `data/us_quotes.db` |
| `/us/api/dashboard` | US | `data/us_quotes.db` |
| `/us/api/chat`、`/us/api/chat/history` | US | `data/us_quotes.db` |

页面顶部提供账户选择器，可切换 A 股主账户、A 股小额账户和美股；金额、标题和币种由市场档案生成，美股使用 `$`，A 股使用 `¥`。股票池保留在数据库和 `/api/dashboard` 数据中供交易筛选，不在看板 HTML 中展示。

## 7. 外部依赖与失败策略

- 腾讯行情：CN 股票池与指数；US 实时股票池与指数。失败时 US 实时行情回退东方财富，指数回退新浪。
- akshare：CN 交易日历、美股历史日线；失败时记录 warning，指标缺失不伪造。
- DeepSeek：请求失败重试；返回必须是可解析 JSON；交易和复盘均有严格字段校验。
- 交易日历：优先读取 `trading_calendar`；CN 缺失时通过 akshare 拉取，US 缺失时按 NYSE 规则生成；失败回退工作日判断。
- 时间：数据库时间戳为 `Asia/Shanghai`；美股交易日按纽约时区计算。

## 8. 不可破坏的约束

- 成交价使用执行瞬间的 `quote.price`，不使用开盘价/收盘价/昨收。
- A 股 T+1，当日买入不能当日卖出；买入数量必须是 100 股整数倍。
- 美股 T+0，允许当日买入后卖出；数量必须是 1 股整数倍。
- 已持仓股票不能重复 BUY。
- 两个市场不得串库、串策略、串聊天或串 API。
- A 股主账户与小额账户不得串持仓、成交、策略、笔记或聊天；行情、指数和交易日历可以共享。
- 动态股票池必须随最新行情刷新；股票池时间戳落后于行情时不得交易。
- 10 秒高频刷新不得扫描全市场或改变动态池成员；池内任一股票刷新失败时不得推进动态池时间戳。
- Python 强制止损优先于模型决策。
- `strategy_notes_history` 必须包含全部历史笔记，不能只传最新一条。
- 只有 `strategies.status = 'active'` 的策略可被选择。
- `principal` 独立于持仓市值，用于计算相对本金的盈亏。

## 9. 变更影响地图

| 想改什么 | 先看哪里 |
| --- | --- |
| 市场规则/本金/固定股票池/指数 | `src/market_profile.py` |
| 账户表、迁移、动态股票池规则 | `src/account_store.py` |
| 双数据库路径 | `src/config.py` |
| A 股行情字段 | `src/collector.py` 的腾讯解析函数 |
| 美股实时/指数/历史行情 | `src/collector.py` 的 US 适配器、`src/trader.py` 的 `_fetch_daily_bars` |
| 指标计算 | `src/trader.py` 的 `compute_technical_indicators` |
| 交易 Prompt | `src/trader.py` 的 `build_system_prompt`、`build_prompt` |
| 参数范围/新增参数 | `src/trader.py` 的 `normalize_strategy_param`、`DEFAULT_STRATEGY_PARAMS` |
| 交易执行/费用 | `src/trader.py` 的 `simulate`、`apply_state`、`calculate_fee` |
| 复盘规则 | `src/review.py` 的 `build_review_system_prompt`、`parse_review_result` |
| 初始化 Prompt | `src/init_strategy.py` 的 `build_init_system_prompt` |
| 笔记历史 | `src/trader.py` 的 `strategy_notes`、`save_strategy_note`、`load_strategy_notes` |
| 网页页面/API/账户与市场切换 | `src/web.py` 的 `load_dashboard_data`、`render_dashboard`、账户路由 |
| 启动/端口 | `scripts/restart_web.sh`、systemd `mny-web.service` |

## 10. 文档维护

- 结构性改动：更新 `docs/PROJECT_MAP.md`。
- 安装、首次运行、定时任务和运维方式变化：更新 `docs/GETTING_STARTED.md`。
- 面向读者的项目入口变化：更新 `README.md`。
- 本开源副本不附带行情、账户交易、聊天、日志或历史变更记录。
- 本文件只描述稳定事实；临时调试细节放到日志，不塞进项目地图。

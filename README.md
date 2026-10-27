# Donation

A 股 / 美股双市场 AI 模拟交易系统。同一套 Python 与 FastAPI 代码，同时维护三个相互隔离的模拟账户：

| 市场 | 账户 | 页面 | 初始本金 | 规则 |
| --- | --- | --- | --- | --- |
| A 股 | `main` | `/` | 1,800,000 CNY | T+1，100 股整数倍，固定龙头股票池 |
| A 股 | `small-3000` | `/accounts/small-3000` | 3,000 CNY | T+1，100 股整数倍，全市场动态低价池 |
| 美股 | `main` | `/us` | 45,000 USD | T+0，1 股整数倍，固定股票池 |

> 本项目只连接公开行情源和 AI JSON API，不连接券商，不执行真实委托。`--live` 的含义只是“把模拟交易写入本地 SQLite”，并不代表真实下单。

## 功能

- 腾讯行情实时采集，A 股全市场扫描与动态股票池，akshare 美股历史日线。
- MA、RSI、MACD、ATR 等技术指标。
- DeepSeek 生成交易决策，并在 Python 侧强制执行仓位、止损、现金、手数和 T+1/T+0 约束。
- 交易、复盘、策略库、复盘笔记和聊天记录按市场或账户隔离。
- 收盘复盘会更新策略参数、策略库、观察名单和历史经验。
- FastAPI 手机端看板、JSON API 和 AI 交易问答。
- SQLite 自动建表与幂等迁移，适合个人服务器或本地研究。

## 工作流

```text
腾讯行情 / akshare
        |
        v
collector.py  行情、指数、技术指标
        |
        v
trader.py     行情 + 参数 + 历史笔记 + 策略库 -> DeepSeek -> 模拟成交
        |
        v
review.py     盈亏 + 大盘 + 聊天 + 历史笔记 -> 参数 / 策略 / 笔记
        |
        v
web.py        FastAPI 看板与问答
```

## 快速开始

要求 Python 3.10+、SQLite 和可访问行情源的网络环境。

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

set -a
source .env
set +a

# 创建空的 A 股和美股数据库，仅写入内置默认配置
./scripts/init_fresh.sh

# 首次采集行情
python3 src/collector.py --market CN
python3 src/collector.py --market US

# 启动看板
WEB_HOST=127.0.0.1 WEB_PORT=8000 ./scripts/restart_web.sh
```

浏览器打开：

- A 股主账户：`http://127.0.0.1:8000/`
- A 股小额账户：`http://127.0.0.1:8000/accounts/small-3000`
- 美股：`http://127.0.0.1:8000/us`

完整安装、首次运行、定时任务、重置与排障教程见 [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)。

## 交易命令

不带 `--live` 时只打印决策和执行结果，不写账户：

```bash
python3 src/trader.py --market CN
python3 src/trader.py --market CN --account small-3000
python3 src/trader.py --market US
```

确认模拟链路正常后才写入本地模拟账户：

```bash
python3 src/trader.py --market CN --live
python3 src/trader.py --market CN --account small-3000 --live
python3 src/trader.py --market US --live
```

收盘复盘与策略初始化：

```bash
python3 src/review.py --market CN
python3 src/review.py --market CN --account small-3000
python3 src/review.py --market US
python3 src/init_strategy.py --market CN
python3 src/init_strategy.py --market US
```

`trader.py`、`review.py`、`init_strategy.py` 和网页问答需要 `DEEPSEEK_API_KEY`；只采集行情不需要。

## 数据目录

```text
data/       SQLite 数据库，默认不提交
logs/       运行日志，默认不提交
debug/      Prompt 调试文件，默认不提交
backups/    手工备份，默认不提交
run/        PID 与锁文件，默认不提交
```

新仓库不包含任何行情、持仓、成交、聊天、日志或 Prompt 调试数据。首次初始化会生成默认账户、参数、策略和固定候选池；这些是内置配置，不是历史交易记录。

## 测试

```bash
python3 -B -m unittest discover -s tests -v
```

测试使用临时数据库，不会改动 `data/` 下的业务数据。

## 部署提醒

- 看板默认绑定 `127.0.0.1`，没有登录和权限系统。公开到公网前请加反向代理鉴权。
- 不要提交 `.env`、数据库、日志或 Prompt 调试文件。
- 公开部署时建议用独立系统用户运行，并限制 `data/` 的读写权限。
- 本项目不提供收益保证、投资建议或券商交易能力。
- 仓库未预置开源许可证；正式发布前请根据你的意愿补充 `LICENSE`。

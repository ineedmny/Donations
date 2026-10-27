# 从零开始使用 mny

这份教程面向第一次拿到项目的人。按顺序执行即可从空仓库运行到看板、模拟交易和每日复盘。

## 0. 先理解它是什么

`mny` 是一个本地模拟交易系统，不是实盘券商客户端：

- 行情来自腾讯和 akshare。
- 买卖决策与复盘由 DeepSeek 生成。
- Python 强制检查现金、仓位、止损、手数、T+1/T+0 和单日交易次数。
- 成交只写入本地 SQLite，不发送给任何券商。
- `--live` 只表示“写入模拟账户”，仍然是模拟交易。
- 看板没有登录系统，默认仅监听 `127.0.0.1`。

默认三个账户：

| 市场 | 账户 slug | 页面 | 本金 |
| --- | --- | --- | --- |
| A 股 | `main` | `/` | 1,800,000 CNY |
| A 股 | `small-3000` | `/accounts/small-3000` | 3,000 CNY |
| 美股 | `main` | `/us` | 45,000 USD |

## 1. 环境要求

推荐 Ubuntu 22.04 或更新版本，也可以使用其他 Linux/macOS 环境。

- Python 3.10+。
- 能访问腾讯行情、akshare 数据源和 DeepSeek API。
- 至少预留 100 MB 空间用于代码、虚拟环境依赖和数据库增长。
- 如果运行定时任务，需要 `cron` 和 `flock`。

检查版本：

```bash
python3 --version
sqlite3 --version
```

Debian/Ubuntu 缺少基础工具时：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip sqlite3 cron
```

## 2. 安装依赖

进入项目根目录，创建独立虚拟环境：

```bash
cd /path/to/mny
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

以后每次在新的终端运行项目，都先执行：

```bash
cd /path/to/mny
source .venv/bin/activate
```

## 3. 配置 DeepSeek Key

只采集行情不需要 Key；交易、复盘、策略初始化和网页问答需要。

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

```dotenv
DEEPSEEK_API_KEY=你的真实Key
```

不要把真实 Key 写进 README、脚本、crontab 命令行、日志或 Git 提交。

项目自带一个小包装脚本，会自动读取 `.env`、切换到项目目录，再执行命令：

```bash
./scripts/run_with_env.sh python3 src/trader.py --market CN
```

如果不想使用包装脚本，也可以在当前 shell 手动加载：

```bash
set -a
source .env
set +a
```

## 4. 初始化空数据库

仓库初始状态没有 `data/quotes.db` 和 `data/us_quotes.db`。执行：

```bash
./scripts/init_fresh.sh
```

它会分别执行：

```bash
python3 src/trader.py --market CN --init-db
python3 src/trader.py --market US --init-db
```

初始化只创建表、默认账户、默认参数、默认策略和固定候选池，并写入项目内置的默认战术说明。这些是内置配置，不是历史交易数据；不会写入行情、持仓、成交或聊天记录。重复执行 `--init-db` 是安全的。

如果脚本提示数据库已经存在，它不会自动删除。需要重建时请先阅读本文第 10 节。

## 5. 采集第一份行情

```bash
python3 src/collector.py --market CN
python3 src/collector.py --market US
```

A 股的第一次全市场扫描还会重建 `small-3000` 账户的动态股票池。交易系统只会使用最新且完整的行情，行情失败时不会用旧价格强行成交。

检查数据：

```bash
sqlite3 data/quotes.db "SELECT market, COUNT(*), MAX(quoted_at) FROM quotes GROUP BY market;"
sqlite3 data/us_quotes.db "SELECT market, COUNT(*), MAX(quoted_at) FROM quotes GROUP BY market;"
```

A 股的账户股票池：

```bash
sqlite3 data/quotes.db "SELECT a.slug, COUNT(p.symbol) FROM accounts a LEFT JOIN account_stock_pool p ON p.account_id=a.id GROUP BY a.id;"
```

## 6. 启动看板

```bash
WEB_HOST=127.0.0.1 WEB_PORT=8000 ./scripts/restart_web.sh
```

打开：

- A 股主账户：`http://127.0.0.1:8000/`
- A 股小额账户：`http://127.0.0.1:8000/accounts/small-3000`
- 美股：`http://127.0.0.1:8000/us`
- A 股问答：`http://127.0.0.1:8000/chat`
- 美股问答：`http://127.0.0.1:8000/us/chat`

查看状态和日志：

```bash
cat run/web.pid
tail -n 50 logs/web.log
```

停止由脚本直接启动的 Web 进程：

```bash
kill "$(cat run/web.pid)"
```

如果机器上存在名为 `mny-web.service` 的 systemd 服务，脚本会优先重启该服务。没有该服务时，脚本使用 `nohup` 启动本地进程。

需要从其他设备访问时：

```bash
WEB_HOST=0.0.0.0 WEB_PORT=8000 ./scripts/run_with_env.sh ./scripts/restart_web.sh
```

不要在没有反向代理鉴权的情况下直接暴露到公网。

## 7. 运行模拟交易

先做 dry-run。它仍然会调用 DeepSeek，但不会写入账户、持仓或成交：

```bash
./scripts/run_with_env.sh python3 src/trader.py --market CN
./scripts/run_with_env.sh python3 src/trader.py --market CN --account small-3000
./scripts/run_with_env.sh python3 src/trader.py --market US
```

确认输出和风险参数符合预期后，才加 `--live`：

```bash
./scripts/run_with_env.sh python3 src/trader.py --market CN --live
./scripts/run_with_env.sh python3 src/trader.py --market CN --account small-3000 --live
./scripts/run_with_env.sh python3 src/trader.py --market US --live
```

重要区别：

- 不加 `--live`：只打印，不写模拟账户。
- 加 `--live`：写入本地模拟账户，不是真实券商订单。
- `--allow-closed-market`：只用于非交易时段测试流程，不要放进正式 cron。

成交价使用执行瞬间的最新价。A 股执行 T+1、100 股整数倍和费用规则；美股执行 T+0、1 股整数倍规则。

## 8. 收盘复盘

复盘会读取该账户的当日表现、大盘、聊天记录、策略库和全部历史复盘笔记，然后更新参数、策略和笔记。

```bash
./scripts/run_with_env.sh python3 src/review.py --market CN
./scripts/run_with_env.sh python3 src/review.py --market CN --account small-3000
./scripts/run_with_env.sh python3 src/review.py --market US
```

建议在交易日收盘后运行。非交易日默认跳过。

查看复盘结果：

```bash
sqlite3 data/quotes.db "SELECT account_id, trade_date, source, updated_at FROM strategy_notes ORDER BY updated_at DESC LIMIT 10;"
sqlite3 data/us_quotes.db "SELECT account_id, trade_date, source, updated_at FROM strategy_notes ORDER BY updated_at DESC LIMIT 10;"
```

## 9. 可选：初始化经典策略参数

在已经采集行情后，可以用 `init_strategy.py` 让模型结合当前行情调整初始策略。先 dry-run：

```bash
./scripts/run_with_env.sh python3 src/init_strategy.py --market CN --dry-run
./scripts/run_with_env.sh python3 src/init_strategy.py --market US --dry-run
```

确认后再去掉 `--dry-run` 写入。该步骤不是启动系统的必要条件。

## 10. 重置为空数据库

先停止 cron 和 Web，避免运行中的进程继续写数据库：

```bash
kill "$(cat run/web.pid)" 2>/dev/null || true
crontab -e   # 注释或删除本项目的定时任务
```

先做一致性备份：

```bash
stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups
sqlite3 data/quotes.db ".backup 'backups/quotes-$stamp.db'"
sqlite3 data/us_quotes.db ".backup 'backups/us-quotes-$stamp.db'"
```

再移走数据库并重新初始化：

```bash
mv data/quotes.db "data/quotes.db.before-reset-$stamp"
mv data/us_quotes.db "data/us_quotes.db.before-reset-$stamp"
rm -f data/*.db-wal data/*.db-shm data/*.lock
./scripts/init_fresh.sh
```

不要用 `cp` 备份正在写入的 SQLite 数据库；优先使用 `sqlite3 .backup`。

## 11. 定时任务

以下是参考模板。把 `PROJECT_DIR` 改成项目真实绝对路径，并确保 `python3` 能加载虚拟环境依赖。最简单的做法是使用项目自带的 `scripts/run_with_env.sh`，它会读取 `.env` 并设置工作目录。

```cron
SHELL=/bin/bash
PROJECT_DIR=/path/to/mny

# A 股已选股票高频行情：每分钟启动，内部每 10 秒刷新
* * * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh ./scripts/refresh_selected_quotes.sh >> $PROJECT_DIR/logs/selected_quotes.log 2>&1

# A 股全市场扫描：每 5 分钟重建动态池
*/5 9-11,13-14 * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh ./scripts/collect_cn_full_market.sh >> $PROJECT_DIR/logs/collector.log 2>&1

# A 股主账户交易
*/5 9-11,13-14 * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/trader.py --market CN --live >> $PROJECT_DIR/logs/trader.log 2>&1

# A 股小额账户交易，错开 2 分钟
2-59/5 9-11,13-14 * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/trader.py --market CN --account small-3000 --live >> $PROJECT_DIR/logs/small_trader.log 2>&1

# 午间收盘后补采一次
30 11 * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/collector.py --market CN >> $PROJECT_DIR/logs/collector.log 2>&1

# A 股复盘
10 15 * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/review.py --market CN >> $PROJECT_DIR/logs/review.log 2>&1
12 15 * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/review.py --market CN --account small-3000 >> $PROJECT_DIR/logs/small_review.log 2>&1

# 美股盘中，脚本内部按美东时段过滤
*/5 21-23 * * 1-5 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/trader.py --market US --live >> $PROJECT_DIR/logs/us_trader.log 2>&1
*/5 0-5 * * 2-6 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/trader.py --market US --live >> $PROJECT_DIR/logs/us_trader.log 2>&1

# 美股复盘：北京时间周二至周六早上
10 6 * * 2-6 cd $PROJECT_DIR && ./scripts/run_with_env.sh python3 src/review.py --market US >> $PROJECT_DIR/logs/us_review.log 2>&1
```

注意：

- cron 的时区应设为 `Asia/Shanghai`，或者确认服务器时区就是北京时间。
- 不要在同一市场同时启动多个 `--live` 写入进程。
- `.env` 权限应为 `600`，不要通过 `DEEPSEEK_API_KEY=...` 直接写进 crontab。
- 先用 dry-run 和手动 `--live` 跑通，再上定时任务。

## 12. 运行测试

```bash
python3 -B -m unittest discover -s tests -v
```

测试使用临时 SQLite，不会修改 `data/`。如果测试卡在“没有可用临时目录”，检查 `/tmp` 是否存在且可写。

## 13. API

只读看板 API：

```text
GET /api/dashboard
GET /accounts/small-3000/api/dashboard
GET /us/api/dashboard
GET /api/chat/history
GET /accounts/small-3000/api/chat/history
GET /us/api/chat/history
```

问答 API：

```text
POST /api/chat
POST /accounts/small-3000/api/chat
POST /us/api/chat
```

接口返回该账户的看板、持仓、成交、参数、策略和聊天；A 股两个账户共享行情但业务数据按账户隔离，美股使用独立数据库。

## 14. 常见问题

### 看板打开后没有行情

先执行 `./scripts/init_fresh.sh`，再执行 `python3 src/collector.py --market CN` 和美股采集。看板不会凭空生成行情。

### 提示未找到 DEEPSEEK_API_KEY

确认 `.env` 存在、权限为 `600`，并且通过 `./scripts/run_with_env.sh` 运行；或者在当前 shell 手动执行 `set -a; source .env; set +a`。

### 提示数据库锁

检查是否同时运行了多个交易、复盘或采集进程。等待当前写入结束后再重试，不要删除正在使用的 `-wal` 或 `-shm` 文件。

### 交易因为非交易时段被跳过

这是正常的保护行为。正式流程不要加 `--allow-closed-market`；它只用于测试。

### 行情或模型失败

查看 `logs/`、`debug/prompt_debug.txt` 或 `debug/us_prompt_debug.txt`。行情失败时系统会跳过本轮，不使用旧价格强行成交。

## 15. 开源发布前检查

```bash
git status --ignored
```

确认以下内容没有进入提交：

- `.env`
- `data/*.db`
- `logs/`
- `debug/`
- `backups/`
- `run/`
- 任何真实 API Key、密码或个人账户信息

仓库默认没有 `LICENSE`。正式开源前请选择并补充许可证，例如 MIT、Apache-2.0 或其他符合你意图的许可证。

# 加密货币趋势山寨扫描机器人

这是一个基于 Binance USDT 永续合约市场的全市场趋势扫描机器人，重点寻找趋势上涨、成交额放大、持仓量上升、短线突破的山寨币机会，并通过 Telegram 推送高评分结果。

## 功能

- **全市场扫描**：自动扫描 Binance USDT 永续合约。
- **趋势追涨评分**：结合多周期趋势、涨跌幅、成交量、持仓量、资金费率、突破情况和风险回报比评分。
- **信号标签过滤**：输出趋势、放量、OI、突破、费率、风控和过度偏离等标签，信号不足不会推送。
- **网络容错**：使用 Binance USDT-M Futures 专用客户端，接口失败会重试并跳过单项，不直接崩溃。
- **摘要推送**：支持将通过增强过滤的高评分机会合并成 Telegram 摘要。
- **支撑压力计算**：输出支撑位、压力位、突破位、进场区、止损和 TP1/TP2。
- **低分过滤**：低于 `SIGNAL_MIN_PUSH_SCORE` 或有效信号不足的币种不会推送。
- **Telegram 推送**：支持 5 分钟一轮推送，S/A/B 评级按不同重复次数提醒。
- **自动下单预留**：已预留 Binance 下单接口，但默认 `TRADING_ENABLED=false` 且 `DRY_RUN=true`。
- **本地复盘**：扫描结果保存到 `data/snapshots`，推送状态保存到 `data/state`。

## 安装

在项目根目录运行：

```powershell
pip install -r requirements.txt
```

## 配置

复制示例配置：

```powershell
copy .env.example .env
```

然后编辑 `.env`：

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=你的 Telegram Bot Token
TELEGRAM_CHAT_ID=
TELEGRAM_AUTO_RESOLVE_CHAT_ID=true

TRADING_ENABLED=false
DRY_RUN=true
BINANCE_API_KEY=
BINANCE_API_SECRET=
```

如果 `TELEGRAM_CHAT_ID` 留空，需要先给你的机器人发送任意一条消息，程序会通过 `getUpdates` 自动识别并缓存 chat id。

## 运行

推荐在项目根目录运行：

```powershell
python -m crypto_monitor.main
```

只扫描一轮后退出：

```powershell
python -m crypto_monitor.main --once
```

只测试 Telegram 推送：

```powershell
python -m crypto_monitor.main --test-telegram
```

等待你给机器人发送 `/start` 并自动缓存 chat id：

```powershell
python -m crypto_monitor.main --wait-telegram-chat
```

兼容旧入口：

```powershell
python crypto_monitor/crypto_websocket_monitor.py
```

## 评分逻辑

- **趋势分**：EMA20/EMA50 排列、多周期斜率、RSI 是否过热。
- **动量分**：5m、15m、1h 与 24h 涨幅。
- **成交量分**：当前短线成交量相对历史均值放大倍数和 24h 成交额。
- **持仓量分**：Open Interest 是否同步上升。
- **突破分**：是否突破近期高点或接近关键压力位。
- **资金费率分**：资金费率不过热时更适合追涨。
- **风控分**：支撑压力推导的风险回报比和止损距离。

评级默认含义：

- **S 级**：趋势、量能、OI、突破和风控同时较强。
- **A 级**：适合重点观察或小仓追涨。
- **B 级**：有异动但信号不完整。
- **C/D 级**：默认不推送。

## 关键配置

```env
SCAN_INTERVAL_SECONDS=300
MAX_SYMBOLS_PER_SCAN=80
TOP_RESULTS=10
MIN_QUOTE_VOLUME_USDT=20000000
MIN_PUSH_SCORE=70
S_GRADE_SCORE=88
A_GRADE_SCORE=78
B_GRADE_SCORE=70
MIN_VOLUME_SPIKE=1.8
MIN_OI_CHANGE_PCT=3
MAX_FUNDING_RATE_FOR_CHASE=0.0012
MAX_CHASE_EXTENSION_PCT=6
SIGNAL_MIN_PUSH_SCORE=72
SIGNAL_MIN_SIGNAL_TAGS=3
SIGNAL_REQUIRE_VOLUME_SPIKE=true
SIGNAL_MIN_VOLUME_SPIKE=1.6
SIGNAL_MIN_OI_CHANGE_PCT=2
SIGNAL_MAX_EXTENSION_PCT=7.5
SIGNAL_MIN_RISK_REWARD=1.45
SIGNAL_DIGEST_ENABLED=true
```

如果推送太少，可以降低 `SIGNAL_MIN_PUSH_SCORE`、`SIGNAL_MIN_SIGNAL_TAGS` 或 `SIGNAL_MIN_VOLUME_SPIKE`；如果推送太多，可以提高这些阈值或提高 `MIN_QUOTE_VOLUME_USDT`。

## 信号标签

- **TREND_ALIGNED**：趋势结构较强。
- **VOLUME_SPIKE**：短线成交量放大。
- **OI_EXPANSION**：持仓量同步上升。
- **BREAKOUT_ATTEMPT**：尝试突破近期高点或压力。
- **FUNDING_OK**：资金费率未明显过热。
- **RISK_REWARD_OK**：风险回报比满足要求。
- **CHASE_READY**：风控允许小仓追涨。
- **OVEREXTENDED**：价格偏离 EMA20 过远，默认降低评分或过滤。

## 自动下单安全

默认不会真实下单：

```env
TRADING_ENABLED=false
DRY_RUN=true
```

只有你明确配置 Binance API Key，并把 `TRADING_ENABLED=true`、`DRY_RUN=false` 后，才会尝试真实下单。建议先长时间 dry-run，确认评分和风控符合预期后再考虑实盘。

## Telegram 注意事项

- 不要把 Bot Token 提交到 Git。
- `.env` 已加入 `.gitignore`。
- 如果自动识别 chat id 失败，先给机器人发一条消息再启动程序。

## 风险提示

本项目只提供行情扫描和交易计划参考，不保证盈利。追涨山寨币风险很高，可能出现插针、滑点、资金费率升高、交易所限速、网络异常等情况。真实交易前请先使用 `DRY_RUN=true` 观察足够长时间。

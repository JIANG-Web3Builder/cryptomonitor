# ASR BTC v17 中文监控预警程序

本目录是独立可打包版本，来源参考：

- `asr_btc_channel_research_v1/output_channel_v4/v17`
- v17 使用 `config_channel_v4.py`、`indicators_channel_v4.py`、`engine_channel_v4.py` 的同源逻辑

程序用途：读取 BTCUSDT 15m K线 CSV，按 v17 策略重新计算最新动作；当出现新的入场、加仓、平仓、减仓、反转动作时，发送中文消息到你的预警系统。

## 目录文件

- `monitor_v17_alert_v1.py`：监控主程序
- `config_alert_v1.json`：预警配置、行情路径、风险提示、webhook 配置
- `engine_v17_alert_v1.py`：v17 策略执行核心副本
- `config_channel_v4.py`：v17 参数和版本特性
- `indicators_channel_v4.py`：指标计算
- `data_loader_channel_v4.py`：行情字段定义
- `strategy_versions_channel_v4.py`：版本特性读取
- `requirements_v1.txt`：Python 依赖
- `state_v17_alert_v1.json`：运行后自动生成，记录已发送动作
- `alerts_sent_v17_alert_v1.csv`：运行后自动生成，记录发送日志

## 安装依赖

```powershell
pip install -r requirements_v1.txt
```

## 配置行情文件

打开 `config_alert_v1.json`，修改：

```json
"data_csv_path": "D:/workspace/20260325/data/BTCUSDT_15m.csv"
```

CSV 必须包含字段：

- `open_time`
- `open`
- `high`
- `low`
- `close`
- `volume`

## 配置预警系统

默认是 dry-run，只打印中文消息，不真实发送：

```json
"alert": {
  "enabled": false,
  "dry_run": true,
  "webhook_url": ""
}
```

如果你的预警系统支持 HTTP webhook，把它改成：

```json
"alert": {
  "enabled": true,
  "dry_run": false,
  "webhook_url": "https://你的预警系统地址",
  "method": "POST",
  "headers": {
    "Content-Type": "application/json"
  },
  "payload_mode": "json_text",
  "text_field": "text"
}
```

发送格式默认为：

```json
{
  "text": "中文预警消息正文",
  "event": {"策略动作结构化字段": "..."}
}
```

如果你的系统只接收纯文本，可改为：

```json
"payload_mode": "raw_text"
```

## 运行方式

只扫描一次：

```powershell
python monitor_v17_alert_v1.py --once
```

持续轮询：

```powershell
python monitor_v17_alert_v1.py --loop
```

轮询间隔由 `config_alert_v1.json` 的 `poll_seconds` 控制。

## 首次运行行为

默认 `send_start_mode` 是 `skip_history_on_first_run`，程序第一次运行只建立历史基线，不发送历史动作；之后只对新增动作提醒。程序会根据 `state_v17_alert_v1.json` 去重。

如果你希望第一次运行就测试历史消息，可以临时改成 `new_only` 并保持 dry-run；测试后建议删除 `state_v17_alert_v1.json`，再改回 `skip_history_on_first_run` 建立正式基线。

推荐流程：

1. 保持 `dry_run=true`。
2. 运行 `python monitor_v17_alert_v1.py --once`。
3. 首次运行会生成 `state_v17_alert_v1.json`，把历史动作标记为已见。
4. 再开启 webhook 真实发送。
5. 后续有新 K 线产生新策略动作时才会提醒。

## 中文消息包含内容

入场/加仓消息包含：

- 策略版本、品种、周期、时间
- 动作类型、方向、信号层级
- 价格、RSI、通道上中下轨
- 按配置权益估算的仓位、名义金额、BTC 数量
- 参考止损、第一目标位
- 杠杆与执行提醒

出场/减仓消息包含：

- 出场原因中文解释
- 原始入场时间和入场价
- 持仓 K 线数
- 本笔估算 PnL
- 最大浮盈/最大浮亏
- 风险处理提醒

## 注意事项

- 本程序只发送提醒，不自动下单。
- 预警来自 v17 回测逻辑复算，实盘前要确认数据源延迟、K线收盘时间、交易所手续费和滑点。
- `only_closed_latest_bar=true` 时，会尽量忽略尚未收完的最新 K 线，降低盘中假信号。
- 打包带走时，把整个 `asr_btc_v17_alert_monitor_v1` 目录复制即可；目标机器需要安装 Python、pandas、numpy，并能访问你的 15m 行情 CSV 或数据更新流程。

# entropy-arb

**[English documentation / 英文文档 → README.md](README.md)**

**[V2 中文详细架构、配置、风控与实盘说明 → V2_ARCHITECTURE.zh-CN.md](V2_ARCHITECTURE.zh-CN.md)**

开源双交易所永续合约套利机器人。其中一条腿永远是 **Entropy**（Hyperliquid 上的
`io` builder dex）；另一条腿（对冲腿）三选一：

| `--hedge` | 交易所 | 计价货币 | 吃单费 | 协议 |
|---|---|---|---|---|
| `lighter` | Lighter 主网 | USDC | 0 bps | zkLighter ws（增量订单簿，异步结算） |
| `lighter-rh` | Lighter Robinhood 链 | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book，IOC 同步结算 |

> **推荐链接** —— 通过以下链接注册即可支持本项目：
> - Entropy — Tier 4 推荐，100% 返佣：<https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood 链：<https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz（Hyperliquid）：<https://app.hyperliquid.xyz/join/QUANTGUY>

当同一品种在一边贵、另一边便宜时，机器人同时在贵的一边卖出、便宜的一边买入
（均为吃单），持有 delta 中性仓位，等溢价回归后反向平仓。所有交易决策使用的
价格都来自**将要实际成交的那个交易所的真实订单簿**——Hyperliquid 的盘口来自
官方 websocket（`wss://api.hyperliquid.xyz/ws`），Lighter 的盘口来自 Lighter
官方 websocket。

机器人运行期间（即使没有密钥、没有开策略）会自动把两边盘口记录成**分钟级
CSV 数据**，配套分析工具提供 Static 或 Dynamic 信号配置所需的价差分布。

## 信号逻辑

系统支持两种兼容信号。**Static** 使用下面的固定 bps 区间；**Dynamic** 使用
Slow Midline 和后文说明的 Z-score OPEN/ADD/EXIT：

```
price_basis: usd（推荐）
premium_bps =（Entropy 价格 × Entropy 计价币/USD
               /（对冲腿价格 × 对冲腿计价币/USD）− 1）× 10 000

price_basis: raw（旧配置兼容）
premium_bps =（Entropy 价格 / 对冲腿价格 − 1）× 10 000

                          ┌──────────────  卖出 Entropy + 买入对冲腿
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   溢价的长期中枢
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  买入 Entropy + 卖出对冲腿
```

- `price_basis` —— `usd` 让采集、Midline、Z-score、Regime、VWAP 和门槛使用
  同一 USD 口径；旧配置省略时继续使用 `raw`。
- `midline_bps` —— Static 溢价中枢，也是 Dynamic 预热期的显示种子。跨所溢价
  几乎从不以零为中心（预言机不同、
  计价货币不同、新上市溢价等），零中心的带只会朝一个方向开仓、打满仓位上限、
  永远无法平仓。请实际测量溢价所在的位置，然后填入。
- `upper_bps` / `lower_bps` —— 仅供 Static 模式使用；Dynamic 会忽略它们。

Static 模式下，两个方向的门槛都作用于**可实际成交的价格**（Entropy 买一 对 对冲腿卖一，
反之亦然），并且是**扣除双边吃单手续费之后的净门槛**——引擎会在阈值之上
另行叠加手续费。但带宽只是价格比率空间中的信号距离，**不构成往返美元利润
下限保证**。反例：`midline=5, upper=4, lower=3`，先以
`Entropy/Hedge=100.091/100` 卖出一单位 Entropy，再在共同价格上涨十倍后以
`1000/999.801` 反向退出；两个方向都通过门槛，但未计手续费已亏约 `$0.108`。
滑点、Funding 和 quote/USD 变化还会进一步改变结果。

Static 模式有一点必须理解：当 `midline_bps: 5` 时，所选口径的下边界是
`midline − lower`；实际 BUY-Entropy 方向会换算成精确的 Hedge/Entropy 倒数，
而不是简单使用 `lower − midline`，换算后的门槛仍可能为**负数**。这是有意为之——如果 Entropy 长期贵 5 bps，
那么在溢价为 0 时买入它，相对其自身均衡水平就是便宜了 5 bps，这笔交易正是
此前在 `midline + upper` 处卖出的获利平仓。这同时意味着**中枢填错就是亏钱
策略**：若真实溢价中枢是 0 而你填了 5，机器人会整天以公允价买入 Entropy。
先测量、再交易——数据采集器和分析工具就是为此而生。

## 快速开始

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 数据采集只需要这些

cp config.example.yaml config.yaml       # 策略配置（阈值、规模、风控）
cp .env.example .env                     # 密钥——交易必填
```

仅开发和运行单元测试时，可安装不含实盘签名 SDK 的开发依赖：

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

交易哪个市场**不在**配置文件中——每次启动时用命令行参数显式指定：
`--symbol`（两个交易所共同交易的品种）和 `--hedge`（三选一：
`lighter`、`lighter-rh`、`tradexyz`；Entropy 永远是
另一条腿）。

本机器人**没有模拟盘**——要么采集数据（`--record-only`），要么实盘交易。
请用采集的数据和最小的仓位上限来验证策略，而不是模拟成交。

**第一步：先采集数据**（不需要任何密钥）：

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

至少运行几个小时（最好一整天——溢价存在日内规律），数据写入
`logs/minutes.csv`。

**第二步：分析数据、设定阈值：**

```bash
python3 tools/analyze.py
```

它会输出溢价分布、各档带宽的历史触发频率，以及可直接粘贴进
`config.yaml` 的 `thresholds:` 配置块。默认分析 USD 换算后的样本并输出
`price_basis: usd`；`--basis raw` 只用于兼容旧配置和检查原始数据。

**第三步：实盘** —— 填写 `.env`，安装签名 SDK，仓位上限从刚好满足
交易所最小名义的水平开始：

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

不带 `--record-only` 运行时，只要两边行情就绪且溢价越过带宽，就会立即
发送真实订单。

`requirements-live.txt` 只是开发安装入口，不是可复现实盘锁文件：目前包含只有下限
的依赖，以及指向持续变化的 Lighter SDK GitHub `main`。实盘部署前必须把本地审计过
的包版本、commit SHA 和制品哈希全部固定。

**仪表盘。** 在终端运行时会显示实时 Rich 仪表盘：两边盘口（含数据龄/点差）、
持仓与上限、账户权益与本次会话盈亏、两个方向的可成交溢价对比完整门槛
（已含手续费与库存加价，● 表示已武装）、数据采集进度、最近成交，以及日志
尾部。延迟面板滚动显示行情到信号、信号到发送、发送到响应、响应到成交、
双腿成交时间差的 P50/P95/P99（完整日志写入 `logging.file`，默认
`logs/engine.log`）。`--record-only`
模式同样可用。加 `--cn` 参数可使仪表盘全部以中文显示。`--no-dashboard`
可切换为纯日志输出（nohup/systemd 等非终端环境会自动退回纯日志），也可
设置 `logging.dashboard: false`。

## 数据采集与分析

采集器在所有模式下自动运行（`recorder.enabled: true`）：每秒采样一次两边
的真实盘口，每分钟写一行：

| 列 | 含义 |
|---|---|
| `minute_ts`, `time_utc` | 分钟起点（epoch 秒 / ISO UTC） |
| `entropy_bid/ask`, `hedge_bid/ask` | 该分钟最后一次有效盘口 |
| `premium_open/high/low/close/mean/std_bps` | Entropy 相对对冲腿的中间价溢价 |
| `sell_edge_mean/max_bps` | 卖出 Entropy 方向的可成交溢价（Entropy 买一 / 对冲腿卖一 − 1） |
| `buy_edge_mean/max_bps` | 买入 Entropy 方向的可成交溢价（对冲腿买一 / Entropy 卖一 − 1） |
| `samples` | 该分钟约 60 秒中两边盘口同时有效的秒数 |
| `entropy_quote_asset`, `hedge_quote_asset` | 本行两腿计价资产身份 |
| `entropy_quote_usd_close`, `hedge_quote_usd_close` | Kraken 最新有效 quote/USD 中间价 |
| `hedge_entropy_quote_basis_close_bps` | 对冲计价币/Entropy 计价币基差；Lighter RH 即 USDG/USDC |
| `premium_usd_*_bps` | 两腿换算 USD 后的中间价溢价 |
| `sell_edge_usd_*_bps`, `buy_edge_usd_*_bps` | USD 口径的两方向可执行 edge |
| `*_usd_min/p50/p95/p99_bps` | 该分钟每秒样本的分布，避免只看一次尖峰 |
| `*_usd_ge_10_samples` | 该分钟达到 10 bps 的样本总数 |
| `*_usd_longest_ge_10_samples/span_seconds` | 达到 10 bps 的最长连续次数和首尾持续时间；相邻样本间隔超过 2.5 秒会断开 |
| `*_usd_max_time_utc`、`*_usd_max_*` | 最高值的精确 UTC 时间及当时两腿、汇率盘口 |
| `book_update_skew_ms_p95/max` | 两个平台盘口更新时间差，帮助排除不同步造成的假价差 |
| `fx_samples` | 同时具有新鲜 quote/USD 的盘口样本数 |

FX 缺失时仍保留 raw 字段，但 USD 字段留空且 `fx_samples=0`，绝不伪造 1:1。
旧表头 CSV 会轮换为 `.old`，因为缺失的历史 FX 无法安全重建。采集的 edge 为
费前口径；分析工具默认使用 USD 字段，并在统计触发频率前扣除 `--fees-bps`
（请传入**两边吃单费之和**——零费交易所默认 0.0，对冲腿为 `tradexyz` 时
约为 1.0），因此其表格与建议值可直接填入配置。`--hours 24`
可只分析最近数据；溢价中枢会漂移，请定期重新分析并更新 `config.yaml`。
10 bps 是机会观察线，不等于利润保证；初步候选至少应同时满足
`longest_ge_10_samples >= 4`、`span_seconds >= 3`，再结合可成交深度和延迟判断。
可直接运行：`python3 tools/opportunities.py --csv logs/minutes.csv --hours 24`。

## 配置说明

策略在 `config.yaml`（严格校验——未知键名直接报错），密钥在 `.env`。
交易市场由命令行指定（`--symbol`、`--hedge`）。完整的双语注释参考：
[config.example.yaml](config.example.yaml)。核心项：

| 键 | 含义 | 默认值 |
|---|---|---|
| `thresholds.price_basis` | `usd` 使用统一美元信号；`raw` 保持旧语义 | 默认 `raw`，示例为 `usd` |
| `thresholds.midline_bps` | 静态溢价中枢（必须实测！） | — |
| `thresholds.upper_bps` / `lower_bps` | 入场带宽（> 0） | — |
| `midline.mode` | `static` 或实时 `dynamic` 慢速中位数中枢 | `dynamic`（示例配置） |
| `midline.fast_window_seconds` / `slow_window_seconds` | Fast EMA / Slow Rolling Median 窗口 | 300 / 1800 |
| `midline.min_samples` | dynamic 交易前需要的 1Hz 新鲜样本数 | 300 |
| `midline.volatility_method` | 滚动 `std` 或稳健缩放 `mad` | `std` |
| `midline.entry_z_score` / `exit_z_score` | Dynamic 开仓/加仓与退出阈值 | 2.5 / 0.5 |
| `regime.enabled` | 确认状态断裂后暂停新增交易 | `true`（示例配置） |
| `entropy.dex` | Entropy 在 Hyperliquid 上的 dex 名 | `io` |
| `*.taker_fee_bps` | 各所吃单费 | 0.0（tradexyz 对冲腿：1.0） |
| `*.max_position_usd` | 各所持仓上限 | 示例为 500 |
| `*.max_orders_per_min` | 各所每分钟下单预算（滑动 60 秒） | 120；Lighter 对冲腿 30 |
| `sizing.take_fraction` | 吃掉可套利深度的比例 | 0.5 |
| `sizing.max_order_notional_usd` | 旧模式单笔名义上限 | 示例为 100 |
| `sizing.vwap_enabled` | 当前盘口 VWAP 自动仓位；关闭则保留旧逻辑 | `true`（示例配置） |
| `sizing.min_order_usd` / `max_order_usd` | 自动仓位搜索范围 | 示例为 10 / 100 |
| `sizing.minimum_net_edge_bps` | 扣除建模成本后的最小方向偏离 | 6 |
| `sizing.max_vwap_slippage_bps` / `max_book_impact_bps` | 仓位可接受的最大盘口滑点/冲击 | 5 / 5 |
| `sizing.safety_buffer_bps` / `expected_latency_cost_bps` | 手续费和可见深度之外的显式扣减 | 2 / 0 |
| `inventory.scale_bps` / `floor_frac` | 库存阶梯（仓位超过上限的 `floor_frac` 后额外加价） | 10 / 0.5 |
| `execution.premium_persist_sec` | 信号需持续多久才触发 | 3.0 |
| `execution.risk_recovery_enabled` / `hedge_timeout_ms` | 单腿超时恢复 | 示例为 `true` / 250ms |
| `execution.max_unhedged_delta_usd` | 触发紧急对冲的净敞口阈值 | 示例为 100 |
| `kill_switch.enabled` | 统一风险事件与持久暂停入口 | `true`（示例配置） |
| `kill_switch.emergency_flatten_enabled` | 严重持续风险后允许 reduce-only 紧急平仓 | `false` |
| `accounting.enabled` | 持久化 Pair 台账与重启快照 | `true`（示例配置） |
| `funding.enabled` / `expected_holding_hours` | 双所实时资金费成本 | 示例为 `true` / 1 小时 |
| `stablecoin.enabled` | 各交易所计价资产换算 USD 并监控脱锚 | `true`（示例配置） |
| `stablecoin.provider` / `source_url` | 公共一档盘口来源 | `kraken` / `https://api.kraken.com` |
| `stablecoin.max_spread_bps` | 换算盘口允许的最大买卖价差 | `10` |
| `execution.*` | 滑点保护、超时、对账周期等 | 见配置文件 |
| `market_data.enforce_book_age` | 是否启用毫秒级盘口年龄拒单 | `true`（示例配置） |
| `market_data.max_book_age_ms` | 任一盘口超过该年龄则禁止新增交易 | `300` |
| `session.enabled` | `false`：虚拟货币使用一个 24/7 统计池；`true`：股票永续按时段隔离统计 | `false` |
| `recorder.*` | 分钟数据采集器 | 开启，`logs/minutes.csv` |
| `logging.dashboard` / `logging.file` | 终端仪表盘；开启时日志写入文件 | 开启，`logs/engine.log` |

### 行情质量与延迟

程序分别维护两类新鲜度：`execution.staleness_sec` 判断 WebSocket 最近是否仍有
消息，`market_data.max_book_age_ms` 判断订单簿本身最近是否真正更新。心跳仍在但
盘口过期时，启用 `enforce_book_age` 后也会禁止新增交易并清除已武装信号。

Hyperliquid `l2Book` 的官方 `time`（毫秒）还会按 `max_book_age_ms` 校验；交易所
时间已过期的快照和明显来自未来的异常时间戳会被拒绝。Lighter
官方订单簿 WebSocket 当前未提供服务器时间字段，因此只记录本地接收时间，
交易所时间保持未知，不用本地时间伪造。成交相关时间同样是程序本地“观察到响应/
成交”的时间，不等同于交易所撮合引擎内部时间。

延迟分位数使用最近 2,000 个内存样本，进程重启后清空。示例的 300ms 是保守安全
值；正式启用前应先观察实际更新频率和 P95/P99，再按品种与部署位置调整。

### 虚拟货币与股票交易时段

时段感知只有一个手动开关，不会根据 symbol 自动猜测：

```yaml
session:
  enabled: false  # 虚拟货币 24/7
```

虚拟货币保持 `false`；股票永续改为 `true`。开启后按美国东部时间使用四个统计
时段：夜盘（20:00-04:00）、盘前、正常时段和盘后，并处理周末、常规美股节假日
以及重复出现的 13:00 提前收盘日。

**所有时段都允许股票永续 OPEN/ADD/EXIT。** 实际能否新增仓位仍必须通过该时段
自己的 Dynamic Midline 预热、Z-score、Regime、Executable Edge、盘口新鲜度、
资金费、稳定币 basis 和 Kill Switch 检查。现货美股休市不等于股票永续休市；
周末和节假日统一进入夜盘统计池，只要合约行情和风控检查正常就仍可交易。

夜盘、盘前、正常时段和盘后分别维护 Dynamic Midline、波动率、Z-score 和 Regime，
互不污染。如果已有 Pair，而新时段的独立统计仍在预热，EXIT 可以保守使用最近一个
已就绪中枢来降低风险；新的 OPEN/ADD 则等待本时段完成预热。这是统计模型尚未就绪，
不是夜盘禁交易。20:00 直接进入夜盘是股票永续的统计设计，目的是消除现货市场
20:00-21:00 的空档，不代表修改了现货交易所时段。内置日历惯例参照
[NYSE 官方交易日历](https://www.nyse.com/trade/hours-calendars)。临时全国休市、
交易所特有的预言机规则变化无法靠静态日历提前预测，仍由盘口过期和交易所故障
保护执行 fail-closed。

### 当前盘口 VWAP 与自动仓位

这里的 VWAP **不是 K 线或 TradingView VWAP**。程序针对每个候选基础资产数量，
分别逐档吃掉买入腿的 asks 和卖出腿的 bids，计算两腿真实盘口加权成交价，并计算：

```
预期净利润
  = 卖出腿 VWAP 名义金额 - 买入腿 VWAP 名义金额
  - 两边吃单费
  - safety buffer - 配置的预期延迟成本
```

盘口可见滑点已经体现在两腿 VWAP 名义金额中，不会再重复扣减。程序通过二分搜索
寻找满足最小/最大订单、净 edge、VWAP 滑点、盘口冲击、交易所最小数量和持仓余量
的最大共同基础资产数量。盘口不足或任一约束不通过时不会下单。

Static 模式中，`minimum_net_edge_bps` 表示扣除建模成本后，相对配置中枢的最小
方向偏离，并与旧版 upper/lower 区间共同生效。Dynamic 模式由 Z-score 决定
OPEN/ADD/EXIT，并直接给出当前盘口 VWAP 的可执行门槛，完全忽略旧区间。启用后，
系统会在检查 minimum net edge 前计入两边新鲜的资金费率和计价资产 USD 价格；
已启用但数据缺失或过期时，OPEN/ADD 会 fail-closed。

### Dynamic Midline、Z-score 与 Regime Detection

当 `midline.mode: dynamic` 时，引擎最多每秒采样一次新鲜的 mid-to-mid 溢价，维护：

```
Fast Midline = 基于时间的 5 分钟 EMA
Slow Midline = 30 分钟 Rolling Median（实际交易中枢）
deviation    = 当前 spread - Slow Midline
Z-score      = deviation / max(滚动波动率, volatility floor)
```

波动率支持总体标准差，或经过 `1.4826` 正态一致性缩放的 MAD。滚动窗口内达到
`min_samples` 前，动态模式显示 `WARMUP` 并**禁止新增交易**，不会静默使用静态值
下单。统计状态只保存在内存，重启后需要重新预热。

Fast EMA 永远不会单独成为交易基准。启用 `regime.enabled` 后，系统同时监控
Fast/Slow 分叉、绝对 spread 和绝对 Z-score。异常连续达到
`break_persist_seconds` 后进入 `PAUSE_NEW_ENTRY`；恢复则必须连续健康达到
`recovery_persist_seconds`。暂停会解除两个策略方向的武装，但不会自动平仓，也
不会禁止紧急 Delta 对冲。

Dynamic 模式现在真正使用 Z-score 作为主信号：

```
空仓：                 Z >= +entry_z  -> OPEN 卖 Entropy Pair
空仓：                 Z <= -entry_z  -> OPEN 买 Entropy Pair
已有同方向 Pair：      再次超过 entry_z -> ADD
已有卖 Entropy Pair：  Z <= +exit_z   -> 买 Entropy 退出
已有买 Entropy Pair：  Z >= -exit_z   -> 卖 Entropy 退出
```

EXIT 数量会被硬限制为当前 Pair 剩余的匹配基础数量，因此回归信号不能反向开仓。
当前盘口 VWAP 和建模成本检查仍然必须通过，所以在买卖价差或手续费使该快照不可执行
时，退出可能会略晚于准确的 Z 边界。Dynamic 模式下 `upper_bps` / `lower_bps`
不再驱动信号，只保留给 Static 模式兼容使用。

运行时维护 PairPosition（ID、方向、剩余基础数量），启动时也会根据两边匹配持仓
保守恢复。第一次严格持仓对账发生在策略任务创建前；如果发现单腿或不平衡持仓，
立即持久暂停 OPEN/ADD 并安排 reduce-only 风险恢复，不会先启动策略再等待风险循环。
每次新执行还会创建带事件记录的状态机。启用 `accounting.enabled` 后，
未完成 Pair、最近 200 个已完成 Pair、执行事件、风险事件、持久暂停和待完成紧急
平仓都会从原子替换的快照恢复；审计事件追加写入 JSONL 并落盘。
实盘模式强制要求启用该台账；若发现未完成的 `.tmp` 快照，重启会 fail-closed，
要求人工检查。

### Pair PnL、Funding 与计价资产 Basis

同一个 Pair 统一聚合 OPEN、ADD、EXIT 和紧急平仓成交，记录 entry/exit spread、
Z-score、中枢、两腿进出场 VWAP、手续费、预计和交易所核对后的资金费、计价币基差
调整、进出场滑点、进出场市场时段、gross/net PnL、持仓时间、最大不利/有利价差。

资金费按双方当前费率和每条腿各自的 USD 名义额计算，不会把 USDC、USDG 原始数字
直接相加。Hyperliquid asset context 已是小时费率；Lighter 跨所
接口返回 8 小时等效费率，因此程序除以 8。开仓成本按 `expected_holding_hours`
估算；Pair 持有期间再用账户 funding history 替换为交易所实际支付数据。

可执行价差会先换算成统一 USD：

```text
调整后 gross edge
  = 卖出腿 VWAP × 卖出计价币/USD
  - 买入腿 VWAP × 买入计价币/USD
```

VWAP 最小/最大订单、两边仓位上限、净 Delta、对账差异、成交量、账户变化和会话
MTM PnL 使用同一套实时 quote→USD 汇率；各 venue 的 `cash` 仍保留原始计价币，
只在跨 venue 汇总时换算，避免把 USDG、USDC 数字直接相加。

示例配置从 Kraken 公共 REST API 读取 `USDC/USD` 和直接的 `USDG/USDC` 一档盘口，
其中 USDG 是 Lighter Robinhood 使用的 Paxos USDG。Bitget 的 USDGO 是 Anchorage
Digital/OSL 发行的另一种资产，禁止用它替代 USDG。只有全部所需盘口均为有限正数、
未交叉且买卖价差不超过 `max_spread_bps` 时才整批更新；REST 成功接收时间用于判断
快照新鲜度，档位挂单本身多久未变化不会误判为接口过期。否则保留旧时间戳并最终
因过期失败关闭。来源缺失或过期会禁止 OPEN/ADD；超过 `halt_deviation_bps` 也会暂停新增风险。必须正确设置每条腿的
`quote_asset`；默认 Entropy/Lighter 主网/trade.xyz 为 USDC，Lighter Robinhood
为 USDG。有效中枢门槛也会按当前方向（反向使用精确倒数）换算到同一 USD 比率，
避免 USDG/USDC 基差只移动可执行 edge 却不移动门槛。非 USD 计价资产在实盘模式
必须启用且取得新鲜的 stablecoin 换算数据，否则拒绝启动；换算过期后，账户变化和
会话 PnL 显示为未知，不会继续把旧的 USDG/USDC 汇率冒充实时 USD 估值。

USDG/USDC 报价基差直接使用该交易对的中间价；执行成本按方向使用真实盘口：
买入 USDG 使用 Ask，卖出 USDG 使用 Bid。分钟 CSV 还记录
`hedge_entropy_quote_bid_close`、`hedge_entropy_quote_ask_close` 和
`hedge_entropy_quote_spread_close_bps`：

```text
USDG/USDC basis bps = ((bid + ask) / 2 - 1) * 10,000
```

## 密钥配置（`.env`，仅实盘需要）

- **Entropy / tradexyz（Hyperliquid）** —— 在
  <https://app.hyperliquid.xyz/API> 创建 API（agent）钱包。`HL_PRIVATE_KEY`
  填 **agent 钱包私钥**，`HL_ACCOUNT_ADDRESS` 填主账户地址。当
  `--hedge tradexyz` 时两条腿默认共用该账户（内部自动共享 nonce 序列）；
  如需分开，设置 `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`。注意给
  所交易的各 dex 分别充入保证金。
- **Lighter** —— `LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、
  `LIGHTER_API_PRIVATE_KEY`，必须注册在与启动参数 `--hedge` **相同的部署**上
  （主网与 Robinhood 链是两套独立的账户和密钥——参见
  [lighter-python](https://github.com/elliottech/lighter-python)）。

## 执行机制

- 两条腿**同时发出吃单**：Lighter 用带均价保护的市价单，在鉴权 websocket
  上异步确认成交；Hyperliquid 用 IOC 限价单同步结算（结果未知时轮询
  orderStatus 兜底）。
- **未知结果立即 fail-closed**：任一订单结果 unresolved，会在释放两边执行锁前
  持久暂停 OPEN/ADD。暂停期间只允许持仓对账、EXIT 和 reduce-only 风险恢复；
  重启不能自动清除。
- **EXIT 强制 reduce-only**：两条退出腿都传 `reduce_only=true`。本地 Pair 数量
  即使过期，最坏是交易所拒单或只减少现有仓位，不允许反向开出新仓。
- **持续性闸门**（`premium_persist_sec`）：信号先"武装"，持续存在才触发，
  过滤单 tick 的假信号。
- **库存阶梯**：仓位超过上限的 `floor_frac` 后，同方向加仓需要线性递增的
  额外溢价，满仓时最高加 `scale_bps`。
- **净敞口对冲**：两腿成交不对等时立即用 reduce-only 单（带滑点保护）
  削减敞口，并每 `reconcile_sec` 与链上仓位对账。
- **单腿超时恢复**：启用 `risk_recovery_enabled` 后，两条订单任务都会持续追踪。
  到 `hedge_timeout_ms` 只有一条 OPEN/ADD 腿确认成交时，立即在该交易所发送
  reduce-only 反向单。EXIT 的另一腿结果未知时不会盲目撤销，而是等待结果后恢复
  净 Delta。
- **故障隔离**：被限频的交易所短暂暂停；交易所不可达时每
  `venue_probe_sec` 探测。启用 Kill Switch 后，连续执行失败会暂停新增仓位，
  不会继续累积风险。
- **实际成交均价审计**：Hyperliquid 超时/5xx 后按 exchange order ID 查询成交历史
  并计算实际 VWAP。若成交数量已知但仍取不到均价，Pair 会写入
  `accounting_complete=false`，同时持久暂停新增仓位。
- **仅实盘**：没有模拟成交模式。`--record-only` 是唯一无风险的运行方式，
  其余都是真金白银。

### Execution State Machine

```text
NEW -> SIGNAL_CONFIRMED -> ORDERS_SENT
                              |       \
                              |        -> BOTH_FILLED -> COMPLETE
                              v
                           PARTIAL -> RECOVERY -> HEDGED -> COMPLETE
                                         \
                                          -> UNWINDING -> COMPLETE / FAILED
```

所有状态变化都会记录时间戳、原因、Pair ID 和附加数据。进入 `RECOVERY` 后，恢复
Delta Neutral 的优先级高于套利利润。`UNWINDING` 已纳入状态契约，供完整 Pair
撤销流程使用；当前“撤销已知单腿”仍记录在 `RECOVERY` 内，因为未知的另一腿随后
仍可能成交。

### Kill Switch

风险事件分为 `PAUSE_NEW_ENTRY`、`EMERGENCY_HEDGE` 和
`EMERGENCY_FLATTEN`，当前覆盖：

- 净敞口一旦超过 `max_unhedged_delta_usd` 就立即禁止 OPEN/ADD 并请求紧急对冲；
  持续超过 `max_unhedged_duration_ms` 后转为持久暂停。任一腿无法估值时计时不会重置；
- 连续不等量成交或部分成交；
- 连续执行失败；
- 链上与本地持仓对账不一致；
- 配置启用后的会话 MTM 亏损上限；
- 可恢复的 WebSocket/盘口过期、交易所 API 故障和 regime break。

持久触发会禁止 OPEN/ADD，但保留降低风险的 EXIT 和对冲，单纯重启不会清除。
人工检查后可用 `--clear-risk-pause` 重启；只有实盘对账确认两边都已空仓且没有待完成
紧急平仓时才会清除。紧急平仓默认关闭。显式开启后，交易所断连、盘口缺失、锁占用或订单失败会
保留为持久待办，并每 `emergency_flatten_retry_sec` 重试；最大次数为 0 表示持续
重试直到已知持仓归零。外部交易所不可用时，任何客户端都无法保证一定成交。
关闭程序只会有界等待正在执行的订单；超时后会把仍在途的执行持久化为未知状态。
重启恢复到任何非终态执行记录时，都必须先对账，不能 OPEN/ADD。

在你独立确认两边都已空仓后，可以执行下面的人工复位。该命令会先做实盘持仓对账，
因此不能与 `--record-only` 一起使用：

```bash
python3 main.py --symbol SNDK --hedge lighter-rh --clear-risk-pause
```

## 目录结构

```
main.py                  入口（--record-only，默认即实盘）
entropy_arb/config.py    YAML + .env 配置契约与校验
entropy_arb/book.py      订单簿 + 含手续费的套利规模计算
entropy_arb/pricing.py   当前盘口 VWAP、可执行 edge、二分仓位
entropy_arb/midline.py   Fast/Slow 中枢、波动率、Z-score、regime 保护
entropy_arb/models.py    最小 Pair 持仓及执行/风险契约
entropy_arb/costs.py     资金费预测 + 计价币/USD basis 新鲜度保护
entropy_arb/ledger.py    持久化 Pair PnL 账本 + 重启快照
entropy_arb/metrics.py   滚动执行延迟分位数
entropy_arb/session.py   单开关虚拟货币/美股时段时钟
entropy_arb/feeds.py     官方 HL ws + zkLighter ws 行情
entropy_arb/venue_hl.py  Hyperliquid dex 适配器（Entropy、tradexyz）
entropy_arb/venue_lighter.py  zkLighter 适配器（主网、Robinhood 链）
entropy_arb/engine.py    双交易所策略主循环
entropy_arb/dashboard.py Rich 终端仪表盘
entropy_arb/recorder.py  分钟级盘口数据采集
tools/analyze.py         minutes.csv -> 阈值建议
tests/                   python3 -m pytest tests/
```

## 已知风险

- **中枢错误或样本被污染仍会亏钱。** Dynamic 模式能减少人工漂移，但无法识别
  所有预言机或计价资产变化；regime 阈值应保持保守，并持续检查采集数据。
- **计价币价格源风险**：独立 basis 模块会移除已知 quote/USD 变动，但外部现货源
  本身也可能过期、不可用，或其一档价格并不可按目标规模成交；启用后过期数据会
  禁止新增仓位。
- **资金费预测误差**：开仓时只是把当前费率外推到 `expected_holding_hours`，未来
  每小时费率仍可能变化；真实资金费会事后核入 Pair PnL，但无法提前完美预测。
- **薄盘口**：VWAP sizing 会拒绝超过可见滑点/冲击上限的仓位，但信号出现后盘口
  仍可能变化，部分成交后的恢复对冲仍会产生真实滑点。
- **交易时段模型风险**：股票模式只使用夜盘、盘前、正常和盘后四个统计池，不会因
  现货休市禁止股票永续交易。周末和节假日归入夜盘，但交易所专属预言机冻结或规则
  变化仍可能让该统计池失真；关闭时段开关的虚拟货币模式是连续的 24/7 统计池。
- **单腿/交易所故障风险**：程序会持久化恢复任务并持续重试，但无法强迫已宕机的
  交易所接受或成交订单，仍然需要人工运维监控。

风险自负。本软件直接操作真实资金，本文档不构成任何投资建议。请从最小的
仓位上限开始。

## 开源协议

[MIT](LICENSE)

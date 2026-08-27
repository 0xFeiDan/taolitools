# entropy-arb V2 中文详细说明

> 文档状态：与当前工作区代码一致（2026-08-27）  
> 当前范围：Entropy + 一个指定的 hedge venue  
> 明确不包含：多交易所自动选择、跨多个 hedge venue 分配资金、Expected PnL 排名

## 1. 文档目的

本文件说明 `entropy-arb` 从基础双交易所价差框架升级到 V2 后的实际行为，包括：

- 当前系统架构；
- Dynamic Midline、Z-score 和 Regime Detection；
- 当前订单簿 VWAP 与自动仓位计算；
- Executable Edge、Funding 和 Stablecoin Basis；
- 双腿执行状态机与单腿恢复；
- Kill Switch、紧急对冲和紧急平仓；
- Pair PnL、持久化和重启恢复；
- 行情新鲜度、延迟指标和交易时段；
- 完整配置说明、测试结果和剩余实盘风险。

这不是策略收益承诺，也不是投资建议。该程序除 `--record-only` 外没有模拟盘，
正常启动会向真实交易所发送真实订单。

---

## 2. 当前范围和不做的事情

### 2.1 当前支持的结构

一条腿固定为 Entropy，另一条腿由启动参数选择：

| `--hedge` | 对冲交易所 | 默认计价资产 |
|---|---|---|
| `lighter` | Lighter 主网 | USDC |
| `lighter-rh` | Lighter Robinhood Chain | USDG |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC |

核心交易仍然是：

```text
Entropy 贵：卖出 Entropy + 买入 hedge venue
Entropy 便宜：买入 Entropy + 卖出 hedge venue
价差回归：两腿反向退出
```

### 2.2 本阶段明确不做

- 不同时连接多个 hedge venue；
- 不自动比较多个交易所的盘口；
- 不做跨 venue 的资金分配；
- 不按 Expected PnL 自动挑选交易所；
- 不为了低延迟重写为 Rust 或更换整个网络层；
- 不承诺交易所宕机时一定能够成交或平仓。

当前优先级仍然是把一组 `Entropy + hedge venue` 的数据、信号、执行、对账和风控
做稳定。

---

## 3. 总体架构

```text
┌───────────────────────────────────────────────────────────────┐
│                         Market Data                           │
│ Entropy/HL l2Book                         Lighter Order Book  │
└───────────────┬──────────────────────────────────┬────────────┘
                │                                  │
                v                                  v
┌───────────────────────────────────────────────────────────────┐
│ 行情质量层                                                   │
│ connection staleness / book age / exchange timestamp / skew │
└───────────────────────────────┬───────────────────────────────┘
                                v
┌───────────────────────────────────────────────────────────────┐
│ Session + Statistics                                         │
│ crypto 24/7 或美股时段 / Fast EMA / Slow Median / Vol / Z   │
│ Regime Detection                                             │
└───────────────────────────────┬───────────────────────────────┘
                                v
┌───────────────────────────────────────────────────────────────┐
│ Signal Lifecycle                                             │
│ Static band 或 Dynamic Z-score / OPEN / ADD / EXIT          │
└───────────────────────────────┬───────────────────────────────┘
                                v
┌───────────────────────────────────────────────────────────────┐
│ Executable Pricing                                           │
│ 双边 Order Book VWAP / quote→USD / fees / funding / buffer  │
│ 二分搜索最大可交易仓位                                      │
└───────────────────────────────┬───────────────────────────────┘
                                v
┌───────────────────────────────────────────────────────────────┐
│ Execution + Risk                                             │
│ 双腿并发 / 状态机 / partial recovery / hedge / kill switch  │
└───────────────────────────────┬───────────────────────────────┘
                                v
┌───────────────────────────────────────────────────────────────┐
│ Accounting + Observability                                   │
│ Pair PnL / JSONL 审计 / restart snapshot / latency / CSV    │
└───────────────────────────────────────────────────────────────┘
```

### 3.1 关键设计原则

1. **信号和成交价分离**：统计模型判断价差是否异常，真实订单簿判断是否能赚钱。
2. **慢线是交易基准**：Fast EMA 只用于发现状态变化，不直接追着异常价格开仓。
3. **新增风险 fail-closed**：关键数据缺失、过期或异常时禁止 OPEN/ADD。
4. **退出优先于利润**：风险暂停不会阻断 EXIT、紧急对冲和紧急平仓。
5. **两腿作为一个 Pair 记账**：不再只观察每个交易所单腿盈亏。
6. **恢复优先于继续套利**：出现不等量成交后，首要任务是恢复 Delta Neutral。
7. **尽量兼容原项目**：保留原交易所适配器、Static Midline 和旧版 sizing 开关。

---

## 4. 模块与职责

| 文件 | 职责 |
|---|---|
| `main.py` | 命令行入口、配置加载、中文仪表盘开关、风险暂停人工复位 |
| `entropy_arb/config.py` | YAML 配置契约、默认值、未知字段拒绝和参数校验 |
| `entropy_arb/book.py` | 订单簿维护、旧版套利计算、V2 plan 组装 |
| `entropy_arb/pricing.py` | 当前盘口 VWAP、Executable Edge、二分仓位搜索 |
| `entropy_arb/midline.py` | Fast EMA、Slow Median、STD/MAD、Z-score、Regime |
| `entropy_arb/session.py` | 虚拟货币 24/7 与美股交易时段单开关 |
| `entropy_arb/costs.py` | Funding、新鲜度、quote/USD 和脱锚保护 |
| `entropy_arb/models.py` | Pair、执行状态机、风险事件、延迟时间线领域模型 |
| `entropy_arb/ledger.py` | Pair PnL、追加式审计日志、运行状态快照 |
| `entropy_arb/metrics.py` | 延迟样本和 P50/P95/P99 |
| `entropy_arb/feeds.py` | 行情时间戳规范化和订单簿推送 |
| `entropy_arb/venue_hl.py` | Entropy/trade.xyz 行情、订单、持仓、Funding 适配 |
| `entropy_arb/venue_lighter.py` | Lighter 行情、订单、持仓、Funding 适配 |
| `entropy_arb/engine.py` | 策略循环、执行、恢复、对账、Kill Switch 和持久化协调 |
| `entropy_arb/dashboard.py` | 盘口、信号、时段、成本、风险、Pair PnL 和延迟展示 |
| `entropy_arb/recorder.py` | 分钟级订单簿数据采集 |
| `tools/analyze.py` | 对采集数据进行价差分布分析 |

---

## 5. 价差定义

统计层按 `thresholds.price_basis` 计算 Entropy 相对对冲腿的溢价。推荐的
`usd` 模式使用：

```text
premium_bps
  = (Entropy mid × Entropy quote/USD
     / (Hedge mid × Hedge quote/USD) - 1) × 10,000
```

`raw` 模式保留旧公式和旧配置兼容性。选定的同一口径用于 Midline、波动率、
Z-score、Regime 和方向门槛；USD 汇率不会被重复换算。该价差不等于最终可成交利润。

真正下单前使用的是：

```text
贵的一边 Sell VWAP - 便宜的一边 Buy VWAP
```

然后再扣除手续费、资金费、安全缓冲和预期延迟成本，并对不同计价资产换算 USD。

---

## 6. Static Midline 兼容模式

配置：

```yaml
midline:
  mode: static
```

Static 模式保留原有语义：

```text
卖出 Entropy 门槛 = midline_bps + upper_bps
买入 Entropy 门槛 = lower_bps - midline_bps
```

`midline_bps` 不是必须为 0。不同预言机、计价资产或市场结构可能使长期跨所价差
自然偏向一边。Static 模式的主要风险是人工填写的中枢过期，导致程序把正常价差
误认为异常价差。

---

## 7. Dynamic Midline

配置：

```yaml
midline:
  mode: dynamic
  fast_method: ema
  fast_window_seconds: 300
  slow_method: median
  slow_window_seconds: 1800
  min_samples: 300
  volatility_method: std
  volatility_window_seconds: 1800
  volatility_floor_bps: 0.1
  entry_z_score: 2.5
  exit_z_score: 0.5
```

### 7.1 Fast Midline

Fast Midline 使用按真实时间间隔更新的 EMA：

```text
alpha = 1 - exp(-elapsed_seconds / fast_window_seconds)
fast  = fast + alpha × (spread - fast)
```

Fast Midline 的职责是检测短期价差中心是否快速移动，不是唯一交易基准。

### 7.2 Slow Midline

Slow Midline 使用最近 `slow_window_seconds` 内样本的 Rolling Median：

```text
slow_midline = median(recent_spreads)
```

选择中位数而不是快速均值，是为了降低极端点和短期异常对交易基准的污染。

### 7.3 波动率

支持两种模式：

```yaml
volatility_method: std | mad
```

- `std`：总体标准差；
- `mad`：`median(|x - median(x)|) × 1.4826`；
- 最终值不会低于 `volatility_floor_bps`，避免平稳行情除以 0。

### 7.4 Z-score

```text
deviation = current_spread - slow_midline
z_score   = deviation / rolling_volatility
```

Dynamic 模式的生命周期：

```text
空仓：Z >= +entry_z  → OPEN 卖 Entropy、买 hedge
空仓：Z <= -entry_z  → OPEN 买 Entropy、卖 hedge

已有同方向 Pair：再次越过 entry_z → ADD

卖 Entropy Pair：Z 回到 +exit_z 以内 → EXIT
买 Entropy Pair：Z 回到 -exit_z 以内 → EXIT
```

Dynamic 模式中，`upper_bps` 和 `lower_bps` 不再驱动 OPEN/ADD/EXIT，只保留给
Static 模式兼容。

Static 的 upper/lower 是价格比率空间的信号距离，不是固定美元利润保证。即使两次
成交分别跨过两侧阈值，共同价格水平、两边真实成交量、Funding 与 quote/USD 汇率
也可能在持仓期间变化。`midline=5, upper=4, lower=3` 时，按
`100.091/100` 开仓、再按 `1000/999.801` 退出即可得到未计费约 `-$0.108/base`
的反例。因此分析工具不得把 `upper+lower` 描述为无条件往返收益下限。

反向 BUY-Entropy 门槛由原始 `midline-lower` 边界取精确倒数得到，不使用
`lower-midline` 的线性近似。Dynamic 的 Z-score 边界也先在 Entropy/Hedge 原始
比率空间计算，再按执行方向取倒数并换算 quote/USD。

### 7.5 预热

在有效样本数小于 `min_samples` 时：

```text
PAUSE_NEW_ENTRY
```

静态 `thresholds.midline_bps` 在预热期间只作为显示种子，不会绕过预热直接交易。
Dynamic Midline 的窗口数据目前保存在内存中，进程重启后会重新预热。

---

## 8. Regime Detection

Regime 模块同时检查：

```text
abs(fast_midline - slow_midline)
abs(current_spread)
abs(z_score)
异常持续时间
恢复持续时间
```

配置：

```yaml
regime:
  enabled: true
  max_fast_slow_difference_bps: 8
  max_z_score: 5
  max_absolute_spread_bps: 50
  break_persist_seconds: 1
  recovery_persist_seconds: 30
```

触发后只暂停新增仓位：

```text
OPEN / ADD → 禁止
EXIT       → 保留
HEDGE      → 保留
FLATTEN    → 保留
```

设置 `break_persist_seconds` 是为了过滤单次脉冲；设置
`recovery_persist_seconds` 是为了避免状态在阈值附近频繁开关。

---

## 9. 虚拟货币与股票交易时段

配置只有一个开关，不自动根据 symbol 猜测资产类型：

```yaml
session:
  enabled: false
```

### 9.1 虚拟货币

```yaml
session:
  enabled: false
```

行为：

- 24/7；
- 不限制 OPEN/ADD；
- 使用一个连续统计池。

### 9.2 股票永续

```yaml
session:
  enabled: true
```

行为：

- 使用美国东部时间；
- 只使用夜盘、盘前、正常时段和盘后四个统计状态；
- 处理美国夏令时；
- 处理周末、常规美股节假日和重复出现的 13:00 ET 提前收盘；
- 所有时段都允许 OPEN/ADD/EXIT；
- Session 只隔离统计状态，不直接触发 `PAUSE_NEW_ENTRY`。

时段边界：

```text
20:00-04:00  overnight（夜盘）
04:00-09:30  pre_market（盘前）
09:30-16:00  regular（正常时段）
16:00-20:00  after_hours（盘后）
周末/节假日  overnight（夜盘统计池）
```

提前收盘日从 13:00 起进入 `after_hours`。夜盘、盘前、正常时段和盘后分别拥有
独立的 Dynamic Midline、波动率、Z-score 和 Regime 状态，防止不同预言机与流动性
机制互相污染。20:00 直接切换到夜盘是股票永续的统计设计：它消除现货时段定义中
20:00-21:00 的空档，不代表合约在这段时间停止交易。周末和节假日如果合约仍提供
行情，也归入夜盘统计池；是否交易由行情新鲜度、Executable Edge 和风险系统决定。

每个时段完成自己的预热后，即可按正常的 Z-score、Executable Edge、成本和风险
规则 OPEN/ADD。如果 Pair 已经存在，而新时段统计尚未完成预热，EXIT 可以使用最近
一个已就绪的中枢作为保守的风险降低基准。临时全国休市或交易所特殊规则无法通过
固定日历预测，仍需依赖行情过期和交易所故障保护。

---

## 10. 行情新鲜度

每个订单簿保存：

```text
exchange_timestamp
local_receive_timestamp
book_age_ms
```

系统区分两类问题：

1. `execution.staleness_sec`：WebSocket 或连接多久没有收到消息；
2. `market_data.max_book_age_ms`：订单簿本身多久没有真实更新，以及有服务器时间戳时
   交易所快照距本地接收时间是否过旧。

配置：

```yaml
market_data:
  enforce_book_age: true
  max_book_age_ms: 300
```

任一参与交易的盘口出现以下情况时禁止新增交易：

- disconnected；
- not ready；
- empty book；
- connection stale；
- book stale；
- exchange stale、明显未来的交易所时间戳。

行情恢复后，瞬时行情风险事件可以自动清除；持久 Kill Switch 事件不会因为重启自动
清除。

---

## 11. 当前订单簿 VWAP

这里的 VWAP 不是 K 线或 TradingView VWAP，而是当前订单簿逐档模拟成交。

买入数量 `Q`：

```text
从最低 ask 开始逐档吃单
buy_vwap = Σ(price_i × filled_i) / Q
```

卖出数量 `Q`：

```text
从最高 bid 开始逐档吃单
sell_vwap = Σ(price_i × filled_i) / Q
```

同时计算：

- top price；
- worst consumed price；
- VWAP slippage；
- book impact；
- 是否有足够深度完成目标数量。

盘口不足时返回不可执行，不会使用部分盘口假设完整成交。

---

## 12. Executable Edge

### 12.1 计价资产换算

两边使用不同计价资产时，先换算成 USD：

```text
buy_usd  = buy_vwap  × qty × buy_quote_usd
sell_usd = sell_vwap × qty × sell_quote_usd
```

### 12.2 费用和净收益

```text
fee_usd
  = buy_usd × buy_fee_rate
  + sell_usd × sell_fee_rate

extra_cost_bps
  = safety_buffer_bps
  + expected_latency_cost_bps
  + funding_cost_bps
  + optional_manual_basis_cost_bps

expected_profit_usd
  = sell_usd - buy_usd
  - fee_usd
  - buy_usd × extra_cost_bps / 10,000

expected_net_edge_bps
  = expected_profit_usd / buy_usd × 10,000
```

订单簿可见滑点已经包含在 VWAP 内，不会再重复扣一次。

只有同时满足信号门槛与 Executable Edge 门槛时才允许下单。

---

## 13. VWAP 自动仓位计算

配置：

```yaml
sizing:
  vwap_enabled: true
  min_order_usd: 1000
  max_order_usd: 50000
  minimum_net_edge_bps: 6
  max_vwap_slippage_bps: 5
  max_book_impact_bps: 5
  safety_buffer_bps: 2
  expected_latency_cost_bps: 0
```

### 13.1 搜索上限

候选最大数量取以下限制的最小值：

- 买盘可见深度；
- 卖盘可见深度；
- `max_order_usd`；
- 两边持仓上限余量；
- EXIT 时当前 Pair 的剩余匹配数量；
- 交易所最小数量和数量步长。

### 13.2 二分搜索

在订单簿正常排序时，随着数量增加，VWAP edge 通常单调下降。因此程序：

1. 先验证最小可交易数量；
2. 再验证最大候选数量；
3. 最大数量不通过时，按交易所数量步长二分；
4. 返回满足所有约束的最大共同基础资产数量。

每个候选数量必须同时通过：

- 两边盘口完整；
- 最小/最大名义金额；
- minimum net edge；
- max VWAP slippage；
- max book impact；
- 持仓上限；
- Pair EXIT 数量上限。

关闭 `vwap_enabled` 后，系统仍可使用旧版 `take_fraction + max_order_notional`
路径，但实盘建议使用 V2 VWAP sizing。

---

## 14. Funding

配置：

```yaml
funding:
  enabled: true
  expected_holding_hours: 1
  refresh_seconds: 60
  max_age_seconds: 180
```

当前费率按方向估算：

```text
卖 Entropy / 买 hedge：hourly_cost = -entropy_rate + hedge_rate
买 Entropy / 卖 hedge：hourly_cost =  entropy_rate - hedge_rate

funding_cost_bps
  = hourly_cost × expected_holding_hours × 10,000
```

开仓前使用预计资金费；Pair 持有或完成后，系统尝试读取交易所账户 Funding History，
用交易所实际数据更新 Pair PnL。

适配器统一把费率转换为小时口径：Hyperliquid `metaAndAssetCtxs` 的当前 Funding
直接按小时使用；Lighter funding-rates 接口返回的 8 小时等效值除以 8。两边账户
历史分别通过 Hyperliquid `userFunding` 和 Lighter `positionFunding` 汇总为正数表示
成本、负数表示收入。

启用后，如果任一交易所 Funding 数据缺失或超过 `max_age_seconds`：

```text
OPEN / ADD → 禁止
EXIT       → 允许
```

Funding 预测只是当前费率外推，无法保证未来费率不变。

---

## 15. Stablecoin Basis

配置：

```yaml
entropy:
  quote_asset: USDC

hedge:
  quote_asset: USDG

stablecoin:
  enabled: true
  provider: kraken
  source_url: https://api.kraken.com
  refresh_seconds: 30
  max_age_seconds: 90
  max_spread_bps: 10
  warning_deviation_bps: 10
  halt_deviation_bps: 30
```

系统读取 Kraken `ASSET/USD` 一档盘口中间价，将每条腿独立换算成 USD。USDG
指 Lighter Robinhood 使用的 Paxos USDG；Bitget USDGO 是不同资产，不得替代。
盘口必须有限、为正、未交叉、未过期且 spread 不超过 `max_spread_bps`，并且全部
所需资产验证成功后才原子更新。Stablecoin Basis 不是简单塞进 Dynamic Midline，
而是作为独立成本和风险输入。下式是参考报价基差，执行成本字段会按买卖方向取符号：

```text
USDG/USDC basis bps = (USDG_USD / USDC_USD - 1) * 10000
```

状态：

- 低于 warning：正常；
- 超过 warning：仪表盘告警；
- 超过 halt：禁止 OPEN/ADD；
- 数据缺失或过期：禁止 OPEN/ADD。

外部现货价格源也可能过期、失真或不能代表目标规模的真实可成交价格，因此该模块
只能降低风险，不能消除风险。

---

## 16. 信号到下单的完整流程

```text
1. 两边订单簿更新
2. 检查连接、盘口年龄和深度
3. 检查股票/虚拟货币 Session
4. 更新对应 Session 的 Midline、Vol、Z-score、Regime
5. 确定 OPEN / ADD / EXIT
6. 检查暂停原因
7. 模拟双边当前订单簿 VWAP
8. 换算 quote/USD
9. 扣手续费、Funding、延迟和 Safety Buffer
10. 二分搜索最大可交易数量
11. 信号持续时间确认
12. 为执行生成 pair_id
13. 并发发送两条腿
14. 记录 ACK、Fill 和两腿成交差
15. 完成、恢复、对冲或撤销
16. 更新 Pair PnL、CSV、状态快照和审计事件
```

---

## 17. Execution State Machine

状态：

```text
NEW
  ↓
SIGNAL_CONFIRMED
  ↓
ORDERS_SENT
  ├──────────────→ BOTH_FILLED ─→ COMPLETE
  │                       └─────→ RECOVERY
  ├──────────────→ PARTIAL ─────→ RECOVERY
  └──────────────→ RECOVERY
                              ├─→ HEDGED ─→ COMPLETE
                              ├─→ UNWINDING ─→ COMPLETE
                              └─→ FAILED
```

每次状态变化记录：

- 时间；
- 原状态；
- 新状态；
- 原因；
- 相关成交或恢复数据。

非法状态跳转会被拒绝。例如已经 `COMPLETE` 或 `FAILED` 的执行不能再次回到运行状态。

---

## 18. One-leg Risk 与恢复

两腿仍尽可能并发发送，但成交结果可能不一致。

系统持续计算：

```text
effective_buy_fill
effective_sell_fill
matched_fill
unmatched_fill
net_delta_usd
```

启用：

```yaml
execution:
  risk_recovery_enabled: true
  hedge_timeout_ms: 250
  max_unhedged_delta_usd: 5000
```

### 18.1 已知 OPEN/ADD 单腿成交

如果 `hedge_timeout_ms` 到期时只确认一条 OPEN/ADD 腿成交，系统优先在该交易所发送
reduce-only 反向单撤销已知单腿。

### 18.2 未知结果

如果订单结果 unresolved，不会假设未成交。系统会在释放两边执行锁前写入持久化
`PAUSE_NEW_ENTRY`，因此对账完成前不能 OPEN/ADD；只保留对账、EXIT 和 reduce-only
风险恢复。随后触发持仓对账，等待真实结果后恢复净 Delta。单纯重启不会清除暂停。

### 18.3 EXIT

EXIT 的另一腿结果未知时，不盲目把已完成的退出腿重新开回去，而是等待结算和持仓
对账，再恢复 Delta Neutral。所有 EXIT 双腿都强制 `reduce_only=true`，即使本地
Pair 数量过期也不能反向开仓。

### 18.4 启动持仓保护

实盘启动时先完成双方严格持仓查询，再创建策略任务。若两边基础资产数量不平衡且
超过 `net_tolerance_base`，立即持久暂停 OPEN/ADD 并唤醒对账/对冲流程。行情尚未
就绪时也不会先放行策略。

### 18.5 Hyperliquid 实际成交均价

`/exchange` 超时或 5xx 后，先用 cloid 查询 `orderStatus`，再按返回的 exchange
order ID 查询成交历史并按数量计算实际 VWAP。若成交数量已确认但成交历史仍拿不到
均价，系统不会使用计划限价伪造现金流或 PnL；只持久化已知数量，把 Pair 标记为
`accounting_complete=false` 并持久暂停新增仓位，等待人工审计或后续真实成交证据。

### 18.6 恢复记账

撤销单腿、补 hedge 和恢复滑点都记录到原 `pair_id` 的 `recovery_pnl`、手续费和
净 PnL 中，不会创建一笔看起来独立盈利、实际掩盖恢复损失的新 Pair。

---

## 19. Kill Switch

风险动作分为：

| 动作 | 含义 |
|---|---|
| `PAUSE_NEW_ENTRY` | 禁止 OPEN/ADD，保留 EXIT 和风险恢复 |
| `EMERGENCY_HEDGE` | 优先减少净 Delta |
| `EMERGENCY_FLATTEN` | 对已知持仓发送 reduce-only 平仓 |

当前风险来源：

| 触发条件 | 当前处理 |
|---|---|
| 行情断连、空盘口、connection stale、book stale | 瞬时暂停新增，恢复后可自动解除 |
| API 连续不可达 | 暂停该 Pair，周期探测恢复 |
| Funding 数据缺失/过期 | 暂停新增 |
| Stablecoin 数据缺失/过期/严重脱锚 | 暂停新增 |
| Regime break | 暂停新增 |
| 股票交易时段切换 | 切换到独立统计池；Session 本身不暂停新增 |
| 净 Delta 超过阈值 | 立即暂停 OPEN/ADD 并尝试 Emergency Hedge；无法估值时计时不重置 |
| 净 Delta 持续超时 | 持久暂停新增；可选 Flatten |
| 连续不等量/部分成交 | 持久暂停新增 |
| 连续执行失败 | 持久暂停新增 |
| 未知订单结果 | 释放执行锁前持久暂停新增并触发对账 |
| 已知成交数量但缺少实际均价 | 账本标记不完整并持久暂停新增 |
| 启动持仓不平衡 | 策略任务创建前持久暂停并安排风险恢复 |
| 链上与本地持仓不一致 | 持久暂停新增并采用对账结果 |
| Pair 账本与链上持仓不一致 | 追加对账事件并持久暂停 |
| 会话 MTM 亏损超过上限 | 持久暂停；可选 Flatten |

配置：

```yaml
kill_switch:
  enabled: true
  max_unhedged_duration_ms: 1000
  max_consecutive_partial_fills: 3
  max_reconcile_mismatch_usd: 1000
  max_session_loss_usd: 0
  emergency_flatten_enabled: false
  emergency_flatten_retry_sec: 2
  emergency_flatten_max_attempts: 0
```

`emergency_flatten_enabled` 默认关闭，因为开启后会发送真实订单。

`emergency_flatten_max_attempts: 0` 表示持续重试，直到已知持仓归零。交易所断连、
盘口缺失、交易所锁占用或订单失败时，待办会写入运行状态快照，重启后继续处理。

任何程序都无法强迫已宕机或拒单的交易所成交，因此 Emergency Flatten 是“持续尝试
并保持风险状态”，不是成交保证。

---

## 20. 持久风险暂停与人工复位

以下风险会持久化：

- 净 Delta 长时间超限；
- 连续部分成交；
- 连续执行失败；
- 未知订单结果；
- 实际成交均价缺失；
- 启动持仓不平衡；
- 持仓对账严重不一致；
- Pair 账本与链上不一致；
- 会话亏损超限；
- 待完成 Emergency Flatten。
- 关闭时仍在途或重启恢复出的非终态执行。

仅仅重启程序不会清除这些暂停。

在人工检查并独立确认两边都已空仓后，可以使用：

```powershell
.\.venv\Scripts\python.exe main.py `
  --symbol SNDK `
  --hedge lighter-rh `
  --clear-risk-pause
```

程序仍会先做实盘持仓对账，只有以下条件同时满足才会清除：

- 两边持仓都在容差内为 0；
- 没有待完成紧急平仓；
- 使用的是实盘对账模式。

`--clear-risk-pause` 不能与 `--record-only` 一起使用。

---

## 21. Pair PnL

每一次套利使用唯一 `pair_id`：

```text
ARB-YYYYMMDD-HHMMSS-XXXXXXXX
```

主要字段：

| 字段 | 含义 |
|---|---|
| `pair_id` | Pair 唯一标识 |
| `symbol` | 交易品种 |
| `venue_a` / `venue_b` | 两条腿交易所 |
| `direction` | sell_entropy / buy_entropy |
| `entry_time` / `exit_time` | 进出场时间 |
| `entry_session` / `exit_session` | 进出场市场时段 |
| `entry_spread` / `exit_spread` | 进出场统计价差 |
| `entry_z` / `exit_z` | 进出场 Z-score |
| `entry_midline` / `exit_midline` | 进出场中枢 |
| `leg_a_entry_vwap` / `leg_b_entry_vwap` | 两腿进场 VWAP |
| `leg_a_exit_vwap` / `leg_b_exit_vwap` | 两腿退出 VWAP |
| `fees` | 双边手续费和恢复手续费 |
| `expected_funding_cost` | 开仓时预计资金费 |
| `funding` / `funding_source` | 最终资金费和来源 |
| `stablecoin_basis` | 基差 bps |
| `stablecoin_basis_usd` | 基差造成的 USD 调整 |
| `entry_slippage` / `exit_slippage` | 相对计划价格的滑点 |
| `recovery_pnl` | 单腿撤销/恢复产生的净损益 |
| `gross_pnl` / `net_pnl` | Pair 毛收益/净收益 |
| `holding_time` | 持仓时间 |
| `max_adverse_spread` | 最大不利价差变化 |
| `max_favorable_spread` | 最大有利价差变化 |
| `entry_base` / `exit_base` | 累计进出数量 |
| `remaining_base` | 未退出匹配数量 |
| `complete` | Pair 是否完成 |
| `accounting_complete` | 是否有完整本地成交证据 |
| `reconciliation_adjustment_base` | 链上对账造成的数量调整 |

```text
net_pnl
  = gross_pnl
  + stablecoin_basis_usd
  - fees
  - funding
```

如果启动后从链上发现一个本地没有完整历史的 Pair，系统会保守恢复持仓，但将
`accounting_complete` 标为 false，不会伪造进场成交。

实盘启动强制要求 `accounting.enabled=true`。若状态目录留下
`runtime-state.json.tmp`，说明上一次原子快照没有完成，程序拒绝实盘重启并要求
人工检查。只要任一腿使用 USDC、USDG 等非 USD 计价资产，实盘也强制要求启用
新鲜的 stablecoin quote/USD 换算；换算缺失、过期或脱锚时禁止 OPEN/ADD。
账户变化和会话 PnL 只使用新鲜换算；过期时返回未知，不能用旧 USDG/USDC 汇率
继续执行 `max_session_loss_usd` 的 USD 计算。

所有 YAML 浮点数必须通过 `math.isfinite()`；`.nan`、`.inf` 和 `-.inf` 在启动时
直接拒绝。手续费、仓位上限、订单上下限、滑点、延迟、次数和时间参数还会执行各自
范围校验。纯函数 planner 也再次拒绝非有限输入，防止绕过配置层直接调用。

---

## 22. 账本与重启恢复

配置：

```yaml
accounting:
  enabled: true
  ledger_jsonl: logs/pair-ledger.jsonl
  state_json: logs/runtime-state.json
```

### 22.1 `pair-ledger.jsonl`

追加式审计事件，包括：

- Pair 创建；
- 每次成交；
- Pair 完成；
- Funding 更新；
- 单腿恢复；
- 链上对账调整；
- 风险暂停人工清除。

写入后执行 flush 和 fsync，尽可能把审计证据落盘。

### 22.2 `runtime-state.json`

使用临时文件写入后原子替换，保存：

- 当前未完成 Pair；
- 最近 200 个完成 Pair；
- 最近 200 个执行状态历史；
- 最近 200 个风险事件；
- 持久暂停原因；
- 待完成 Emergency Flatten；
- Emergency Flatten 尝试次数；
- 等待恢复的 unmatched leg。

状态快照与 JSONL 是两个文件，无法组成数据库级跨文件原子事务。实现优先确保重启后
不会因为审计日志晚一步写入而恢复更旧的风险敞口。

程序关闭只会有界等待正在执行的双腿任务；若任务仍未结束，会先把结果记录为未知并
持久暂停。重启读取到 `ORDERS_SENT`、`PARTIAL`、`BOTH_FILLED`、`RECOVERY`、
`HEDGED` 或 `UNWINDING` 等非终态执行时，不会把它们当作已完成，而是要求先对账。

`requirements-live.txt` 当前不是实盘供应链锁文件：Hyperliquid/签名依赖只有版本下限，
Lighter SDK 直接跟随 GitHub `main`。在取得并本地审计一个确定版本前，不应安装它用于
真实资金；部署时还必须固定 commit、包版本与制品哈希。

---

## 23. 延迟指标

记录时间戳：

```text
market_data_receive_ts
signal_generated_ts
order_send_ts
order_ack_ts
first_fill_ts
final_fill_ts
leg_a_final_fill_ts
leg_b_final_fill_ts
```

输出指标：

```text
market_to_signal_ms
signal_to_send_ms
send_to_ack_ms
ack_to_fill_ms
order_to_final_fill_ms
leg_fill_gap_ms
book_update_skew_ms
exchange_to_local_ms（交易所提供时间戳时）
```

滚动保存最近 2,000 个样本并计算：

```text
P50 / P95 / P99 / max
```

这些指标目前保存在内存中，进程重启后重新统计。Lighter 订单簿推送没有可靠服务端
时间戳时，只记录本地接收时间，不伪造 exchange timestamp。

---

## 24. 配置总览

### 24.1 信号

| 配置 | 作用 |
|---|---|
| `thresholds.price_basis` | `usd` 统一换算后统计与交易；`raw` 兼容旧配置 |
| `thresholds.midline_bps` | Static 中枢；Dynamic 预热显示种子 |
| `thresholds.upper_bps` | Static 上方带宽 |
| `thresholds.lower_bps` | Static 下方带宽 |
| `midline.mode` | static / dynamic |
| `midline.fast_window_seconds` | Fast EMA 时间常数 |
| `midline.slow_window_seconds` | Rolling Median 窗口 |
| `midline.min_samples` | 允许 Dynamic 新增交易前的样本数 |
| `midline.volatility_method` | std / mad |
| `midline.volatility_window_seconds` | 波动率窗口 |
| `midline.volatility_floor_bps` | 最小波动率 |
| `midline.entry_z_score` | OPEN/ADD 阈值 |
| `midline.exit_z_score` | EXIT 回归阈值 |

### 24.2 Regime 与行情

| 配置 | 作用 |
|---|---|
| `regime.enabled` | 是否启用状态断裂保护 |
| `regime.max_fast_slow_difference_bps` | Fast/Slow 最大分叉 |
| `regime.max_z_score` | 最大绝对 Z-score |
| `regime.max_absolute_spread_bps` | 最大绝对价差 |
| `regime.break_persist_seconds` | 异常持续多久才暂停 |
| `regime.recovery_persist_seconds` | 健康持续多久才恢复 |
| `market_data.enforce_book_age` | 是否启用毫秒级盘口年龄 |
| `market_data.max_book_age_ms` | 最大可接受盘口年龄 |
| `session.enabled` | false=虚拟货币 24/7；true=股票时段 |

### 24.3 仓位与成本

| 配置 | 作用 |
|---|---|
| `sizing.vwap_enabled` | 启用当前盘口 VWAP 自动仓位 |
| `sizing.min_order_usd` | 自动仓位最小名义金额 |
| `sizing.max_order_usd` | 自动仓位最大名义金额 |
| `sizing.minimum_net_edge_bps` | 建模成本后的最小方向偏离 |
| `sizing.max_vwap_slippage_bps` | 最大 VWAP 滑点 |
| `sizing.max_book_impact_bps` | 最大最差档冲击 |
| `sizing.safety_buffer_bps` | 显式安全缓冲 |
| `sizing.expected_latency_cost_bps` | 根据实测延迟配置的成本 |
| `*.quote_asset` | 该交易所价格/保证金计价资产 |
| `*.taker_fee_bps` | 吃单手续费 |
| `*.max_position_usd` | 单交易所持仓上限 |
| `inventory.scale_bps` | 接近上限时额外提高的 edge |
| `inventory.floor_frac` | 库存阶梯开始位置 |

### 24.4 执行与风险

| 配置 | 作用 |
|---|---|
| `execution.premium_persist_sec` | 信号需持续多久 |
| `execution.cooldown_sec` | 两次执行最小间隔 |
| `execution.settle_timeout_sec` | 等待成交确认时间 |
| `execution.leg_slippage_bps` | 两条套利腿限价保护 |
| `execution.hedge_slippage_bps` | 紧急对冲限价保护 |
| `execution.net_tolerance_base` | 可容忍基础资产净敞口 |
| `execution.risk_recovery_enabled` | 单腿恢复开关 |
| `execution.hedge_timeout_ms` | 已知单腿等待上限 |
| `execution.max_unhedged_delta_usd` | 净 Delta 紧急对冲阈值 |
| `execution.reconcile_sec` | 持仓对账周期 |
| `kill_switch.*` | 持久暂停和紧急平仓阈值 |

### 24.5 资金费、稳定币和账本

| 配置 | 作用 |
|---|---|
| `funding.enabled` | 启用 Funding 预测和实际核对 |
| `funding.expected_holding_hours` | 预计持仓时间 |
| `funding.refresh_seconds` | 当前费率刷新周期 |
| `funding.max_age_seconds` | Funding 最大数据年龄 |
| `stablecoin.enabled` | 启用 quote/USD 换算和脱锚保护 |
| `stablecoin.provider` | 行情适配器；当前必须为 `kraken` |
| `stablecoin.source_url` | 独立现货价格源 |
| `stablecoin.max_spread_bps` | 换算盘口最大允许 spread |
| `stablecoin.warning_deviation_bps` | 告警阈值 |
| `stablecoin.halt_deviation_bps` | 暂停新增阈值 |
| `accounting.enabled` | Pair PnL 和重启恢复开关 |
| `accounting.ledger_jsonl` | 追加式审计文件 |
| `accounting.state_json` | 运行状态快照 |

所有配置字段都经过严格校验。未知键、空路径、同一个账本/快照文件、非法范围或依赖
冲突会在启动时直接报错，而不是静默忽略。

---

## 25. 推荐运行步骤

### 25.1 安装开发依赖

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item config.example.yaml config.yaml
```

### 25.2 只采集数据

```powershell
.\.venv\Scripts\python.exe main.py `
  --symbol SNDK `
  --hedge lighter-rh `
  --config config.yaml `
  --record-only `
  --cn
```

`--record-only`：

- 不需要交易密钥；
- 不运行交易策略；
- 不发送任何订单；
- 继续采集两边分钟级数据。

### 25.3 分析数据

```powershell
.\.venv\Scripts\python.exe tools\analyze.py
```

### 25.4 实盘前最低检查

- 两边品种和合约乘数一致；
- `quote_asset` 正确；
- taker fee 正确；
- 两边 API 权限和账户地址正确；
- Dynamic Midline 已完成预热；
- Funding 和 Stablecoin 数据显示 fresh；
- Book age、P95/P99 延迟符合部署预期；
- 股票永续已打开 `session.enabled`；
- 从极小的 `max_position_usd` 和 `max_order_usd` 开始；
- `emergency_flatten_enabled` 未经验证不要开启。

---

## 26. 仪表盘与输出文件

仪表盘显示：

- 两边 bid/ask、盘口点差、book age 和 exchange lag；
- 两边持仓、权益、可用余额和成交额；
- 当前市场 Session；
- Midline、波动率、Z-score 和 Regime；
- VWAP sizing 模式与门槛；
- Funding/Stablecoin 成本状态；
- 净 Delta、执行状态、最近风险事件；
- 当前或最近 Pair Net PnL；
- 延迟 P50/P95/P99；
- 最近执行和事件日志。

主要输出：

| 文件 | 内容 |
|---|---|
| `logs/minutes.csv` | raw 与 USD 双口径分钟盘口统计、quote/USD、basis 和有效样本数 |
| `logs/trades.csv` | 每次执行计划、VWAP、成本、时段和成交结果 |
| `logs/pair-ledger.jsonl` | 追加式 Pair 审计事件 |
| `logs/runtime-state.json` | 重启恢复状态 |
| `logs/engine.log` | 完整运行日志 |

---

## 27. 测试

当前完整测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

当前结果：

```text
125 passed
```

覆盖范围包括：

- 订单簿 VWAP；
- 盘口不足；
- VWAP 滑点与冲击；
- 二分仓位搜索；
- Executable Edge 和成本只扣一次；
- Dynamic Midline；
- STD/MAD 与 Z-score；
- Fast/Slow 分叉和 Regime 恢复；
- stale book 和断连；
- Funding 和 Stablecoin Basis；
- 配置严格校验；
- NaN/Infinity 与 planner 二次防御；
- 非有限订单簿档位丢弃；
- 股票 Session、夏令时、周末、节假日和提前收盘；
- Session 统计隔离；
- 股票永续四时段都允许交易并使用独立统计；
- 执行状态机；
- 单腿超时撤销；
- 未知订单结果持久暂停；
- EXIT 两腿 reduce-only；
- 启动不平衡持仓 fail-closed；
- HL 成交历史实际 VWAP 与不完整账本暂停；
- quote→USD 的 sizing、仓位和 MTM 统一换算；
- 连续 partial fill；
- Emergency Hedge 和 Emergency Flatten 重试；
- Pair PnL、Funding、Basis 和恢复损益；
- 账本持久化和重启恢复；
- 风险暂停人工复位；
- 延迟分位数和仪表盘渲染。

同时通过：

```text
Python compileall
git diff --check
```

测试全部通过不等于实盘一定安全。单元测试无法模拟真实交易所排队、撮合优先级、
网络分区、交易所回滚或极端市场中的全部行为。

---

## 28. 相对原项目的文件变化

### 28.1 新增核心模块

```text
entropy_arb/pricing.py
entropy_arb/midline.py
entropy_arb/models.py
entropy_arb/metrics.py
entropy_arb/costs.py
entropy_arb/ledger.py
entropy_arb/session.py
requirements-dev.txt
```

### 28.2 修改的核心文件

```text
main.py
entropy_arb/config.py
entropy_arb/book.py
entropy_arb/feeds.py
entropy_arb/engine.py
entropy_arb/dashboard.py
entropy_arb/venue_hl.py
entropy_arb/venue_lighter.py
config.example.yaml
README.md
README.zh-CN.md
```

### 28.3 新增测试

```text
tests/test_pricing.py
tests/test_midline.py
tests/test_models.py
tests/test_metrics.py
tests/test_costs.py
tests/test_ledger.py
tests/test_feeds.py
tests/test_funding_adapters.py
tests/test_session.py
```

并扩展：

```text
tests/test_book.py
tests/test_config.py
tests/test_dashboard.py
tests/test_engine.py
```

---

## 29. 当前仍无法消除的实盘风险

### 29.1 交易所不可用

交易所宕机、拒单、暂停市场或没有流动性时，程序无法保证另一腿或紧急平仓成交。
持久化和重试只能保证程序继续尝试，不能保证外部系统执行。

### 29.2 信号到成交期间价格变化

即使当前订单簿显示足够 edge，信号计算、签名、发送、排队和撮合期间盘口仍会变化。
Safety Buffer 和延迟成本只能降低风险。

### 29.3 可见深度不等于可成交深度

订单簿可能撤单、被别人抢先成交或包含无法按当前延迟获得的流动性。

### 29.4 Funding 预测误差

预计资金费使用当前费率外推，未来费率可能迅速变化。

### 29.5 Stablecoin 价格源风险

独立现货源本身可能过期、失真或与目标交易所的真实兑换能力不同。

### 29.6 Dynamic Midline 污染

Rolling Median 和 Regime 能降低污染，但无法识别所有永久性市场结构变化、预言机
切换或新资产定价机制。

### 29.7 特殊交易日历

内置股票日历处理常规规则，无法预测临时全国休市、交易所临时提前收盘或股票永续
交易所自己的特殊预言机时段。系统不再产生 `closed` 统计状态；只有本时段统计未
预热、行情过期、成本/价差不合格或风险开关触发时才阻止新增仓位。

### 29.8 本地持久化边界

JSONL 与状态快照不是数据库级跨文件事务。磁盘损坏、权限问题或文件被外部程序改写
仍可能破坏恢复数据，应定期备份日志目录。

### 29.9 模型风险和参数风险

错误的中枢窗口、Z-score、最小 edge、手续费、Funding、计价资产或持仓上限都可能
让逻辑正确的程序执行错误策略。

---

## 30. 当前阶段结论

当前 V2 已经不是只看买一卖一和固定 bps 的基础机器人，而是具备：

```text
Dynamic Midline
+ Z-score lifecycle
+ Regime Detection
+ 当前订单簿 VWAP
+ 自动仓位 sizing
+ Executable Edge
+ Funding
+ Stablecoin Basis
+ Book Freshness
+ Session Awareness
+ 双腿执行状态机
+ 单腿恢复
+ Kill Switch
+ Pair PnL
+ 重启恢复
+ 延迟与仪表盘
```

但是它仍然是高风险实盘交易系统。合理的下一步不是增加更多交易所，而是先进行：

1. 长时间 `--record-only` 数据验证；
2. Funding、Stablecoin 和时段数据源稳定性验证；
3. 最小资金实盘执行验证；
4. P50/P95/P99 与真实滑点统计；
5. Emergency Hedge/Flatten 的受控演练；
6. 根据真实 Pair Net PnL 调整参数。

在这些验证完成前，不建议扩大仓位或增加交易所复杂度。

---

## 31. 外部接口参考

- Hyperliquid Perpetual API：<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals>
- Hyperliquid Funding：<https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding>
- Lighter Python SDK：<https://github.com/elliottech/lighter-python>
- Lighter Funding：<https://docs.lighter.xyz/trading/funding>
- Kraken REST Order Book：<https://docs.kraken.com/api/docs/rest-api/get-order-book/>
- NYSE Holidays & Trading Hours：<https://www.nyse.com/trade/hours-calendars>

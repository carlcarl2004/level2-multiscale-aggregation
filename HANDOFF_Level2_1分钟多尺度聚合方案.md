# Level2 以 1 分钟为最小单位的多尺度聚合方案（已实现并试跑）

日期：2026-10-12。状态：方案已落地为脚本并在服务器上跑通 2025-01-02 的沪深各一只票，全部验收项通过。

## 1. 方案要点

把原来的 3 秒 backward-asof 网格改成 **1 分钟为最小聚合单位**，并在 1 分钟基础上派生 5 分钟、30 分钟：

| 项目 | 原（3 秒版） | 新（1 分钟版） |
|---|---|---|
| 最小时间单位 | 3 秒 | **1 分钟** |
| 一天行数/票 | 9600（两只票 9604） | **240** |
| 窗口 | 3/15/60/300 秒 | **1/5/30 分钟**，5/30 分钟滚动挂在 1 分钟行上 |
| 盘口 | 仅 bar 端点快照 | **bar 内时间加权 + 波动 + 盘口质量 + 形状** |
| 输出表 | 3 秒一张 | **1 分钟主表 + 5 分钟表 + 30 分钟表 + 竞价表** |
| 脚本 | `aggregate_level2_day.py`（保留） | **`aggregate_level2_minute.py`** |

1 分钟不是简单地把 3 秒降采样：它多了一整层「bar 内」信息（时间加权中间价、bar 内已实现波动、盘口深度集中度与斜率、成交间隔、大单占比），这些在 3 秒端点上算不出来。

## 2. 时间口径

- 连续竞价两段：09:30:00–11:30:00、13:00:00–15:00:00，各 120 根，**共 240 根/票/日**。
- 标记为 `t` 的 bar 覆盖左开右闭区间 `(t-60s, t]`，`bar_start`/`bar_end` 两列都写出，避免标签歧义。
- 盘口状态用 backward-asof：只取 `bar_end` 及之前最近的快照，**不做未来插值**。
- 滚动窗口按 bar 计数（1/5/30 根），窗口不跨午休、不跨隔夜；午盘开盘的前几根用 `nb_bars_*`、`warmup_*` 标出部分窗口。
- 收盘集合竞价：默认 `--close-grace-ms 1000`，把 15:00:00.000–15:00:01.000 之间的收盘打印并入最后一根 PM bar（沪市收盘价打印在 15:00:00.990）。
- 集合竞价单列：09:15–09:30 与收盘后的记录进入独立的竞价表，**不混进连续竞价 bar**。
- 时间加权统计对超过 `--max-quote-age`（默认 6 秒）的过期报价不再延展权重，另给 `twap_coverage` 表示覆盖率。

## 3. 三张表的结构

### 3.1 `level2_<date>_1m_features.csv.gz`（主表，140 列）

身份列：`symbol, trading_date, session(AM/PM), bar_index, bar_seq, bar_start, bar_end`

盘口端点状态（bar 收盘，23 列）：`mid, spread, relative_spread, weighted_mid, wm_minus_mid, depth_bid_1/3/5/10, depth_ask_1/3/5/10, di_1/3/5/10, acc_price_diff, acc_size_diff, depth_hhi_bid, depth_hhi_ask, slope_bid, slope_ask`

bar 内盘口统计（20 列）：`quote_age_s, stale_quote, snap_count, valid_l1, locked_count, crossed_count, twap_mid, twap_spread, twap_wm, twap_coverage, mid_open, mid_close, mid_ret, mid_min, mid_max, mid_range, mid_std, realized_vol, ofi_raw, ofi_count`

事件流滚动（1/5/30 分钟各一组，每组 27 列）：`trade_buy_qty, trade_sell_qty, trade_unknown_qty, trade_buy_count, trade_sell_count, trade_count, trade_amount, order_buy_qty, order_sell_qty, order_unknown_qty, order_count, cancel_buy_qty, cancel_sell_qty, cancel_unknown_qty, cancel_count, [ofi_raw], trade_imb, order_imb, cancel_imb, trade_amount_per_min, trade_count_per_min, order_count_per_min, ofi_per_min, trade_vwap, nb_bars, warmup`

bar 级成交分布（8 列）：`vwap, avg_trade_size, max_trade_size, large_trade_ratio_x10med, iat_mean_s, iat_median_s, iat_p90_s, zero_gap_ratio`

尺度比较（2 列）：`trade_amt_rate_ratio_1m_30m, trade_cnt_rate_ratio_1m_30m`

标签（3 列，**不是特征**）：`label_fwd_logret_1m/5m/30m`，用未来 bar 收盘中间价比计算，缺失（跨午休/收盘/停牌）留空。

### 3.2 `level2_<date>_5m_features.csv.gz` / `_30m_features.csv.gz`（72 / 71 列）

非重叠粗 bar：收盘十档状态 + 窗口内盘口统计（`valid_l1_ratio, snap_count, twap_*, mid_open/close/ret/min/max/range/std, realized_vol, locked/crossed_count, ofi_raw`）+ 窗口事件流与不平衡度/速率。全部由 1 分钟表按 5 根/30 根重组，可逐列对账。

### 3.3 `level2_<date>_auction_features.csv`（每票 1–3 行）

`pre_open` / `lunch` / `post_close` 三段，列含成交/委托/撤单汇总与最后一条竞价快照的 `quote_last_mid`（沪市 09:25 的 10.295 即开盘竞价参考价）。

### 3.4 其他输出

`level2_<date>_daily_summary.csv`（每票一行，含对账列）、`level2_<date>_feature_dictionary_1m.csv`（列名/公式/单位/known_at）、`level2_<date>_metadata.json`。

## 4. 验收结果（2025-01-02，000001.SZ + 600000.SH）

服务器：`/aidata/wentao/level2_agg_2025/pilot1m_20250102/`，耗时 2.3 秒（含解压）。

| 检查 | 000001.SZ | 600000.SH |
|---|---|---|
| 行情快照行数 | 4836 | 4969 |
| 1 分钟 bar 数 | 240（239 根有有效盘口） | 240 |
| 有效成交笔数 | 108734 | 68602 |
| 逐笔成交金额 | 2102923078.11 | 803904040.23 |
| 行情累计成交额 | 2102923078 | 803904040 |
| 差额 | 0.11 | 0.23 |
| bar 汇总 + 竞价汇总 − 文件总额 | 0.0 | 0.0 |

- 与上一版 3 秒聚合的成交笔数、成交金额**完全一致**（108734 / 68602，2102923078.11 / 803904040.23），只有时间刻度变了。
- **因果性测试**：把原始数据截断到 10:00:00.000 重跑，137 个特征列、60 行 bar 与全量运行逐格相同（0 处不一致）；只有跨截断点的 70 个标签格按预期变化，标签已排除在严格比较之外。
- **跨尺度一致性**：5 分钟/30 分钟表的 12 个汇总列与对应 1 分钟 bar 之和逐格相等（1456 次比对，0 处不一致）。
- **对账闭合**：SZ 连续竞价 2093310331.11 + 开盘竞价 9612747.00 = 2102923078.11；SH 连续竞价 801412470.23 + 开盘竞价 2491570.00 = 803904040.23，与行情累计成交额一致。
- 数据质量：`逐笔成交.csv` 各 1 行既非成交也非撤单（价格或数量为 0），已在汇总中以 `trade_rows_no_price` 计数，不再静默丢弃。时间列全部可解析（`*_bad_time` 全为 0）。

## 5. 复跑命令

```bash
python /aidata/wentao/level2_agg_2025/aggregate_level2_minute.py \
  --archive /aidata/wentao/market_data/202501/20250102.7z \
  --out /aidata/wentao/level2_agg_2025/pilot1m_20250102 \
  --date 20250102 \
  --symbols 000001.SZ 600000.SH
```

先解压一次、反复调参时可加 `--raw-dir <已解压日目录>` 跳过解压；`--truncate-ms` 用于因果性回归测试；`verify_causality.py` 做逐格比对。

## 6. 全年成本

1 分钟网格不增加解压与解析量（这部分与原 3 秒版相同），反而省掉了逐事件多窗口双端队列。按此前的全日基准，7330 只票约 3.5–4 小时/日；2025 年约 240 个交易日，单机串行约 35–40 天，需要按股票切分并行（例如 8 路 → 5 天左右）。输出体量按每天每股 240 行估算，全年压缩后可控。

## 7. 已知限制

- 盘口是交易所十档观测快照，不是重建后的完整订单簿；`ofi_raw` 是按快照链计算的 sampled quote OFI，遗漏 bar 内价格往返路径，不等于事件级 OFI。
- `trade_imb` 用的是供应商 BS 标志，未验证主动方语义；上海缺少完整逐笔委托/撤单链路，`order_*`/`cancel_*` 在沪市含义弱于深市。
- `large_trade_ratio_x10med` 用 bar 内中位数做阈值，是描述性指标；要进预测矩阵应改为训练期分位数。
- 最后一根 PM bar 吸收收盘竞价打印，存在不超过 1 秒的信息前视，只影响当日最后一行，且不参与任何前瞻标签。
- `label_fwd_*` 是标签列，做特征时务必剔除。

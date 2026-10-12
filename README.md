# A 股 Level2 多尺度特征聚合

把逐日 Level2 原始日包（十档行情 + 逐笔委托 + 逐笔成交）聚合成**以 1 分钟为最小单位**的多尺度特征表，供微观结构研究、因子挖掘与建模使用。

## 为什么是 1 分钟

1 分钟 bar 除端点快照外，还能保留一整层「bar 内」信息：时间加权中间价、bar 内已实现波动、盘口深度集中度与斜率、成交间隔分布、大单占比。这些在 3 秒端点网格上算不出来，而更细的刻度会让行数膨胀十倍以上。1 分钟作为基础时钟，再向上派生 5 分钟、30 分钟，短中长三个尺度挂在同一行上。

## 目录结构

| 路径 | 说明 |
|---|---|
| `aggregate_level2_minute.py` | 主脚本：1 分钟多尺度聚合（当前版本） |
| `aggregate_level2_minute_README.md` | 主脚本参数与输出说明 |
| `HANDOFF_Level2_1分钟多尺度聚合方案.md` | 方案全文：口径、特征族、验收结果、成本估算 |
| `aggregate_level2_day.py` | 早期 3 秒版本，保留用于对照，不再维护 |
| `HANDOFF_Level2订单簿与聚合.md` | 早期 3 秒版的字段与订单簿说明 |
| `verify_causality.py` | 截断数据后逐格比对，验证特征不依赖未来信息 |
| `probe_counts.py` | 原始文件行数画像（成交 / 撤单 / 异常行） |
| `preview_1m.py` | 生成便于人工查看的关键列预览 |
| `make_synthetic_level2.py` | 离线合成测试数据，无需真实行情即可自测 |
| `remote_ops.py`、`inspect_remote.py` | 远程服务器辅助脚本（密码通过环境变量传入） |
| `outputs_1m_20250102/` | 1 分钟版试跑结果：2025-01-02，沪深各一只票 |
| `outputs/` | 早期 3 秒版试跑结果（同一天、同样两只票） |
| `tmp_audit_20261012/` | 原始数据的审计脚本、字段字典与实施方案 |

## 运行

```bash
python aggregate_level2_minute.py \
  --archive /aidata/wentao/market_data/202501/20250102.7z \
  --out ./out/pilot1m_20250102 \
  --date 20250102 \
  --symbols 000001.SZ 600000.SH
```

省略 `--symbols` 处理整包；已有解压目录时用 `--raw-dir` 跳过解压。详细参数见 `aggregate_level2_minute_README.md`。

## 输出

每个交易日、每个输出目录：

- `level2_<date>_1m_features.csv.gz`：1 分钟主表，240 行/票，140 列
- `level2_<date>_5m_features.csv.gz`、`level2_<date>_30m_features.csv.gz`：非重叠粗 bar
- `level2_<date>_auction_features.csv`：开盘 / 午间 / 收盘竞价汇总
- `level2_<date>_daily_summary.csv`、`level2_<date>_feature_dictionary_1m.csv`、`level2_<date>_metadata.json`

## 验收

已用 2025-01-02 的 `000001.SZ` 与 `600000.SH` 跑通：

- 逐笔成交笔数与金额和早期 3 秒版完全一致（108734 / 2102923078.11、68602 / 803904040.23）
- 逐笔金额与行情累计成交额差 0.11 / 0.23 元（整数舍入）
- 「连续竞价 bar + 集合竞价」与文件总额差 0.0，对账闭合
- 截断到 10:00 重跑，137 个特征列、60 行 bar 逐格相同，仅跨截断标签按预期变化
- 5 / 30 分钟表与对应 1 分钟 bar 之和逐格一致（1456 次比对）

## 已知限制

- 盘口是交易所十档观测快照，不是重建后的完整订单簿；`ofi_raw` 为 sampled quote OFI，不等于事件级 OFI。
- 深市 `成交代码=C` 已按撤单处理；沪市缺少完整逐笔委托链路，`order_*` / `cancel_*` 的语义弱于深市。
- `trade_imb` 沿用供应商 BS 标志，未验证主动方语义。
- `label_fwd_logret_*` 是标签列，不参与特征。
- 原始商业行情数据不随本仓库分发。

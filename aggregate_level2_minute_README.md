# 1 分钟多尺度 Level2 聚合

`aggregate_level2_minute.py` 把一个交易日的 Level2 日包聚合成以 **1 分钟为最小单位**的特征表，并派生 5 分钟、30 分钟与集合竞价表。原始 `.7z` 与解压出的临时目录不做修改，处理结束后只删除自己创建的临时解压目录。

输出（同一目录）：

- `level2_<date>_1m_features.csv.gz`：1 分钟主表，240 行/票，含 bar 内盘口统计与 1/5/30 分钟滚动事件流
- `level2_<date>_5m_features.csv.gz`、`level2_<date>_30m_features.csv.gz`：非重叠粗 bar
- `level2_<date>_auction_features.csv`：开盘/午间/收盘竞价汇总
- `level2_<date>_daily_summary.csv`、`level2_<date>_feature_dictionary_1m.csv`、`level2_<date>_metadata.json`

```bash
python aggregate_level2_minute.py \
  --archive /aidata/wentao/market_data/202501/20250102.7z \
  --out /aidata/wentao/level2_agg_2025/pilot1m_20250102 \
  --date 20250102 \
  --symbols 000001.SZ 600000.SH
```

常用参数：

- `--symbols`：只解压并处理指定股票；省略表示整包
- `--raw-dir`：复用已解压的日目录，跳过 7z 解压
- `--max-quote-age`：时间加权统计信任报价的最长年龄，默认 6 秒
- `--close-grace-ms`：收盘打印并入最后一根 PM bar 的窗口，默认 1000 毫秒
- `--truncate-ms`：丢弃该毫秒之后的所有原始记录，用于因果性回归测试
- `--keep-raw`：保留临时解压目录

配套脚本：`verify_causality.py`（截断前后逐格比对）、`probe_counts.py`（原始文件行数画像）、`preview_1m.py`（生成便于人工查看的关键列预览）、`make_synthetic_level2.py`（离线合成测试数据）。

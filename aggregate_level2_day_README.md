# 一天 Level2 聚合试跑

脚本 `aggregate_level2_day.py` 针对一个日 `.7z` 包和指定股票解压临时文件，按 GB18030 字段位置读取行情、逐笔委托、逐笔成交，生成 3 秒 backward-asof 网格，并计算 3/15/60/300 秒窗口特征。价格整数统一除以 10000；深交所 `成交代码=C` 只计入撤单，不计入成交量、成交额和成交笔数。临时解压目录默认处理结束后删除。

服务器上的复跑示例：

```bash
python /aidata/wentao/level2_agg_2025/aggregate_level2_day.py \
  --archive /aidata/wentao/market_data/202501/20250102.7z \
  --out /aidata/wentao/level2_agg_2025/pilot_20250102 \
  --date 20250102 \
  --symbols 000001.SZ 600000.SH
```

输出为 `level2_20250102_3s_features.csv.gz`、`level2_20250102_daily_summary.csv` 和 `level2_20250102_metadata.json`。

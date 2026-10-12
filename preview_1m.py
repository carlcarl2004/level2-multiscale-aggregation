#!/usr/bin/env python3
"""Write a small human-readable preview of the 1-minute table and print checks."""
import csv
import gzip
import sys
from pathlib import Path

SRC = Path('outputs_1m_20250102/level2_20250102_1m_features.csv.gz')
OUT = Path('outputs_1m_20250102/preview_1m_key_columns.csv')
COLS = ['symbol', 'trading_date', 'session', 'bar_seq', 'bar_start', 'bar_end',
        'mid_close', 'spread', 'weighted_mid', 'twap_mid', 'twap_coverage',
        'mid_ret', 'realized_vol', 'snap_count', 'quote_age_s', 'stale_quote',
        'depth_bid_10', 'depth_ask_10', 'di_10', 'ofi_raw',
        'trade_count_1m', 'trade_amount_1m', 'trade_imb_1m', 'vwap',
        'cancel_count_1m', 'order_count_1m',
        'trade_count_5m', 'trade_imb_5m', 'trade_count_30m', 'trade_amount_30m',
        'trade_imb_30m', 'nb_bars_30m', 'warmup_30m',
        'label_fwd_logret_1m', 'label_fwd_logret_30m']


def f(x):
    return float(x) if x not in ('', None) else None


def main():
    with gzip.open(SRC, 'rt', encoding='utf-8', newline='') as fh:
        r = csv.reader(fh)
        header = next(r)
        rows = [dict(zip(header, x)) for x in r]
    print('rows', len(rows), 'columns', len(header))
    by = {}
    for x in rows:
        by.setdefault(x['symbol'], []).append(x)
    for sym, rs in by.items():
        assert len(rs) == 240, (sym, len(rs))
        cov = [f(x['twap_coverage']) for x in rs if f(x['twap_coverage']) is not None]
        print(f'{sym}: bars={len(rs)} twap_coverage mean={sum(cov)/len(cov):.4f} '
              f'min={min(cov):.4f} stale_bars={sum(1 for x in rs if x["stale_quote"]=="1")} '
              f'trade_bars={sum(1 for x in rs if f(x["trade_count_1m"]))} '
              f'warmup_ok={all(x["warmup_30m"]=="1" for x in rs[:29]) and all(x["warmup_30m"]=="0" for x in rs[29:120])}')
        print(f'   last PM bar: {rs[239]["bar_end"]} trade_count_1m={rs[239]["trade_count_1m"]} '
              f'mid_close={rs[239]["mid_close"]} spread={rs[239]["spread"]}')
    want = {'09:31:00.000', '10:00:00.000', '11:30:00.000',
            '13:01:00.000', '14:57:00.000', '15:00:00.000'}
    picked = [x for x in rows if x['bar_end'] in want]
    with open(OUT, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for x in picked:
            w.writerow([x.get(c, '') for c in COLS])
    print('preview rows', len(picked), '->', OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())

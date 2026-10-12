#!/usr/bin/env python3
"""Write a tiny synthetic Level2 day so the aggregator can be tested offline.

Column positions follow the vendor field dictionary, so the same parser paths
are exercised: 61-column quotes, 12-column trades and 10-column orders.
"""
import argparse
import os
from pathlib import Path

QUOTE_COLS = 61
TRADE_COLS = 12
ORDER_COLS = 10


def hhmmssmmm(ms):
    sec, milli = divmod(ms, 1000)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{m:02d}{s:02d}{milli:03d}"


def quote_row(ms, ap, aq, bp, bq, cum_vol, cum_amt, cum_cnt):
    x = [0] * QUOTE_COLS
    x[3] = hhmmssmmm(ms)
    x[7] = cum_cnt
    x[11] = cum_vol
    x[12] = cum_amt
    for i in range(10):
        x[17 + i] = ap[i]
        x[27 + i] = aq[i]
        x[37 + i] = bp[i]
        x[47 + i] = bq[i]
    return x


def trade_row(ms, code, side, price, qty, ask_ref, bid_ref):
    x = [0] * TRADE_COLS
    x[3] = hhmmssmmm(ms)
    x[5] = code
    x[7] = side
    x[8] = price
    x[9] = qty
    x[10] = ask_ref
    x[11] = bid_ref
    return x


def order_row(ms, typ, side, price, qty):
    x = [0] * ORDER_COLS
    x[3] = hhmmssmmm(ms)
    x[6] = typ
    x[7] = side
    x[8] = price
    x[9] = qty
    return x


def write_csv(path, header, data):
    with open(path, 'w', encoding='gb18030', newline='') as f:
        f.write(','.join(header) + '\n')
        for row in data:
            f.write(','.join(str(v) for v in row) + '\n')


def build(symbol, out_dir):
    exchange = symbol.split('.')[-1]
    os.makedirs(out_dir, exist_ok=True)
    base_bid, base_ask = 99900, 100100
    quotes, trades, orders = [], [], []
    cum_vol = cum_amt = cum_cnt = 0
    t = (9 * 3600 + 29 * 60) * 1000
    end = (9 * 3600 + 36 * 60) * 1000
    tick_no = 0
    while t <= end:
        shift = (tick_no // 20) % 5 - 2
        ap = [base_ask + shift * 100 + 100 * i for i in range(10)]
        bp = [base_bid + shift * 100 - 100 * i for i in range(10)]
        aq = [100 * (i + 1) for i in range(10)]
        bq = [100 * (i + 1) for i in range(10)]
        quotes.append(quote_row(t, ap, aq, bp, bq, cum_vol, cum_amt, cum_cnt))
        tick_no += 1
        t += 3000
    for k in range(60):
        ms = (9 * 3600 + 30 * 60) * 1000 + 1000 + k * 4000
        qty = 100 * (1 + k % 7)
        if exchange == 'SZ' and k % 10 == 9:
            trades.append(trade_row(ms, 'C', '', 0, qty, 0, 900000 + k))
            continue
        px = 100000 if k % 3 else 100100
        side = 'B' if k % 2 else 'S'
        cum_vol += qty
        cum_amt += px * qty // 10000
        cum_cnt += 1
        trades.append(trade_row(ms, '0', side, px, qty, 800000 + k, 900000 + k))
    boundary_ms = (11 * 3600 + 30 * 60) * 1000
    cum_vol += 500
    cum_amt += 100000 * 500 // 10000
    cum_cnt += 1
    trades.append(trade_row(boundary_ms, '0', 'B', 100000, 500, 800999, 900999))
    pm_ms = (13 * 3600) * 1000 + 10000
    cum_vol += 300
    cum_amt += 100000 * 300 // 10000
    cum_cnt += 1
    trades.append(trade_row(pm_ms, '0', 'S', 100000, 300, 801000, 901000))
    if exchange == 'SZ':
        for k in range(40):
            ms = (9 * 3600 + 30 * 60) * 1000 + 2000 + k * 5000
            orders.append(order_row(ms, '0', 'B' if k % 2 else 'S', 100000, 100 * (1 + k % 5)))
    else:
        for k in range(40):
            ms = (9 * 3600 + 30 * 60) * 1000 + 2000 + k * 5000
            typ = 'D' if k % 7 == 6 else 'A'
            orders.append(order_row(ms, typ, 'B' if k % 2 else 'S', 100000, 100 * (1 + k % 5)))
    qh = ['万得代码', '交易所代码', '自然日', '时间', '成交价', '成交量', '成交额',
          '成交笔数', 'IOPV', '成交标志', 'BS标志', '当日累计成交量', '当日成交额',
          '最高价', '最低价', '开盘价', '前收盘'] + \
         [f'申卖价{i}' for i in range(1, 11)] + [f'申卖量{i}' for i in range(1, 11)] + \
         [f'申买价{i}' for i in range(1, 11)] + [f'申买量{i}' for i in range(1, 11)]
    th = ['万得代码', '交易所代码', '自然日', '时间', '成交编号', '成交代码', '委托代码',
          'BS标志', '成交价格', '成交数量', '叫卖序号', '叫买序号']
    oh = ['万得代码', '交易所代码', '自然日', '时间', '委托编号', '交易所委托号',
          '委托类型', '委托代码', '委托价格', '委托数量']
    write_csv(Path(out_dir) / '行情.csv', qh, quotes)
    write_csv(Path(out_dir) / '逐笔成交.csv', th, trades)
    write_csv(Path(out_dir) / '逐笔委托.csv', oh, orders)
    return {'symbol': symbol, 'quotes': len(quotes), 'trades': len(trades),
            'orders': len(orders), 'cum_vol': cum_vol, 'cum_amt': cum_amt, 'cum_cnt': cum_cnt}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--date', required=True)
    ap.add_argument('--symbols', nargs='+', required=True)
    a = ap.parse_args()
    day = Path(a.out) / a.date
    for sym in a.symbols:
        print(build(sym, day / sym))


if __name__ == '__main__':
    main()

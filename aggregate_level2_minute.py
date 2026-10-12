#!/usr/bin/env python3
"""Aggregate one Level2 trading day into 1-minute multi-scale feature tables.

The minimum aggregation unit is one minute.  Each day produces three tables:

* ``level2_<date>_1m_features.csv.gz``
  Base clock. One row per 1-minute bar holding the bar-close ten-level state,
  intra-bar quote statistics (time weighted mid, realized volatility, quote
  quality) and the event flow of the bar itself. Trailing 5-minute and
  30-minute rolling windows are attached on the same row so every multi-scale
  feature stays on the 1-minute clock.
* ``level2_<date>_5m_features.csv.gz`` / ``_30m_features.csv.gz``
  Non-overlapping coarser bars, re-aggregated from the 1-minute rows.

Conventions
-----------
* Continuous trading only, split into 09:30-11:30 and 13:00-15:00.
* A bar labelled ``t`` covers the half open interval ``(t-1min, t]``.  No
  future information is used: the bar-close state is the last snapshot at or
  before ``t`` (backward as-of) and rolling windows never cross the lunch
  break.
* Prices are integers scaled by 10000; a raw integer divided by 10000 gives
  yuan.  Sizes are shares.
* Shenzhen ``成交代码=C`` rows are cancellations, never trades.
* Time weighted statistics exclude stretches where the latest quote is older
  than ``--max-quote-age`` seconds and report the covered fraction.

The raw vendor files remain the source of truth.  Only the temporary
extraction directory created by this program is removed.
"""
import argparse, csv, gzip, json, math, shutil, statistics, time
from pathlib import Path

SESSIONS = (((9 * 3600 + 30 * 60) * 1000, (11 * 3600 + 30 * 60) * 1000),
            (13 * 3600 * 1000, 15 * 3600 * 1000))
BARS_PER_SESSION = 120
BAR_MS = 60000
N_BARS = 2 * BARS_PER_SESSION
WINDOW_MIN = (1, 5, 30)
COARSE_MIN = (5, 30)
TICK_INT = 100


def clean(x):
    if x is None:
        return ''
    return str(x).replace('\x00', '').strip()


def num(x, default=0):
    s = clean(x)
    try:
        return int(s) if s else default
    except ValueError:
        return default


def t_ms(x):
    s = clean(x).zfill(9)
    if len(s) != 9 or not s.isdigit():
        return None
    return ((num(s[:2]) * 60 + num(s[2:4])) * 60 + num(s[4:6])) * 1000 + num(s[6:9])


def t_label(ms):
    sec, milli = divmod(ms, 1000)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def fmt(v):
    if v is None:
        return ''
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ''
        return round(v, 6)
    return v


def safe_div(a, b):
    if a is None or not b:
        return None
    return a / b


def ratio(a, b):
    d = a + b
    return ((a - b) / d) if d else None


def percentile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


def bar_index(t):
    for k, (a, b) in enumerate(SESSIONS):
        if a < t <= b:
            i = (t - a - 1) // BAR_MS
            if 0 <= i < BARS_PER_SESSION:
                return k * BARS_PER_SESSION + i
    return None


def bar_pos(t, grace_ms=0):
    """Bar index for a timestamp; closing print grace attaches to the last PM bar."""
    i = bar_index(t)
    if i is not None:
        return i
    if grace_ms and SESSIONS[1][1] < t <= SESSIONS[1][1] + grace_ms:
        return N_BARS - 1
    return None


def phase_of(t, grace_ms=0):
    """Auction bucket name for a timestamp that falls outside every bar."""
    if t <= SESSIONS[0][0]:
        return 'pre_open'
    if SESSIONS[0][1] < t < SESSIONS[1][0]:
        return 'lunch'
    if t > SESSIONS[1][1] + grace_ms:
        return 'post_close'
    return None


def bar_grid():
    out = []
    for a, _ in SESSIONS:
        out.extend(a + i * BAR_MS for i in range(1, BARS_PER_SESSION + 1))
    return out


def rows(path):
    with open(path, 'r', encoding='gb18030', errors='replace', newline='') as f:
        r = csv.reader(f)
        next(r, None)
        for x in r:
            if x and len(x) >= 4:
                yield x


def parse_quote(path, max_ms=None):
    out = []
    bad = 0
    for x in rows(path):
        if len(x) < 61:
            continue
        tm = t_ms(x[3])
        if tm is None:
            bad += 1
            continue
        if max_ms is not None and tm > max_ms:
            continue
        out.append((tm, [num(v) for v in x]))
    out.sort(key=lambda z: z[0])
    return out, bad


def parse_trade(path, exchange, max_ms=None):
    out = []
    n = cancel = unknown = buy = sell = bad = skipped = 0
    amt = 0.0
    for x in rows(path):
        if len(x) < 12:
            continue
        tm = t_ms(x[3])
        if tm is None:
            bad += 1
            continue
        if max_ms is not None and tm > max_ms:
            continue
        code = clean(x[5])
        side = clean(x[7]).upper()
        px = num(x[8])
        qty = num(x[9])
        bid_ref = num(x[11])
        ask_ref = num(x[10])
        if exchange == 'SZ' and code == 'C':
            cancel += 1
            cside = 'B' if bid_ref > 0 and ask_ref == 0 else ('S' if ask_ref > 0 and bid_ref == 0 else '')
            out.append((tm, 'C', cside, 0, qty, 0.0))
            continue
        if px <= 0 or qty <= 0:
            skipped += 1
            continue
        n += 1
        value = px * qty / 10000.0
        if side == 'B':
            buy += qty
        elif side == 'S':
            sell += qty
        else:
            unknown += qty
        amt += value
        out.append((tm, 'T', side, px, qty, value))
    out.sort(key=lambda z: z[0])
    return out, {'rows': n + cancel, 'trades': n, 'cancels': cancel,
                 'buy_qty': buy, 'sell_qty': sell, 'unknown_qty': unknown,
                 'amount': amt, 'bad_time': bad, 'no_price': skipped}


def parse_orders(path, exchange, max_ms=None):
    out = []
    stats = {'rows': 0, 'new': 0, 'cancel': 0, 'buy_qty': 0, 'sell_qty': 0,
             'cancel_qty': 0, 'bad_time': 0}
    for x in rows(path):
        if len(x) < 10:
            continue
        tm = t_ms(x[3])
        if tm is None:
            stats['bad_time'] += 1
            continue
        if max_ms is not None and tm > max_ms:
            continue
        typ = clean(x[6]).upper()
        side = clean(x[7]).upper()
        qty = num(x[9])
        stats['rows'] += 1
        if exchange == 'SH' and typ == 'D':
            stats['cancel'] += 1
            stats['cancel_qty'] += qty
            kind = 'C'
        else:
            stats['new'] += 1
            if side == 'B':
                stats['buy_qty'] += qty
            elif side == 'S':
                stats['sell_qty'] += qty
            kind = 'N'
        out.append((tm, kind, side, qty))
    out.sort(key=lambda z: z[0])
    return out, stats


STATE_KEYS = ['mid', 'spread', 'relative_spread', 'weighted_mid', 'wm_minus_mid',
              'depth_bid_1', 'depth_bid_3', 'depth_bid_5', 'depth_bid_10',
              'depth_ask_1', 'depth_ask_3', 'depth_ask_5', 'depth_ask_10',
              'di_1', 'di_3', 'di_5', 'di_10',
              'acc_price_diff', 'acc_size_diff',
              'depth_hhi_bid', 'depth_hhi_ask', 'slope_bid', 'slope_ask']


def hhi(sizes):
    tot = sum(sizes)
    if tot <= 0:
        return None
    return sum((s / tot) ** 2 for s in sizes)


def slope(prices, sizes, mid_int):
    pts = [(abs(p - mid_int) / TICK_INT, s) for p, s in zip(prices, sizes) if p > 0]
    if len(pts) < 3:
        return None
    tot = sum(s for _, s in pts)
    if tot <= 0:
        return None
    xs, ys, cum = [], [], 0
    for x, s in pts:
        cum += s
        xs.append(x)
        ys.append(cum / tot)
    if len(set(xs)) < 3:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx


def lob_state(a):
    ap = [a[17 + i] for i in range(10)]
    aq = [a[27 + i] for i in range(10)]
    bp = [a[37 + i] for i in range(10)]
    bq = [a[47 + i] for i in range(10)]
    valid = int(ap[0] > 0 and bp[0] > 0)
    st = {'valid_l1': valid, 'best_ask_p': ap[0], 'best_ask_q': aq[0],
          'best_bid_p': bp[0], 'best_bid_q': bq[0]}
    if valid:
        mid = (ap[0] + bp[0]) / 20000.0
        spread = (ap[0] - bp[0]) / 10000.0
        den = aq[0] + bq[0]
        wm = ((ap[0] * bq[0] + bp[0] * aq[0]) / den / 10000.0) if den > 0 else None
        st.update(mid=mid, spread=spread, weighted_mid=wm,
                  relative_spread=safe_div(spread, mid),
                  wm_minus_mid=(wm - mid) if wm is not None else None)
    else:
        st.update(mid=None, spread=None, weighted_mid=None,
                  relative_spread=None, wm_minus_mid=None)
    for k in (1, 3, 5, 10):
        db = sum(bq[:k])
        da = sum(aq[:k])
        st[f'depth_bid_{k}'] = db
        st[f'depth_ask_{k}'] = da
        st[f'di_{k}'] = ratio(db, da)
    st['acc_price_diff'] = sum(ap) - sum(bp)
    st['acc_size_diff'] = sum(bq) - sum(aq)
    st['depth_hhi_bid'] = hhi(bq)
    st['depth_hhi_ask'] = hhi(aq)
    if valid:
        mid_int = (ap[0] + bp[0]) / 2.0
        st['slope_bid'] = slope(bp, bq, mid_int)
        st['slope_ask'] = slope(ap, aq, mid_int)
    else:
        st['slope_bid'] = st['slope_ask'] = None
    return st


QUOTE_BAR_KEYS = ['quote_age_s', 'stale_quote', 'snap_count', 'valid_l1',
                  'locked_count', 'crossed_count',
                  'twap_mid', 'twap_spread', 'twap_wm', 'twap_coverage',
                  'mid_open', 'mid_close', 'mid_ret', 'mid_min', 'mid_max',
                  'mid_range', 'mid_std', 'realized_vol', 'ofi_raw', 'ofi_count']

RAW_FLOW_KEYS = ['trade_buy_qty', 'trade_sell_qty', 'trade_unknown_qty',
                 'trade_buy_count', 'trade_sell_count', 'trade_count',
                 'trade_amount',
                 'order_buy_qty', 'order_sell_qty', 'order_unknown_qty', 'order_count',
                 'cancel_buy_qty', 'cancel_sell_qty', 'cancel_unknown_qty', 'cancel_count']

BAR_ONLY_KEYS = ['vwap', 'avg_trade_size', 'max_trade_size',
                 'large_trade_ratio_x10med',
                 'iat_mean_s', 'iat_median_s', 'iat_p90_s', 'zero_gap_ratio']

COARSE_STATE_KEYS = ['valid_l1', 'snap_count', 'quote_age_s',
                     'twap_mid', 'twap_spread', 'mid_open', 'mid_close', 'mid_ret',
                     'mid_min', 'mid_max', 'mid_range', 'mid_std', 'realized_vol',
                     'valid_l1_ratio', 'locked_count', 'crossed_count', 'ofi_raw']

FLOW_EXTRA_KEYS = ['trade_imb', 'order_imb', 'cancel_imb',
                   'trade_amount_per_min', 'trade_count_per_min',
                   'order_count_per_min', 'ofi_per_min', 'trade_vwap']

AUCTION_HEADER = ['symbol', 'trading_date', 'phase', 'first_time', 'last_time',
                  'trade_rows', 'trade_count', 'trade_buy_qty', 'trade_sell_qty',
                  'trade_unknown_qty', 'trade_amount',
                  'order_rows', 'order_count', 'order_buy_qty', 'order_sell_qty',
                  'cancel_count',
                  'quote_rows', 'quote_last_time', 'quote_last_mid', 'quote_last_valid_l1']


def blank_auction():
    return {'trade_rows': 0, 'trade_count': 0, 'trade_buy_qty': 0,
            'trade_sell_qty': 0, 'trade_unknown_qty': 0, 'trade_amount': 0.0,
            'order_rows': 0, 'order_count': 0, 'order_buy_qty': 0,
            'order_sell_qty': 0, 'cancel_count': 0,
            'quote_rows': 0, 'quote_last_time': None, 'quote_last_mid': None,
            'quote_last_valid_l1': None,
            'first': None, 'last': None}


def rolling_names(w):
    names = [f'{k}_{w}m' for k in RAW_FLOW_KEYS]
    if w > 1:
        names += [f'ofi_raw_{w}m']
    names += [f'{k}_{w}m' for k in FLOW_EXTRA_KEYS]
    names += [f'nb_bars_{w}m', f'warmup_{w}m']
    return names


def header_1m():
    h = ['symbol', 'trading_date', 'session', 'bar_index', 'bar_seq', 'bar_start', 'bar_end']
    h += STATE_KEYS + QUOTE_BAR_KEYS
    for w in WINDOW_MIN:
        h += rolling_names(w)
    h += BAR_ONLY_KEYS
    h += ['trade_amt_rate_ratio_1m_30m', 'trade_cnt_rate_ratio_1m_30m',
          'label_fwd_logret_1m', 'label_fwd_logret_5m', 'label_fwd_logret_30m']
    return h


def header_coarse(w):
    h = ['symbol', 'trading_date', 'session', 'bar_index', 'bar_seq', 'bar_start', 'bar_end']
    h += STATE_KEYS + COARSE_STATE_KEYS + RAW_FLOW_KEYS + FLOW_EXTRA_KEYS
    h += [f'label_fwd_logret_{w}m']
    if w == 5:
        h += ['label_fwd_logret_30m']
    return h


SUMMARY_HEADER = ['symbol', 'trading_date', 'quote_rows', 'bars', 'bars_with_quote',
                  'bars_valid_l1', 'bars_with_trade', 'trade_rows', 'cancel_rows',
                  'trade_buy_qty', 'trade_sell_qty', 'trade_unknown_qty', 'trade_amount',
                  'sum_bar_trade_amount', 'auction_trade_amount',
                  'bar_plus_auction_trade_amount', 'order_file_rows', 'new_order_rows',
                  'cancel_order_rows', 'order_buy_qty', 'order_sell_qty', 'cancel_order_qty',
                  'quote_rows_bad_time', 'trade_rows_bad_time', 'trade_rows_no_price',
                  'order_rows_bad_time']

FEATURE_DEFS = {
    'mid': ('(ask1+bid1)/2/10000', 'yuan', 'bar close'),
    'spread': ('(ask1-bid1)/10000', 'yuan', 'bar close'),
    'relative_spread': ('spread/mid', 'ratio', 'bar close'),
    'weighted_mid': ('(a1*qb1+b1*qa1)/(qa1+qb1)/10000', 'yuan', 'bar close'),
    'wm_minus_mid': ('weighted_mid-mid', 'yuan', 'bar close'),
    'di_1': ('(depth_bid_1-depth_ask_1)/(depth_bid_1+depth_ask_1)', 'ratio', 'bar close'),
    'di_3': ('(depth_bid_3-depth_ask_3)/(depth_bid_3+depth_ask_3)', 'ratio', 'bar close'),
    'di_5': ('(depth_bid_5-depth_ask_5)/(depth_bid_5+depth_ask_5)', 'ratio', 'bar close'),
    'di_10': ('(depth_bid_10-depth_ask_10)/(depth_bid_10+depth_ask_10)', 'ratio', 'bar close'),
    'depth_bid_1': ('sum(bid qty 1..1)', 'shares', 'bar close'),
    'depth_bid_3': ('sum(bid qty 1..3)', 'shares', 'bar close'),
    'depth_bid_5': ('sum(bid qty 1..5)', 'shares', 'bar close'),
    'depth_bid_10': ('sum(bid qty 1..10)', 'shares', 'bar close'),
    'depth_ask_1': ('sum(ask qty 1..1)', 'shares', 'bar close'),
    'depth_ask_3': ('sum(ask qty 1..3)', 'shares', 'bar close'),
    'depth_ask_5': ('sum(ask qty 1..5)', 'shares', 'bar close'),
    'depth_ask_10': ('sum(ask qty 1..10)', 'shares', 'bar close'),
    'acc_price_diff': ('sum(ask price 1..10)-sum(bid price 1..10)', '1/10000 yuan', 'bar close'),
    'acc_size_diff': ('sum(bid qty 1..10)-sum(ask qty 1..10)', 'shares', 'bar close'),
    'depth_hhi_bid': ('sum((q_i/sum(q))^2) over bid levels', 'ratio', 'bar close'),
    'depth_hhi_ask': ('sum((q_i/sum(q))^2) over ask levels', 'ratio', 'bar close'),
    'slope_bid': ('OLS slope of cumulative bid depth share on distance from mid in ticks', 'ratio/tick', 'bar close'),
    'slope_ask': ('OLS slope of cumulative ask depth share on distance from mid in ticks', 'ratio/tick', 'bar close'),
    'quote_age_s': ('bar_end minus timestamp of latest snapshot at or before bar_end', 'seconds', 'bar_end'),
    'stale_quote': ('1 if quote_age_s > max_quote_age', 'flag', 'bar_end'),
    'snap_count': ('snapshots inside the bar', 'count', 'bar_end'),
    'locked_count': ('snapshots with bid1 == ask1', 'count', 'bar_end'),
    'crossed_count': ('snapshots with bid1 > ask1', 'count', 'bar_end'),
    'twap_mid': ('time weighted mid over the bar, stale segments excluded', 'yuan', 'bar_end'),
    'twap_spread': ('time weighted spread over the bar', 'yuan', 'bar_end'),
    'twap_wm': ('time weighted weighted_mid over the bar', 'yuan', 'bar_end'),
    'twap_coverage': ('share of the bar covered by non-stale valid quotes', 'ratio', 'bar_end'),
    'mid_open': ('first observed mid inside the bar', 'yuan', 'bar_end'),
    'mid_close': ('mid at bar_end', 'yuan', 'bar_end'),
    'mid_ret': ('log(mid_close/mid_open)', 'log return', 'bar_end'),
    'mid_min': ('minimum observed mid inside the bar', 'yuan', 'bar_end'),
    'mid_max': ('maximum observed mid inside the bar', 'yuan', 'bar_end'),
    'mid_range': ('mid_max-mid_min', 'yuan', 'bar_end'),
    'mid_std': ('population std of observed mids inside the bar', 'yuan', 'bar_end'),
    'realized_vol': ('sqrt(sum of squared consecutive log-mid changes in the bar)', 'log return', 'bar_end'),
    'ofi_raw': ('sum of Cont-Kukanov-Stoikov L1 quote OFI over consecutive snapshots', 'shares', 'bar_end'),
    'ofi_count': ('snapshot transitions used for ofi_raw', 'count', 'bar_end'),
    'trade_buy_qty': ('quantity of B side trade rows', 'shares', 'bar_end'),
    'trade_sell_qty': ('quantity of S side trade rows', 'shares', 'bar_end'),
    'trade_unknown_qty': ('quantity of trade rows with empty side', 'shares', 'bar_end'),
    'trade_buy_count': ('number of B side trade rows', 'count', 'bar_end'),
    'trade_sell_count': ('number of S side trade rows', 'count', 'bar_end'),
    'trade_count': ('valid trade rows', 'count', 'bar_end'),
    'trade_amount': ('sum(price*qty)/10000 over valid trade rows', 'yuan', 'bar_end'),
    'order_buy_qty': ('new bid order quantity', 'shares', 'bar_end'),
    'order_sell_qty': ('new ask order quantity', 'shares', 'bar_end'),
    'order_unknown_qty': ('new order quantity with empty side', 'shares', 'bar_end'),
    'order_count': ('new order rows (SZ order file, SH non-cancel rows)', 'count', 'bar_end'),
    'cancel_buy_qty': ('cancelled bid quantity (SZ code C with bid ref, SH type D)', 'shares', 'bar_end'),
    'cancel_sell_qty': ('cancelled ask quantity', 'shares', 'bar_end'),
    'cancel_unknown_qty': ('cancelled quantity with unknown side', 'shares', 'bar_end'),
    'cancel_count': ('cancellation rows', 'count', 'bar_end'),
    'trade_imb': ('(buy_qty-sell_qty)/(buy_qty+sell_qty)', 'ratio', 'bar_end'),
    'order_imb': ('(order_buy_qty-order_sell_qty)/(sum)', 'ratio', 'bar_end'),
    'cancel_imb': ('(cancel_buy_qty-cancel_sell_qty)/(sum)', 'ratio', 'bar_end'),
    'trade_amount_per_min': ('trade_amount divided by minutes in the window', 'yuan/min', 'bar_end'),
    'trade_count_per_min': ('trade_count divided by minutes in the window', 'count/min', 'bar_end'),
    'order_count_per_min': ('order_count divided by minutes in the window', 'count/min', 'bar_end'),
    'ofi_per_min': ('ofi_raw divided by minutes in the window', 'shares/min', 'bar_end'),
    'trade_vwap': ('trade_amount divided by traded quantity', 'yuan', 'bar_end'),
    'vwap': ('trade_amount divided by traded quantity of the bar', 'yuan', 'bar_end'),
    'avg_trade_size': ('mean trade size inside the bar', 'shares', 'bar_end'),
    'max_trade_size': ('largest trade inside the bar', 'shares', 'bar_end'),
    'large_trade_ratio_x10med': ('share of trades larger than 10x the bar median size', 'ratio', 'bar_end'),
    'iat_mean_s': ('mean trade inter-arrival time; first gap may start before the bar', 'seconds', 'bar_end'),
    'iat_median_s': ('median trade inter-arrival time', 'seconds', 'bar_end'),
    'iat_p90_s': ('p90 trade inter-arrival time', 'seconds', 'bar_end'),
    'zero_gap_ratio': ('share of trade gaps equal to 0 ms', 'ratio', 'bar_end'),
    'trade_amt_rate_ratio_1m_30m': ('1-minute amount rate divided by 30-minute amount rate', 'ratio', 'bar_end'),
    'trade_cnt_rate_ratio_1m_30m': ('1-minute trade count rate divided by 30-minute trade count rate', 'ratio', 'bar_end'),
    'label_fwd_logret_1m': ('log(next bar close mid / this bar close mid); LABEL not a feature', 'log return', 'future'),
    'label_fwd_logret_5m': ('log(close mid 5 minutes ahead / this close mid); LABEL not a feature', 'log return', 'future'),
    'label_fwd_logret_30m': ('log(close mid 30 minutes ahead / this close mid); LABEL not a feature', 'log return', 'future'),
    'valid_l1': ('1 if ask1 > 0 and bid1 > 0', 'flag', 'bar close'),
    'valid_l1_ratio': ('share of 1-minute bars in the window with valid L1', 'ratio', 'bar_end'),
}


def feature_dictionary(names):
    out = []
    for name in names:
        d = FEATURE_DEFS.get(name)
        if d is None:
            for w in WINDOW_MIN:
                suf = f'_{w}m'
                if not name.endswith(suf):
                    continue
                base = name[:-len(suf)]
                b = FEATURE_DEFS.get(base)
                if b:
                    d = (f'{b[0]}, summed over the trailing {w} minute window', b[1], f'bar_end trailing {w}m')
                elif base.startswith(('nb_bars', 'warmup')):
                    d = ('bars available in the trailing window; warmup=1 marks a partial window', 'count', 'bar_end')
                elif base.endswith('per_min'):
                    d = ('window total divided by the number of 1-minute bars', 'per minute', 'bar_end')
                break
        if d is None:
            d = ('see aggregate_level2_minute.py', '', 'bar_end')
        out.append([name, d[0], d[1], d[2]])
    return out


def window_roll(vals, w):
    out = [0.0] * N_BARS
    nb = [0] * N_BARS
    for s in range(0, N_BARS, BARS_PER_SESSION):
        pre = [0.0] * (BARS_PER_SESSION + 1)
        for i in range(s, s + BARS_PER_SESSION):
            pre[i - s + 1] = pre[i - s] + vals[i]
        for i in range(s, s + BARS_PER_SESSION):
            lo = max(0, i - s - w + 1)
            out[i] = pre[i - s + 1] - pre[lo]
            nb[i] = i - s + 1 - lo
    return out, nb


def process_symbol(sym_dir, date, max_age_s, truncate_ms=None, close_grace_ms=1000):
    symbol = sym_dir.name
    exchange = symbol.split('.')[-1]
    qf = sym_dir / '行情.csv'
    tf = sym_dir / '逐笔成交.csv'
    of = sym_dir / '逐笔委托.csv'
    if not (qf.exists() and tf.exists()):
        return None
    quotes, quote_bad_time = parse_quote(qf, truncate_ms)
    trades, tstat = parse_trade(tf, exchange, truncate_ms)
    if of.exists():
        orders, ostat = parse_orders(of, exchange, truncate_ms)
    else:
        orders = []
        ostat = {'rows': 0, 'new': 0, 'cancel': 0, 'buy_qty': 0, 'sell_qty': 0,
                 'cancel_qty': 0, 'bad_time': 0}

    grid = bar_grid()
    flow = {k: [0.0] * N_BARS for k in RAW_FLOW_KEYS}
    trade_times = [[] for _ in range(N_BARS)]
    trade_sizes = [[] for _ in range(N_BARS)]
    auctions = {}

    def auc(tm):
        ph = phase_of(tm, close_grace_ms)
        if ph is None:
            return None
        return auctions.setdefault(ph, blank_auction())

    for qt, arr in quotes:
        if bar_pos(qt, close_grace_ms) is not None:
            continue
        b = auc(qt)
        if b is None:
            continue
        b['quote_rows'] += 1
        st = lob_state(arr)
        b['quote_last_time'] = t_label(qt)
        b['quote_last_mid'] = st['mid']
        b['quote_last_valid_l1'] = st['valid_l1']

    for tm, typ, side, _px, qty, val in trades:
        i = bar_pos(tm, close_grace_ms)
        if i is None:
            b = auc(tm)
            if b is None:
                continue
            b['trade_rows'] += 1
            b['first'] = tm if b['first'] is None else min(b['first'], tm)
            b['last'] = tm if b['last'] is None else max(b['last'], tm)
            if typ == 'C':
                b['cancel_count'] += 1
                continue
            b['trade_count'] += 1
            b['trade_amount'] += val
            if side == 'B':
                b['trade_buy_qty'] += qty
            elif side == 'S':
                b['trade_sell_qty'] += qty
            else:
                b['trade_unknown_qty'] += qty
            continue
        if typ == 'C':
            flow['cancel_count'][i] += 1
            if side == 'B':
                flow['cancel_buy_qty'][i] += qty
            elif side == 'S':
                flow['cancel_sell_qty'][i] += qty
            else:
                flow['cancel_unknown_qty'][i] += qty
            continue
        flow['trade_count'][i] += 1
        flow['trade_amount'][i] += val
        if side == 'B':
            flow['trade_buy_qty'][i] += qty
            flow['trade_buy_count'][i] += 1
        elif side == 'S':
            flow['trade_sell_qty'][i] += qty
            flow['trade_sell_count'][i] += 1
        else:
            flow['trade_unknown_qty'][i] += qty
        trade_times[i].append(tm)
        trade_sizes[i].append(qty)

    for tm, kind, side, qty in orders:
        i = bar_pos(tm, close_grace_ms)
        if i is None:
            b = auc(tm)
            if b is None:
                continue
            b['order_rows'] += 1
            b['first'] = tm if b['first'] is None else min(b['first'], tm)
            b['last'] = tm if b['last'] is None else max(b['last'], tm)
            if kind == 'C':
                b['cancel_count'] += 1
                continue
            b['order_count'] += 1
            if side == 'B':
                b['order_buy_qty'] += qty
            elif side == 'S':
                b['order_sell_qty'] += qty
            continue
        if kind == 'C':
            flow['cancel_count'][i] += 1
            if side == 'B':
                flow['cancel_buy_qty'][i] += qty
            elif side == 'S':
                flow['cancel_sell_qty'][i] += qty
            else:
                flow['cancel_unknown_qty'][i] += qty
            continue
        flow['order_count'][i] += 1
        if side == 'B':
            flow['order_buy_qty'][i] += qty
        elif side == 'S':
            flow['order_sell_qty'][i] += qty
        else:
            flow['order_unknown_qty'][i] += qty

    max_age_ms = max_age_s * 1000.0
    qstat = {k: [None] * N_BARS for k in QUOTE_BAR_KEYS}
    close_state = [None] * N_BARS
    qi = 0
    carry = None
    for i, t in enumerate(grid):
        start = t - BAR_MS
        limit = t + (close_grace_ms if i == N_BARS - 1 else 0)
        while qi < len(quotes) and quotes[qi][0] <= start:
            carry = quotes[qi]
            qi += 1
        carry_in = carry
        snaps = []
        while qi < len(quotes) and quotes[qi][0] <= limit:
            snaps.append(quotes[qi])
            qi += 1
        pts = ([carry_in] if carry_in is not None else []) + snaps
        if snaps:
            carry = snaps[-1]
        close = pts[-1] if pts else None
        qstat['snap_count'][i] = len(snaps)
        qstat['quote_age_s'][i] = round((limit - close[0]) / 1000.0, 3) if close else None
        qstat['stale_quote'][i] = int(close is not None and (limit - close[0]) > max_age_ms)
        close_st = lob_state(close[1]) if close is not None else None
        close_state[i] = close_st if (close_st and close_st['valid_l1']) else None
        qstat['valid_l1'][i] = close_st['valid_l1'] if close_st else 0
        qstat['locked_count'][i] = sum(1 for _, a in snaps if a[17] > 0 and a[17] == a[37])
        qstat['crossed_count'][i] = sum(1 for _, a in snaps if a[17] > 0 and a[37] > a[17] > 0)

        wsum = {'mid': 0.0, 'spread': 0.0, 'wm': 0.0}
        covered = 0.0
        chain = []
        for k, (pt, arr) in enumerate(pts):
            st = lob_state(arr)
            nxt = pts[k + 1][0] if k + 1 < len(pts) else limit
            s0 = max(pt, start)
            s1 = min(min(nxt, pt + max_age_ms), limit)
            if s1 > s0 and st['valid_l1']:
                dur = (s1 - s0) / 1000.0
                covered += dur
                if st['mid'] is not None:
                    wsum['mid'] += st['mid'] * dur
                if st['spread'] is not None:
                    wsum['spread'] += st['spread'] * dur
                if st['weighted_mid'] is not None:
                    wsum['wm'] += st['weighted_mid'] * dur
            if st['valid_l1'] and st['mid'] is not None:
                chain.append((pt, st))
        covered_s = BAR_MS / 1000.0
        qstat['twap_coverage'][i] = round(min(covered / covered_s, 1.0), 4)
        if covered > 0:
            qstat['twap_mid'][i] = wsum['mid'] / covered
            qstat['twap_spread'][i] = wsum['spread'] / covered
            qstat['twap_wm'][i] = wsum['wm'] / covered
        qstat['mid_open'][i] = chain[0][1]['mid'] if chain else None
        cs = close_state[i]
        qstat['mid_close'][i] = cs['mid'] if cs else None
        if chain:
            mids = [st['mid'] for _, st in chain]
            qstat['mid_min'][i] = min(mids)
            qstat['mid_max'][i] = max(mids)
            qstat['mid_range'][i] = max(mids) - min(mids)
            qstat['mid_std'][i] = statistics.pstdev(mids) if len(mids) > 1 else 0.0
            pairs = [(mids[k], mids[k + 1]) for k in range(len(mids) - 1)
                     if mids[k] > 0 and mids[k + 1] > 0]
            qstat['realized_vol'][i] = math.sqrt(sum(math.log(b / a) ** 2 for a, b in pairs))
        if qstat['mid_open'][i] and qstat['mid_close'][i]:
            qstat['mid_ret'][i] = math.log(qstat['mid_close'][i] / qstat['mid_open'][i])

        ofi = 0.0
        n_ofi = 0
        for k in range(len(chain) - 1):
            s0 = chain[k][1]
            s1 = chain[k + 1][1]
            b0, b1 = s0['best_bid_p'], s1['best_bid_p']
            a0, a1 = s0['best_ask_p'], s1['best_ask_p']
            qb0, qb1 = s0['best_bid_q'], s1['best_bid_q']
            qa0, qa1 = s0['best_ask_q'], s1['best_ask_q']
            if min(b0, b1, a0, a1) <= 0:
                continue
            ofi += ((qb1 if b1 >= b0 else 0.0) - (qb0 if b1 <= b0 else 0.0)
                    - (qa1 if a1 <= a0 else 0.0) + (qa0 if a1 >= a0 else 0.0))
            n_ofi += 1
        qstat['ofi_raw'][i] = ofi if n_ofi else None
        qstat['ofi_count'][i] = n_ofi

    bar_only = {k: [None] * N_BARS for k in BAR_ONLY_KEYS}
    for s in (0, BARS_PER_SESSION):
        prev_t = None
        for i in range(s, s + BARS_PER_SESSION):
            times = trade_times[i]
            sizes = trade_sizes[i]
            if not times:
                continue
            gaps = []
            prev = prev_t
            for tt in times:
                if prev is not None:
                    gaps.append((tt - prev) / 1000.0)
                prev = tt
            prev_t = times[-1]
            if gaps:
                bar_only['iat_mean_s'][i] = sum(gaps) / len(gaps)
                bar_only['iat_median_s'][i] = statistics.median(gaps)
                bar_only['iat_p90_s'][i] = percentile(gaps, 0.90)
                bar_only['zero_gap_ratio'][i] = sum(1 for g in gaps if g == 0) / len(gaps)
            bar_only['avg_trade_size'][i] = sum(sizes) / len(sizes)
            bar_only['max_trade_size'][i] = max(sizes)
            med = statistics.median(sizes)
            if med > 0:
                bar_only['large_trade_ratio_x10med'][i] = sum(1 for x in sizes if x > 10 * med) / len(sizes)
    for i in range(N_BARS):
        qty = flow['trade_buy_qty'][i] + flow['trade_sell_qty'][i] + flow['trade_unknown_qty'][i]
        if qty > 0:
            bar_only['vwap'][i] = flow['trade_amount'][i] / qty

    roll = {}
    nb = {}
    for w in WINDOW_MIN:
        for k in RAW_FLOW_KEYS:
            roll[(k, w)], nb[(k, w)] = window_roll(flow[k], w)
        ofi_series = [0.0 if x is None else x for x in qstat['ofi_raw']]
        roll[('ofi_raw', w)] = window_roll(ofi_series, w)[0]

    rows = []
    for i, t in enumerate(grid):
        r = {'symbol': symbol, 'trading_date': date,
             'session': 'AM' if i < BARS_PER_SESSION else 'PM',
             'bar_index': i, 'bar_seq': (i % BARS_PER_SESSION) + 1,
             'bar_start': t_label(t - BAR_MS), 'bar_end': t_label(t)}
        cs = close_state[i]
        for k in STATE_KEYS:
            r[k] = cs[k] if cs else None
        for k in QUOTE_BAR_KEYS:
            r[k] = qstat[k][i]
        for w in WINDOW_MIN:
            n = nb[('trade_buy_qty', w)][i]
            for k in RAW_FLOW_KEYS:
                r[f'{k}_{w}m'] = roll[(k, w)][i]
            if w > 1:
                r[f'ofi_raw_{w}m'] = roll[('ofi_raw', w)][i]
            r[f'trade_imb_{w}m'] = ratio(r[f'trade_buy_qty_{w}m'], r[f'trade_sell_qty_{w}m'])
            r[f'order_imb_{w}m'] = ratio(r[f'order_buy_qty_{w}m'], r[f'order_sell_qty_{w}m'])
            r[f'cancel_imb_{w}m'] = ratio(r[f'cancel_buy_qty_{w}m'], r[f'cancel_sell_qty_{w}m'])
            r[f'trade_amount_per_min_{w}m'] = safe_div(r[f'trade_amount_{w}m'], n)
            r[f'trade_count_per_min_{w}m'] = safe_div(r[f'trade_count_{w}m'], n)
            r[f'order_count_per_min_{w}m'] = safe_div(r[f'order_count_{w}m'], n)
            r[f'ofi_per_min_{w}m'] = safe_div(r['ofi_raw'] if w == 1 else r[f'ofi_raw_{w}m'], n)
            r[f'trade_vwap_{w}m'] = safe_div(
                r[f'trade_amount_{w}m'],
                r[f'trade_buy_qty_{w}m'] + r[f'trade_sell_qty_{w}m'] + r[f'trade_unknown_qty_{w}m'])
            r[f'nb_bars_{w}m'] = n
            r[f'warmup_{w}m'] = int(n < w)
        for k in BAR_ONLY_KEYS:
            r[k] = bar_only[k][i]
        r['trade_amt_rate_ratio_1m_30m'] = safe_div(
            r['trade_amount_per_min_1m'], r['trade_amount_per_min_30m'])
        r['trade_cnt_rate_ratio_1m_30m'] = safe_div(
            r['trade_count_per_min_1m'], r['trade_count_per_min_30m'])
        rows.append(r)

    for i, r in enumerate(rows):
        for w in WINDOW_MIN:
            j = i + w
            same = j < N_BARS and (j // BARS_PER_SESSION) == (i // BARS_PER_SESSION)
            r[f'label_fwd_logret_{w}m'] = _fwd(r['mid_close'],
                                               rows[j]['mid_close'] if same else None)

    coarse = {}
    for w in COARSE_MIN:
        out = []
        for s in range(0, N_BARS, BARS_PER_SESSION):
            for k in range(0, BARS_PER_SESSION, w):
                chunk = rows[s + k: s + k + w]
                first, last = chunk[0], chunk[-1]
                c = {'symbol': symbol, 'trading_date': date, 'session': last['session'],
                     'bar_index': last['bar_index'],
                     'bar_seq': (last['bar_index'] % BARS_PER_SESSION) // w + 1,
                     'bar_start': first['bar_start'], 'bar_end': last['bar_end']}
                for key in STATE_KEYS:
                    c[key] = last[key]
                c['valid_l1'] = last['valid_l1']
                c['snap_count'] = sum(x['snap_count'] or 0 for x in chunk)
                c['quote_age_s'] = last['quote_age_s']
                for src, dst in (('twap_mid', 'twap_mid'), ('twap_spread', 'twap_spread')):
                    covs = [(x[src], x['twap_coverage']) for x in chunk
                            if x[src] is not None and x['twap_coverage']]
                    tot = sum(cv for _, cv in covs)
                    c[dst] = (sum(m * cv for m, cv in covs) / tot) if tot else None
                opens = [x['mid_open'] for x in chunk if x['mid_open'] is not None]
                mids = [x['mid_close'] for x in chunk if x['mid_close'] is not None]
                c['mid_open'] = opens[0] if opens else None
                c['mid_close'] = mids[-1] if mids else None
                c['mid_ret'] = _fwd(c['mid_open'], c['mid_close']) if c['mid_open'] else None
                lo = [x['mid_min'] for x in chunk if x['mid_min'] is not None]
                hi = [x['mid_max'] for x in chunk if x['mid_max'] is not None]
                c['mid_min'] = min(lo) if lo else None
                c['mid_max'] = max(hi) if hi else None
                c['mid_range'] = (c['mid_max'] - c['mid_min']) if (lo and hi) else None
                c['mid_std'] = statistics.pstdev(mids) if len(mids) > 1 else (0.0 if mids else None)
                rvs = [x['realized_vol'] for x in chunk if x['realized_vol'] is not None]
                c['realized_vol'] = math.sqrt(sum(v * v for v in rvs)) if rvs else None
                c['valid_l1_ratio'] = sum(x['valid_l1'] or 0 for x in chunk) / len(chunk)
                c['locked_count'] = sum(x['locked_count'] or 0 for x in chunk)
                c['crossed_count'] = sum(x['crossed_count'] or 0 for x in chunk)
                ofs = [x['ofi_raw'] for x in chunk if x['ofi_raw'] is not None]
                c['ofi_raw'] = sum(ofs) if ofs else None
                for key in RAW_FLOW_KEYS:
                    c[key] = sum(x[f'{key}_1m'] or 0 for x in chunk)
                c['trade_imb'] = ratio(c['trade_buy_qty'], c['trade_sell_qty'])
                c['order_imb'] = ratio(c['order_buy_qty'], c['order_sell_qty'])
                c['cancel_imb'] = ratio(c['cancel_buy_qty'], c['cancel_sell_qty'])
                c['trade_amount_per_min'] = safe_div(c['trade_amount'], w)
                c['trade_count_per_min'] = safe_div(c['trade_count'], w)
                c['order_count_per_min'] = safe_div(c['order_count'], w)
                c['ofi_per_min'] = safe_div(c['ofi_raw'], w)
                c['trade_vwap'] = safe_div(
                    c['trade_amount'],
                    c['trade_buy_qty'] + c['trade_sell_qty'] + c['trade_unknown_qty'])
                out.append(c)
        for j, c in enumerate(out):
            nxt = out[j + 1] if j + 1 < len(out) and out[j + 1]['session'] == c['session'] else None
            c[f'label_fwd_logret_{w}m'] = _fwd(c['mid_close'], nxt['mid_close'] if nxt else None)
        coarse[w] = out

    by30 = {(c['session'], c['bar_seq']): c for c in coarse[30]}
    for c in coarse[5]:
        nxt = by30.get((c['session'], (c['bar_index'] % BARS_PER_SESSION) // 30 + 1))
        c['label_fwd_logret_30m'] = _fwd(c['mid_close'], nxt['mid_close'] if nxt else None)

    auction_rows = []
    for ph in ('pre_open', 'lunch', 'post_close'):
        b = auctions.get(ph)
        if not b:
            continue
        r = {k: b[k] for k in AUCTION_HEADER if k in b}
        r['symbol'] = symbol
        r['trading_date'] = date
        r['phase'] = ph
        r['first_time'] = t_label(b['first']) if b['first'] is not None else None
        r['last_time'] = t_label(b['last']) if b['last'] is not None else None
        r['trade_amount'] = round(b['trade_amount'], 2)
        auction_rows.append(r)
    auction_amount = sum(b['trade_amount'] for b in auctions.values())
    bar_amount = sum(flow['trade_amount'])
    last_q = quotes[-1][1] if quotes else []
    summary = {
        'symbol': symbol,
        'trading_date': date,
        'quote_rows': len(quotes),
        'bars': N_BARS,
        'bars_with_quote': sum(1 for x in qstat['mid_close'] if x is not None),
        'bars_valid_l1': sum(1 for x in qstat['valid_l1'] if x),
        'bars_with_trade': sum(1 for i in range(N_BARS) if trade_times[i]),
        'trade_rows': tstat['trades'],
        'cancel_rows': tstat['cancels'],
        'trade_buy_qty': tstat['buy_qty'],
        'trade_sell_qty': tstat['sell_qty'],
        'trade_unknown_qty': tstat['unknown_qty'],
        'trade_amount': round(tstat['amount'], 2),
        'sum_bar_trade_amount': round(bar_amount, 2),
        'auction_trade_amount': round(auction_amount, 2),
        'bar_plus_auction_trade_amount': round(bar_amount + auction_amount, 2),
        'order_file_rows': ostat['rows'],
        'new_order_rows': ostat['new'],
        'cancel_order_rows': ostat['cancel'],
        'order_buy_qty': ostat['buy_qty'],
        'order_sell_qty': ostat['sell_qty'],
        'cancel_order_qty': ostat['cancel_qty'],
        'quote_rows_bad_time': quote_bad_time,
        'trade_rows_bad_time': tstat['bad_time'],
        'trade_rows_no_price': tstat['no_price'],
        'order_rows_bad_time': ostat['bad_time'],
    }
    return {'symbol': symbol, 'rows1m': rows, 'coarse': coarse, 'summary': summary,
            'auction_rows': auction_rows,
            'tstat': tstat, 'quote_last_cum_amount': num(last_q[12]) if last_q else None,
            'quote_last_cum_count': num(last_q[7]) if last_q else None}


def _fwd(c0, c1):
    if c0 and c1 and c0 > 0 and c1 > 0:
        return math.log(c1 / c0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--archive')
    ap.add_argument('--raw-dir', help='already extracted day root; skips 7z extraction')
    ap.add_argument('--out', required=True)
    ap.add_argument('--date', required=True)
    ap.add_argument('--symbols', nargs='*')
    ap.add_argument('--max-quote-age', type=float, default=6.0)
    ap.add_argument('--close-grace-ms', type=int, default=1000,
                    help='closing prints inside this window after 15:00:00 attach to the last PM bar')
    ap.add_argument('--truncate-ms', type=int, default=None,
                    help='drop raw records after this millisecond of day (causality test)')
    ap.add_argument('--keep-raw', action='store_true')
    args = ap.parse_args()
    if not args.archive and not args.raw_dir:
        ap.error('one of --archive or --raw-dir is required')

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    raw = None
    cleanup = False
    if args.raw_dir:
        day = Path(args.raw_dir) / args.date
    else:
        import py7zr
        raw = out / 'raw_extract'
        shutil.rmtree(raw, ignore_errors=True)
        raw.mkdir(parents=True)
        cleanup = not args.keep_raw
        print('extracting', args.archive, flush=True)
        with py7zr.SevenZipFile(args.archive) as z:
            if args.symbols:
                targets = [x.filename for x in z.list()
                           if any(f'/{s}/' in x.filename for s in args.symbols) and not x.is_directory]
                z.extract(targets=targets, path=raw)
            else:
                z.extractall(path=raw)
        day = raw / args.date

    symbols = sorted([x for x in day.iterdir() if x.is_dir()])
    if args.symbols:
        symbols = [x for x in symbols if x.name in set(args.symbols)]

    h1 = header_1m()
    hc = {w: header_coarse(w) for w in COARSE_MIN}
    p1 = out / f'level2_{args.date}_1m_features.csv.gz'
    p5 = out / f'level2_{args.date}_5m_features.csv.gz'
    p30 = out / f'level2_{args.date}_30m_features.csv.gz'
    summary_path = out / f'level2_{args.date}_daily_summary.csv'
    dict_path = out / f'level2_{args.date}_feature_dictionary_1m.csv'
    auction_path = out / f'level2_{args.date}_auction_features.csv'
    if args.truncate_ms is not None:
        p1 = out / f'level2_{args.date}_1m_features_trunc{args.truncate_ms}.csv.gz'
        p5 = out / f'level2_{args.date}_5m_features_trunc{args.truncate_ms}.csv.gz'
        p30 = out / f'level2_{args.date}_30m_features_trunc{args.truncate_ms}.csv.gz'
        summary_path = out / f'level2_{args.date}_daily_summary_trunc{args.truncate_ms}.csv'
        auction_path = out / f'level2_{args.date}_auction_features_trunc{args.truncate_ms}.csv'

    done = 0
    errors = []
    checks = []
    with gzip.open(p1, 'wt', encoding='utf-8', newline='') as g1, \
            gzip.open(p5, 'wt', encoding='utf-8', newline='') as g5, \
            gzip.open(p30, 'wt', encoding='utf-8', newline='') as g30, \
            open(auction_path, 'w', encoding='utf-8', newline='') as af, \
            open(summary_path, 'w', encoding='utf-8', newline='') as sf:
        w1, w5, w30, sw = csv.writer(g1), csv.writer(g5), csv.writer(g30), csv.writer(sf)
        wa = csv.writer(af)
        w1.writerow(h1)
        w5.writerow(hc[5])
        w30.writerow(hc[30])
        wa.writerow(AUCTION_HEADER)
        sw.writerow(SUMMARY_HEADER)
        for d in symbols:
            try:
                res = process_symbol(d, args.date, args.max_quote_age, args.truncate_ms,
                                     args.close_grace_ms)
                if res is None:
                    continue
                for r in res['rows1m']:
                    w1.writerow([fmt(r.get(k)) for k in h1])
                for w, wr in ((5, w5), (30, w30)):
                    for r in res['coarse'][w]:
                        wr.writerow([fmt(r.get(k)) for k in hc[w]])
                for r in res.get('auction_rows', []):
                    wa.writerow([fmt(r.get(k)) for k in AUCTION_HEADER])
                sw.writerow([fmt(res['summary'].get(k)) for k in SUMMARY_HEADER])
                done += 1
                cum_amt = res['quote_last_cum_amount']
                checks.append({
                    'symbol': res['symbol'],
                    'trade_amount': round(res['tstat']['amount'], 2),
                    'quote_last_cum_amount': cum_amt,
                    'amount_minus_quote_cum': (round(res['tstat']['amount'] - cum_amt, 2)
                                               if cum_amt is not None else None),
                    'trade_rows': res['tstat']['trades'],
                    'quote_last_cum_count': res['quote_last_cum_count'],
                    'bars_plus_auction_minus_file_amount': round(
                        res['summary']['bar_plus_auction_trade_amount'] - res['tstat']['amount'], 6),
                })
                if done % 100 == 0:
                    print('processed', done, '/', len(symbols),
                          'elapsed_s', round(time.time() - t0), flush=True)
            except Exception as e:
                errors.append({'symbol': d.name, 'error': repr(e)})
                print('ERROR', d.name, repr(e), flush=True)

    if args.truncate_ms is None:
        with open(dict_path, 'w', encoding='utf-8', newline='') as f:
            dw = csv.writer(f)
            dw.writerow(['name', 'definition', 'unit', 'known_at'])
            dw.writerows(feature_dictionary(h1))

    meta = {
        'date': args.date,
        'archive': args.archive,
        'raw_dir': args.raw_dir,
        'symbols_requested': len(symbols),
        'symbols_processed': done,
        'errors': errors,
        'min_unit': '1 minute',
        'bar_ms': BAR_MS,
        'bars_per_session': BARS_PER_SESSION,
        'bar_convention': 'bar labelled t covers (t-1min, t]; close state is backward as-of',
        'sessions': [[t_label(a), t_label(b)] for a, b in SESSIONS],
        'rolling_windows_min': list(WINDOW_MIN),
        'coarse_tables_min': list(COARSE_MIN),
        'max_quote_age_s': args.max_quote_age,
        'close_grace_ms': args.close_grace_ms,
        'truncate_ms': args.truncate_ms,
        'price_scale': 'integer / 10000',
        'feature_file_1m': str(p1),
        'feature_file_5m': str(p5),
        'feature_file_30m': str(p30),
        'summary_file': str(summary_path),
        'auction_file': str(auction_path),
        'columns_1m': len(h1),
        'columns_5m': len(hc[5]),
        'columns_30m': len(hc[30]),
        'reconciliation': checks,
        'seconds': round(time.time() - t0, 1),
        'raw_deleted': cleanup,
    }
    (out / f'level2_{args.date}_metadata.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    if cleanup and raw is not None:
        shutil.rmtree(raw, ignore_errors=True)
    print(json.dumps(meta, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Definitive row counts for one raw Level2 file."""
import csv
import sys
from collections import Counter


def t_ms(s):
    s = (s or '').strip().zfill(9)
    if len(s) != 9 or not s.isdigit():
        return None
    return ((int(s[:2]) * 60 + int(s[2:4])) * 60 + int(s[4:6])) * 1000 + int(s[6:9])


def num(s):
    try:
        return int((s or '').strip() or 0)
    except ValueError:
        return 0


def main():
    path = sys.argv[1]
    c = Counter()
    lens = Counter()
    with open(path, encoding='gb18030', errors='replace', newline='') as f:
        r = csv.reader(f)
        next(r, None)
        for x in r:
            c['lines'] += 1
            lens[len(x)] += 1
            if len(x) < 12:
                c['short'] += 1
                continue
            c['len_ok'] += 1
            t = t_ms(x[3])
            if t is None:
                c['bad_time'] += 1
                continue
            code = (x[5] or '').strip()
            if code == 'C':
                c['cancel'] += 1
                continue
            if num(x[8]) <= 0 or num(x[9]) <= 0:
                c['no_price'] += 1
                continue
            c['trade'] += 1
    print(path)
    print(dict(c))
    print('column length histogram', dict(sorted(lens.items())[:8]))


if __name__ == '__main__':
    main()

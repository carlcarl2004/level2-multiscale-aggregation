#!/usr/bin/env python3
"""Compare a full run against a truncated run bar by bar.

Every feature column of every row that closes at or before the truncation
point must be identical.  Forward-looking label columns are excluded from the
strict comparison and reported separately, because a label that spans the
truncation point is expected to change.

Usage: python verify_causality.py full_1m.csv.gz trunc_1m.csv.gz 10:00:00.000
"""
import csv
import gzip
import sys


def load(path):
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as f:
        r = csv.reader(f)
        header = next(r)
        return header, [dict(zip(header, x)) for x in r]


def main():
    full_path, trunc_path, cutoff = sys.argv[1], sys.argv[2], sys.argv[3]
    header, full = load(full_path)
    header2, trunc = load(trunc_path)
    if header != header2:
        raise SystemExit('header mismatch')
    features = [c for c in header if not c.startswith('label_')]
    labels = [c for c in header if c.startswith('label_')]
    keep = [x for x in full if x['bar_end'] <= cutoff]
    other = [x for x in trunc if x['bar_end'] <= cutoff]
    if len(keep) != len(other):
        raise SystemExit(f'row count mismatch {len(keep)} vs {len(other)}')
    diffs = []
    label_diffs = 0
    for a, b in zip(keep, other):
        for col in features:
            if a[col] != b[col]:
                diffs.append((a['symbol'], a['bar_end'], col, a[col], b[col]))
        for col in labels:
            if a[col] != b[col]:
                label_diffs += 1
    print(f'compared_rows={len(keep)} cutoff={cutoff} feature_columns={len(features)} '
          f'feature_mismatches={len(diffs)} label_cells_changed={label_diffs} '
          f'(label columns: {",".join(labels)})')
    for d in diffs[:20]:
        print(d)
    raise SystemExit(1 if diffs else 0)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Aggregate one Level2 trading day into a compact 3-second feature table.

The raw vendor files remain the source of truth.  This program extracts one
daily 7z archive to a temporary directory, parses GB18030 CSV files by the
documented column positions, writes a gzipped feature table and a daily audit
summary, then removes only its temporary extraction directory.
"""
import argparse, csv, gzip, json, os, shutil, sys, time
from pathlib import Path

try:
    import py7zr
except ImportError as e:
    raise SystemExit("py7zr is required; install it in the server environment") from e

WINDOWS = (3, 15, 60, 300)
SESSION_RANGES = ((9*3600+30*60, 11*3600+30*60), (13*3600, 15*3600))
MS_DAY = 86400000

def clean(x):
    if x is None: return ''
    return str(x).replace('\x00', '').strip()

def num(x, default=0):
    try: return int(clean(x)) if clean(x) else default
    except ValueError: return default

def t_ms(x):
    s=clean(x).zfill(9)
    return ((num(s[:2])*60+num(s[2:4]))*60+num(s[4:6]))*1000+num(s[6:9])

def t_label(ms):
    sec, milli = divmod(ms,1000); h, rem = divmod(sec,3600); m, s = divmod(rem,60)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"

def grid_times():
    out=[]
    for a,b in SESSION_RANGES:
        out.extend(range(a*1000,b*1000+1,3000))
    return out

def rows(path):
    with open(path, 'r', encoding='gb18030', errors='replace', newline='') as f:
        r=csv.reader(f); next(r, None)
        for x in r:
            if x and len(x)>=4: yield x

def parse_quote(path):
    out=[]
    for x in rows(path):
        if len(x)<61: continue
        a=[num(v) for v in x]
        out.append((t_ms(x[3]), a))
    out.sort(key=lambda z:z[0]); return out

def parse_trade(path, exchange):
    out=[]; n=cancel=unknown=0; buy=sell=unk=amt=0
    for x in rows(path):
        if len(x)<12: continue
        tm=t_ms(x[3]); code=clean(x[5]); side=clean(x[7]).upper(); px=num(x[8]); qty=num(x[9])
        bid_ref=num(x[11]); ask_ref=num(x[10]); is_cancel=(exchange=='SZ' and code=='C')
        if is_cancel:
            cancel += 1
            cside='B' if bid_ref>0 and ask_ref==0 else ('S' if ask_ref>0 and bid_ref==0 else '')
            out.append((tm,'C',cside,0,qty,0.0)); continue
        if px<=0 or qty<=0: continue
        n += 1; value=px*qty/10000.0
        if side=='B': buy+=qty
        elif side=='S': sell+=qty
        else: unknown+=qty
        amt += value; unknown += 0
        out.append((tm,'T',side,px,qty,value))
    out.sort(key=lambda z:z[0])
    return out, {'rows':n+cancel,'trades':n,'cancels':cancel,'buy_qty':buy,'sell_qty':sell,'unknown_qty':unknown,'amount':amt}

def parse_orders(path, exchange):
    out=[]; stats={'rows':0,'new':0,'cancel':0,'buy_qty':0,'sell_qty':0,'cancel_qty':0}
    for x in rows(path):
        if len(x)<10: continue
        tm=t_ms(x[3]); typ=clean(x[6]).upper(); side=clean(x[7]).upper(); qty=num(x[9]); stats['rows']+=1
        is_cancel=(exchange=='SH' and typ=='D')
        if is_cancel: stats['cancel']+=1; stats['cancel_qty']+=qty; kind='C'
        else:
            stats['new']+=1
            if side=='B': stats['buy_qty']+=qty
            elif side=='S': stats['sell_qty']+=qty
            kind='N'
        out.append((tm,kind,side,qty))
    out.sort(key=lambda z:z[0]); return out,stats

def rolling(events, grids, session_start, session_end, kind):
    """Return per-grid rolling sums for one event stream, reset by session."""
    times=[e[0] for e in events]; out=[]; lo=[0]*len(WINDOWS); hi=0
    # Metrics: buy, sell, unknown, amount, count, cancel_buy, cancel_sell, cancel_count.
    sums=[[0.0]*8 for _ in WINDOWS]
    start=0
    while start<len(events) and times[start]<session_start: start+=1
    end=start
    while end<len(events) and times[end]<=session_end: end+=1
    lo=[start]*len(WINDOWS); hi=start; sums=[[0.0]*8 for _ in WINDOWS]
    for t in grids:
        if t<session_start or t>session_end: continue
        while hi<end and times[hi]<=t:
            e=events[hi]
            if kind=='trade':
                _,typ,side,px,qty,val=e; v=[0.0]*8
                if typ=='C': v[7]=1; v[5 if side=='B' else 6 if side=='S' else 7]=qty
                elif side=='B': v[0]=qty; v[3]=val; v[4]=1
                elif side=='S': v[1]=qty; v[3]=val; v[4]=1
                else: v[2]=qty; v[3]=val; v[4]=1
            else:
                _,typ,side,qty=e; v=[0.0]*8
                if typ=='C': v[7]=qty
                elif side=='B': v[0]=qty; v[4]=1
                elif side=='S': v[1]=qty; v[4]=1
                else: v[2]=qty; v[4]=1
            for j in range(len(WINDOWS)):
                for k in range(8): sums[j][k]+=v[k]
            hi+=1
        vals=[]
        for j,w in enumerate(WINDOWS):
            cutoff=t-w*1000
            while lo[j]<hi and times[lo[j]]<=cutoff:
                e=events[lo[j]]
                if kind=='trade':
                    _,typ,side,px,qty,val=e; v=[0.0]*8
                    if typ=='C': v[7]=1; v[5 if side=='B' else 6 if side=='S' else 7]=qty
                    elif side=='B': v[0]=qty; v[3]=val; v[4]=1
                    elif side=='S': v[1]=qty; v[3]=val; v[4]=1
                    else: v[2]=qty; v[3]=val; v[4]=1
                else:
                    _,typ,side,qty=e; v=[0.0]*8
                    if typ=='C': v[7]=qty
                    elif side=='B': v[0]=qty; v[4]=1
                    elif side=='S': v[1]=qty; v[4]=1
                    else: v[2]=qty; v[4]=1
                for k in range(8): sums[j][k]-=v[k]
                lo[j]+=1
            vals.extend(sums[j])
        out.append(vals)
    return out

def feature_header():
    h=['symbol','trading_date','grid_time','quote_age_s','valid_l1','mid','spread','weighted_mid','weighted_mid_minus_mid']
    h += [f'depth_bid_{k}' for k in (1,3,5,10)] + [f'depth_ask_{k}' for k in (1,3,5,10)]
    h += [f'di_{k}' for k in (1,3,5,10)]
    for pref in ('trade','order'):
        for w in WINDOWS:
            h += [f'{pref}_buy_qty_{w}s',f'{pref}_sell_qty_{w}s',f'{pref}_unknown_qty_{w}s',f'{pref}_amount_{w}s',f'{pref}_count_{w}s',f'{pref}_cancel_buy_{w}s',f'{pref}_cancel_sell_{w}s',f'{pref}_cancel_count_{w}s']
    return h

def process_symbol(sym_dir, date, writer, summary_writer, pilot=False):
    symbol=sym_dir.name; ex=symbol.split('.')[-1]
    qf=sym_dir/'行情.csv'; of=sym_dir/'逐笔委托.csv'; tf=sym_dir/'逐笔成交.csv'
    if not (qf.exists() and tf.exists()): return False
    quotes=parse_quote(qf); trades,tstat=parse_trade(tf,ex); orders,ostat=parse_orders(of,ex) if of.exists() else ([],{'rows':0,'new':0,'cancel':0,'buy_qty':0,'sell_qty':0,'cancel_qty':0})
    grids=grid_times(); trade_roll=[]; order_roll=[]
    for a,b in SESSION_RANGES:
        gs=[x for x in grids if a*1000<=x<=b*1000]
        trade_roll.extend(rolling(trades,gs,a*1000,b*1000,'trade'))
        order_roll.extend(rolling(orders,gs,a*1000,b*1000,'order'))
    qi=0; last=None; valid=0; last_quote=None; qrows=[]
    for t in grids:
        while qi<len(quotes) and quotes[qi][0]<=t: last_quote=quotes[qi]; qi+=1
        qrows.append((t,last_quote))
    for idx,(t,qr) in enumerate(qrows):
        age=''; vals=['']*16
        valid_l1=0
        if qr:
            qt,a=qr; age=round((t-qt)/1000.0,3); ap=[a[17+i] for i in range(10)]; aq=[a[27+i] for i in range(10)]; bp=[a[37+i] for i in range(10)]; bq=[a[47+i] for i in range(10)]
            valid_l1=int(ap[0]>0 and bp[0]>0); valid+=valid_l1
            if valid_l1:
                mid=(ap[0]+bp[0])/20000; spread=(ap[0]-bp[0])/10000; den=aq[0]+bq[0]; wm=((ap[0]*bq[0]+bp[0]*aq[0])/den/10000) if den else ''
                vals=[mid,spread,wm,wm-mid if wm!='' else '']
                db=[sum(bq[:k]) for k in (1,3,5,10)]; da=[sum(aq[:k]) for k in (1,3,5,10)]; di=[(x-y)/(x+y) if x+y else '' for x,y in zip(db,da)]; vals += db+da+di
        if qr is None: vals=['']*16
        row=[symbol,date,t_label(t),age,valid_l1]+vals
        row += trade_roll[idx]+order_roll[idx]
        writer.writerow(row)
    last_a=quotes[-1][1] if quotes else []
    summ=[symbol,date,len(quotes),valid,len(trades),tstat['trades'],tstat['cancels'],tstat['buy_qty'],tstat['sell_qty'],tstat['unknown_qty'],round(tstat['amount'],2),len(orders),ostat['new'],ostat['cancel'],ostat['buy_qty'],ostat['sell_qty'],ostat['cancel_qty']]
    if last_a: summ += [num(last_a[11]),num(last_a[12]),num(last_a[7])]
    else: summ += ['','','']
    summary_writer.writerow(summ); return True

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--archive',required=True); ap.add_argument('--out',required=True); ap.add_argument('--date',required=True); ap.add_argument('--symbols',nargs='*'); ap.add_argument('--keep-raw',action='store_true'); args=ap.parse_args()
    out=Path(args.out); raw=out/'raw_extract'; out.mkdir(parents=True,exist_ok=True); shutil.rmtree(raw,ignore_errors=True); raw.mkdir()
    t0=time.time(); print('extracting',args.archive,flush=True)
    with py7zr.SevenZipFile(args.archive) as z:
        if args.symbols:
            targets=[x.filename for x in z.list() if any(f'/{s}/' in x.filename for s in args.symbols) and not x.is_directory]
            z.extract(targets=targets,path=raw)
        else:
            z.extractall(path=raw)
    day=raw/args.date; symbols=sorted([x for x in day.iterdir() if x.is_dir()])
    if args.symbols: symbols=[x for x in symbols if x.name in set(args.symbols)]
    feature_path=out/f'level2_{args.date}_3s_features.csv.gz'; summary_path=out/f'level2_{args.date}_daily_summary.csv'
    with gzip.open(feature_path,'wt',encoding='utf-8',newline='') as gf, open(summary_path,'w',encoding='utf-8',newline='') as sf:
        w=csv.writer(gf); sw=csv.writer(sf); w.writerow(feature_header()); sw.writerow(['symbol','trading_date','quote_rows','valid_l1_grid_rows','trade_file_rows','trade_rows','cancel_rows','trade_buy_qty','trade_sell_qty','trade_unknown_qty','trade_amount','order_file_rows','new_order_rows','cancel_order_rows','order_buy_qty','order_sell_qty','cancel_order_qty','last_cum_volume','last_cum_amount','last_cum_count'])
        done=0; errors=[]
        for d in symbols:
            try:
                if process_symbol(d,args.date,w,sw): done+=1
                if done%100==0: print('processed',done,'/',len(symbols), 'elapsed_s',round(time.time()-t0),flush=True)
            except Exception as e:
                errors.append({'symbol':d.name,'error':repr(e)}); print('ERROR',d.name,repr(e),flush=True)
    meta={'date':args.date,'archive':args.archive,'symbols_requested':len(symbols),'symbols_processed':done,'errors':errors,'feature_file':str(feature_path),'summary_file':str(summary_path),'seconds':round(time.time()-t0,1),'windows_s':WINDOWS,'grid':'3-second backward-asof; sessions 09:30-11:30 and 13:00-15:00','price_scale':'integer / 10000','raw_deleted':not args.keep_raw}
    (out/f'level2_{args.date}_metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    if not args.keep_raw: shutil.rmtree(raw,ignore_errors=True)
    print(json.dumps(meta,ensure_ascii=False),flush=True)
if __name__=='__main__': main()

"""Run: python audit.py --input ../data --output . ; dependencies pandas, numpy."""
import argparse,json,hashlib,re
from pathlib import Path
import pandas as pd
import numpy as np
p=argparse.ArgumentParser();p.add_argument('--input',default='../data');p.add_argument('--output',default='.');args=p.parse_args()
out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
D={}; rows=[]; stats=[]
for f in sorted(Path(args.input).glob('*.csv')):
 d=pd.read_csv(f,encoding='gb18030'); D[f.name]=d
 t=d['时间'];sec=t//10000000*3600+(t//100000%100)*60+t//1000%100+t%1000/1000
 mask=((sec>=34200)&(sec<41400))|((sec>=46800)&(sec<53820)); ds=sec[mask].diff(); ds=ds[(ds>0)&(ds<600)]
 s=dict(file=f.name,sha256=hashlib.sha256(f.read_bytes()).hexdigest(),rows=len(d),columns=len(d.columns),symbols=d['万得代码'].unique().tolist(),dates=d['自然日'].unique().tolist(),duplicates=int(d.duplicated().sum()),backward=int((sec.diff()<0).sum()),same_timestamp=int((sec.diff()==0).sum()),time_min=int(t.min()),time_max=int(t.max()),continuous_positive_gap_top=ds.round(3).value_counts().head(12).to_dict())
 for c in ['BS标志','成交代码','委托类型','委托代码']:
  if c in d:s[c]={str(k):int(v) for k,v in d[c].value_counts(dropna=False).items()}
 if '申卖价1' in d:
  valid=(d['申买价1']>0)&(d['申卖价1']>0)
  s['continuous_valid_l1']=int((valid&mask).sum());s['continuous_locked']=int((valid&mask&(d['申卖价1']==d['申买价1'])).sum());s['continuous_crossed']=int((valid&mask&(d['申卖价1']<d['申买价1'])).sum())
  s['final_totals']=d[['当日累计成交量','当日成交额','成交笔数']].iloc[-1].to_dict()
  s['cumulative_count_monotonic']=bool(d['成交笔数'].is_monotonic_increasing)
  s['increment_volume_equals_cumulative_difference']=bool(np.array_equal(d['成交量'].to_numpy(),d['当日累计成交量'].diff().fillna(d['当日累计成交量'].iloc[0]).to_numpy()))
 stats.append(s)
 for c in d:
  unit='待供应商确认';meaning=c;use='条件可用';note='按原始字段保留；语义未经供应商文档确认'
  if c=='万得代码':unit='字符串';meaning='证券代码及市场后缀';use='分组键';note='不可作为连续数值'
  elif c=='交易所代码':unit='证券数字代码';meaning='不带前导零的证券代码';use='标识';note='本样本不是交易所类别码；以万得代码后缀分市场'
  elif c=='自然日':unit='YYYYMMDD';use='分组键';note='Asia/Shanghai'
  elif c=='时间':unit='HHMMSSmmm编码整数';use='时间键';note='左补零到9位；不能直接整数相减；不等于到达时间'
  elif any(x in c for x in ['价格','申卖价','申买价','平均叫','成交价','最高价','最低价','开盘价','前收盘']):unit='推定1/10000元';note='逐笔价×量/10000与快照日成交额匹配，仍需供应商确认'
  elif any(x in c for x in ['数量','申卖量','申买量','叫卖总量','叫买总量','成交量']):unit='推定股';note='跨文件成交量一致；股/手仍需供应商定义确认'
  elif c in ['成交额','当日成交额']:unit='推定元';note='行情成交额为区间增量；当日成交额为累计；整数金额有舍入'
  elif c=='成交笔数':unit='笔（累计）';note='与逐笔真实成交记录总数相等；用差分而非求和'
  elif c in ['成交编号','委托编号','交易所委托号','叫卖序号','叫买序号']:unit='ID整数/字符串';use='关联键';note='不要作为连续特征；缺channel字段，不能假定跨通道全局顺序'
  elif c=='BS标志':unit='B/S/空';note='成交记录有B/S，主动方定义待文档确认；撤单空白'
  elif c=='成交代码':unit='0/C/空';note='SZ C为撤单候选且价格0；SH空值不能套SZ映射'
  elif c=='委托代码':unit='B/S或0';note='委托文件B/S；成交文件0或空，同名不同义'
  elif c=='委托类型':unit='0/1';note='不可猜测对应限价/市价；保留原码，等待Wind字典'
  if d[c].isna().all():use='不可用';note='全列缺失'
  elif d[c].nunique(dropna=True)==1:use='无横截面/时间变化';note+='；本样本常数'
  rows.append(dict(file=f.name,field=c,dtype=str(d[c].dtype),unit=unit,meaning=meaning,exchange=d['万得代码'].iloc[0].split('.')[-1],feature_use=use,notes=note,missing_rate=float(d[c].isna().mean()),unique=int(d[c].nunique()),example=str(d[c].dropna().iloc[0]) if d[c].notna().any() else ''))
# SZ references: empirical linkage, not proof of global reconstruction.
a=D['逐笔委托(1).csv']; e=D['逐笔成交(2).csv'];ids=set(a['交易所委托号']);tr=e[e['成交代码']=='0'];ca=e[e['成交代码']=='C']
link={'order_id_unique':bool(a['交易所委托号'].is_unique),'cancel_exactly_one_nonzero_ref':int(((ca['叫买序号']>0)^(ca['叫卖序号']>0)).sum()),'cancel_rows':len(ca),'trade_bid_ref_match':float(tr['叫买序号'].isin(ids).mean()),'trade_ask_ref_match':float(tr['叫卖序号'].isin(ids).mean()),'cancel_ref_match':float(ca['叫买序号'].where(ca['叫买序号']>0,ca['叫卖序号']).isin(ids).mean())}
# Order quantity conservation using all executions and cancellation quantities.
used={}
for _,r in e.iterrows():
 for c in ['叫买序号','叫卖序号']:
  if r[c]>0:used[r[c]]=used.get(r[c],0)+int(r['成交数量'])
remaining=a['委托数量']-a['交易所委托号'].map(used).fillna(0)
link['orders_negative_remaining']=int((remaining<0).sum());link['orders_positive_remaining']=int((remaining>0).sum())
pd.DataFrame(rows).to_csv(out/'data_dictionary.csv',index=False,encoding='utf-8-sig')
(out/'audit_results.json').write_text(json.dumps({'files':stats,'sz_linkage':link},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(link,ensure_ascii=False));print('Dictionary rows:',len(rows))

import py7zr,json,collections,pathlib,sys,importlib.util
root=pathlib.Path('/aidata/wentao/market_data')
packs=sorted(root.glob('2025*/*.7z'))
print('packages',len(packs),'bytes',sum(x.stat().st_size for x in packs))
print('months',dict(collections.Counter(x.parent.name for x in packs)))
print('modules',{x:bool(importlib.util.find_spec(x)) for x in ['pandas','numpy','pyarrow','polars','py7zr']})
with py7zr.SevenZipFile(packs[0]) as z:
 info=z.archiveinfo(); print('archive_info',info)
 files=[x for x in z.list() if not x.is_directory]
 print('names',json.dumps([x.filename for x in files[:6]],ensure_ascii=False))
 for s in ['000001.SZ','600000.SH']:
  xs=[x for x in files if s in x.filename]
  print(s,[(x.filename,x.uncompressed) for x in xs])
dest=pathlib.Path('/aidata/wentao/level2_agg_2025');dest.mkdir(exist_ok=True)
(dest/'input_inventory.json').write_text(json.dumps([{'path':str(x),'bytes':x.stat().st_size,'date':x.stem} for x in packs],indent=2))

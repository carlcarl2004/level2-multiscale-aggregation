import argparse, json, os, sys, paramiko
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--code'); p.add_argument('--cmd'); p.add_argument('--upload',nargs=2); p.add_argument('--download',nargs=2); p.add_argument('--timeout',type=int,default=120); a=p.parse_args()
c=paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('10.96.186.57',username='wentao',password=os.environ['LVL2_PW'],timeout=20)
try:
 if a.upload:
  s=c.open_sftp(); s.put(*a.upload); s.close()
 if a.download:
  s=c.open_sftp(); s.get(*a.download); s.close()
 if a.code or a.cmd:
  cmd=a.cmd or 'python -'
  i,o,e=c.exec_command(cmd,timeout=a.timeout)
  if a.code: i.write(Path(a.code).read_text(encoding='utf-8')); i.channel.shutdown_write()
  print(o.read().decode('utf-8','replace')); print(e.read().decode('utf-8','replace'),file=sys.stderr)
  sys.exit(o.channel.recv_exit_status())
finally: c.close()

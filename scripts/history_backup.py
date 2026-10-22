"""Root-only checkpoint used by the portal-only history upgrade."""
import argparse
import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path
from upgrade_monitoring import api, sql_state, secret_state

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('name');args=parser.parse_args()
    if '/' in args.name or not args.name.startswith('history-'):
        raise ValueError('Use a history-* backup name')
    out=ROOT/'.remote-dev/backups'/args.name
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    containers={x['Names'][0].lstrip('/'):api('GET','/containers/'+x['Id']+'/json')
                for x in api('GET','/containers/json?all=true')}
    state={'containers':containers,'accounts':sql_state(),'secrets':secret_state(),
           'sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (ROOT/'app').rglob('*') if p.is_file() and '__pycache__' not in str(p)}}
    target=out/'state.json';target.write_text(json.dumps(state));os.chmod(target,0o600)
    with (out/'platform.dump').open('wb') as stream:
        subprocess.run(['docker','exec','drp-database','pg_dump','-U','platform','-d','platform','-Fc'],stdout=stream,check=True)
    os.chmod(out/'platform.dump',0o600)
    with tarfile.open(out/'sources.tar.gz','w:gz') as archive:
        archive.add(ROOT/'app',arcname='app')
    print(json.dumps({'backup':str(out),'dump_bytes':(out/'platform.dump').stat().st_size,
                      'running_containers':sum(c['State']['Running'] for c in containers.values())}),flush=True)


if __name__=='__main__':main()

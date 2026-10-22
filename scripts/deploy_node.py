"""Run on an authorized compute host via ssh-skill. Requires the SAME mounted JuiceFS.
Enrolls a TLS connection; the administrator then registers it on the Nodes page.
No SSH credentials, daemon TCP port or control-plane credentials are given to the agent.
"""
import argparse, fcntl, ipaddress, json, os, pathlib, re, secrets, shutil, subprocess, tempfile

ROOT=pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT))
from app.storage import IP_ROOTS, SHARE_PATH

def run(args):
    p=subprocess.run(args,capture_output=True,text=True)
    if p.returncode:raise RuntimeError('Command failed: '+args[0]+' '+args[1]+'; '+p.stderr[-600:])
    return p.stdout

def private(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd,'w') as f:f.write(value)
    os.chmod(name,0o600);os.replace(name,path)

def main():
    p=argparse.ArgumentParser();p.add_argument('--node-id',required=True);p.add_argument('--host',required=True)
    release=json.loads((ROOT/'RELEASE.json').read_text()) if (ROOT/'RELEASE.json').is_file() else {}
    p.add_argument('--image',default=release.get('platform_image','dev-resources/platform:1.1.0'))
    p.add_argument('--bundle',type=pathlib.Path,help='Verified offline images archive on this host')
    args=p.parse_args();nid=args.node_id;host=str(ipaddress.IPv4Address(args.host))
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,47}',nid) or nid=='primary':raise RuntimeError('Invalid/reserved node ID')
    addresses=json.loads(run(['ip','-j','addr']))
    if host not in {a['local'] for n in addresses for a in n.get('addr_info',[])}:raise RuntimeError('Host IP does not belong to this machine')
    if not pathlib.Path('/mnt/juicefs').is_mount():raise RuntimeError('Shared JuiceFS is not mounted')
    fs=run(['stat','-f','-c','%T',str(ROOT)]).strip()
    if 'fuse' not in fs:raise RuntimeError('Project must reside on the mounted JuiceFS')
    central=ROOT/'runtime/secrets';control=json.loads((central/'platform.json').read_text())
    if host==control['host_ip']:raise RuntimeError('Use bootstrap.py for the primary node')
    sid=(ROOT/'data/.cluster-id').read_text().strip()
    if sid!=control['storage_id']:raise RuntimeError('Wrong shared storage')
    local=pathlib.Path('/var/lib/dev-resources/nodes')/nid;local.mkdir(parents=True,exist_ok=True,mode=0o700)
    config=local/'platform.json'
    cfg=json.loads(config.read_text()) if config.exists() else {'executor_secret':secrets.token_urlsafe(48)}
    if cfg.get('host_ip',host)!=host:raise RuntimeError('Node IP changed; create a new reviewed connection')
    cfg.update({'host_ip':host,'storage_id':sid});private(config,json.dumps(cfg))
    if not (local/'server.crt').exists():
        run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-days','365','-keyout',str(local/'server.key'),'-out',str(local/'server.crt'),'-subj','/CN='+host,'-addext','subjectAltName=IP:'+host]);os.chmod(local/'server.key',0o600)
    image=args.image
    if subprocess.run(['docker','image','inspect',image],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        bundle=args.bundle
        if bundle is None or not bundle.is_file():raise RuntimeError('Image missing; provide --bundle with the verified offline archive')
        run(['docker','load','-i',str(bundle)])
    base=IP_ROOTS.get(host)
    if not base:raise RuntimeError('Node IP has no fixed storage mapping')
    run(['mountpoint','-q',base])
    name='drp-node-executor'
    old=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
    if not old.returncode:
        labels=json.loads(old.stdout)[0]['Config'].get('Labels',{})
        if labels.get('io.devresources.infrastructure')!='1':raise RuntimeError('Refusing to replace an unowned container')
        run(['docker','rm','-f',name])
    gpu=['--gpus','all'] if shutil.which('nvidia-smi') else []
    run(['docker','run','-d','--name',name,'--restart','unless-stopped','--label','io.devresources.infrastructure=1',
         '--cpus','2','--memory','2g','--log-opt','max-size=10m','--log-opt','max-file=3',
         '-p',host+':8421:8421','-e','HOST_PROJECT_ROOT='+str(ROOT),'-v','/var/run/docker.sock:/var/run/docker.sock','--pid','host','-v','/proc:/host/proc:ro','-v','/:/host/fs:ro',
         '-v',str(ROOT/'data')+':'+str(ROOT/'data'),'-v',str(ROOT/'workspace')+':'+str(ROOT/'workspace')+':ro','-v',str(local)+':/run/secrets:ro','-v',base+':'+base+':rw,rslave','-v',SHARE_PATH+':'+SHARE_PATH+':rw,rslave']+gpu+
        [image,'uvicorn','app.executor:app','--host','0.0.0.0','--port','8421',
         '--ssl-keyfile','/run/secrets/server.key','--ssl-certfile','/run/secrets/server.crt'])
    # The operator already has root access to the shared control directory.
    # Install only the certificate and scoped executor token in its allowlist.
    with (central/'nodes.lock').open('a') as lock:
        os.chmod(central/'nodes.lock',0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        path=central/'nodes.json'
        conns=json.loads(path.read_text()) if path.exists() else {'primary':{
            'url':'http://executor:8090','ip':control['host_ip'],'key':control['executor_secret']}}
        if nid in conns and conns[nid]['ip']!=host:raise RuntimeError('Connection ID belongs to another IP')
        if any(v['ip']==host and k!=nid for k,v in conns.items()):raise RuntimeError('IP already belongs to another connection')
        private(central/'node-certs'/f'{nid}.crt',(local/'server.crt').read_text())
        conns[nid]={'url':'https://'+host+':8421','ip':host,'key':cfg['executor_secret'],'ca_file':f'/run/secrets/node-certs/{nid}.crt'}
        private(path,json.dumps(conns))
    print(json.dumps({'node_id':nid,'ip':host,'agent_port':8421,'tls':True,'next':'在节点资源页面选择可信连接并登记；平台校验 Docker 主机唯一性和共享存储。'},ensure_ascii=False))

if __name__=='__main__':main()

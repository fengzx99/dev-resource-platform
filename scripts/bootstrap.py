"""Idempotent Docker deployment; owns drp-* containers only. No daemon reconfiguration."""
import argparse, hashlib, json, os, pathlib, re, secrets, ssl, subprocess, time, urllib.request, urllib.error

ROOT=pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT))
from app.storage import IP_ROOTS, SHARE_PATH
SECRET=ROOT/'runtime/secrets'
PREFIX='drp'
PLATFORM_IMAGE='dev-resources/platform:1.1.0'
WORKSPACE_IMAGE='dev-resources/workspace:1.1.0-root-login-20261021'
PORT=8420
GPU_ARGS=['--gpus','all']

def run(args,timeout=240,capture=False):
    p=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=timeout)
    if p.returncode:
        print(p.stderr[-2500:],flush=True)
        raise RuntimeError(f'Command failed: {args[0]} {args[1]} (exit {p.returncode})')
    if not capture and p.stdout.strip():print(p.stdout[-2000:],flush=True)
    return p.stdout

def secret_file(name,value):
    p=SECRET/name;p.write_text(value);os.chmod(p,0o600);return p

def prepare_secrets():
    SECRET.mkdir(parents=True,exist_ok=True,mode=0o700)
    p=SECRET/'platform.json'
    if p.exists():cfg=json.loads(p.read_text())
    elif os.environ.get('DRP_BOOTSTRAP_JSON'):cfg=json.loads(os.environ['DRP_BOOTSTRAP_JSON'])
    else:
        host=os.environ['DRP_HOST']
        cfg={k:secrets.token_urlsafe(32) for k in ('db_password','keycloak_admin_password','oidc_secret','session_secret','executor_secret')}
        cfg.update({'host_ip':host,'public_url':'https://'+host+':'+str(PORT),'users':[{'username':'admin','password':secrets.token_urlsafe(18),'role':'platform-admin'},{'username':'developer','password':secrets.token_urlsafe(18),'role':'developer'}]})
    # Reuse this machine's mounted filesystem credentials only for directory quotas.
    if not cfg.get('juicefs_meta_url'):
        for proc in pathlib.Path('/proc').iterdir():
            if not proc.name.isdigit():continue
            try:
                raw=(proc/'cmdline').read_bytes().decode()
                if not raw.startswith('/usr/local/bin/juicefs') or '/mnt/juicefs' not in raw or 'mount' not in raw:continue
                env=dict(x.split('=',1) for x in (proc/'environ').read_text().split('\0') if '=' in x)
                candidates=[env['JUICEFS_META_URL']] if env.get('JUICEFS_META_URL') else re.findall(r'(?:redis|rediss|mysql|postgres|postgresql|tikv)://[^\s\x00]+',raw)
                cfg['juicefs_env']={k:env[k] for k in ('META_PASSWORD','REDIS_PASSWORD','JFS_META_PASSWORD') if k in env}
                if candidates:cfg['juicefs_meta_url']=candidates[0];break
            except (OSError,UnicodeError):pass
    # Keep one shared filesystem identity across all compute nodes.
    import uuid
    if not pathlib.Path('/mnt/juicefs').is_mount():raise RuntimeError('JuiceFS must be mounted before deployment')
    marker=ROOT/'data/.cluster-id';marker.parent.mkdir(parents=True,exist_ok=True)
    if marker.exists():storage_id=marker.read_text().strip()
    else:
        storage_id=uuid.uuid4().hex;marker.write_text(storage_id+'\n');os.chmod(marker,0o600)
    if cfg.get('storage_id',storage_id)!=storage_id:raise RuntimeError('Shared storage identity changed')
    cfg['storage_id']=storage_id
    secret_file('platform.json',json.dumps(cfg))
    if not (SECRET/'initial-access.txt').exists():
        secret_file('initial-access.txt','URL: '+cfg['public_url']+'\n'+''.join(u['username']+': '+u['password']+'\n' for u in cfg['users'])+'\nKeycloak master administrator: drp-bootstrap\nPassword: '+cfg['keycloak_admin_password']+'\n')
    if not (SECRET/'server.crt').exists():
        run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-days','365','-keyout',str(SECRET/'server.key'),'-out',str(SECRET/'server.crt'),'-subj','/CN='+cfg['host_ip'],'-addext','subjectAltName=IP:'+cfg['host_ip']],capture=True)
        os.chmod(SECRET/'server.key',0o600)
    realm={'realm':'dev-resources','enabled':True,'sslRequired':'external','registrationAllowed':False,'resetPasswordAllowed':False,'loginWithEmailAllowed':False,'displayName':'开发资源中心',
      'roles':{'realm':[{'name':'platform-admin'},{'name':'auditor'},{'name':'developer'}]},
      'clients':[{'clientId':'resource-portal','name':'开发资源中心','enabled':True,'publicClient':False,'secret':cfg['oidc_secret'],'standardFlowEnabled':True,'serviceAccountsEnabled':True,'directAccessGrantsEnabled':True,'redirectUris':[cfg['public_url']+'/callback'],'webOrigins':[cfg['public_url']],
        'protocolMappers':[{'name':'portal-audience','protocol':'openid-connect','protocolMapper':'oidc-audience-mapper','config':{'included.client.audience':'resource-portal','id.token.claim':'true','access.token.claim':'true'}},{'name':'realm-roles-id','protocol':'openid-connect','protocolMapper':'oidc-usermodel-realm-role-mapper','config':{'claim.name':'realm_access.roles','multivalued':'true','jsonType.label':'String','id.token.claim':'true','access.token.claim':'true'}}]}],
      'users':[{'username':u['username'],'enabled':True,'emailVerified':True,'firstName':u['username'],'lastName':'User','email':u['username']+'@local.invalid','realmRoles':[u['role']],'clientRoles':{'account':['manage-account','view-profile']},'credentials':[{'type':'password','value':u['password'],'temporary':False}]} for u in cfg['users']]+[{'username':'service-account-resource-portal','enabled':True,'serviceAccountClientId':'resource-portal','clientRoles':{'realm-management':['manage-users','view-users','view-realm','query-users']}}]}
    realmfile=secret_file('dev-resources-realm.json',json.dumps(realm));os.chmod(realmfile,0o644)
    secret_file('postgres.env',f'POSTGRES_PASSWORD={cfg["db_password"]}\nPOSTGRES_USER=platform\nPOSTGRES_DB=platform\n')
    secret_file('keycloak.env',f'KC_DB=postgres\nKC_DB_URL=jdbc:postgresql://database:5432/platform\nKC_DB_USERNAME=platform\nKC_DB_PASSWORD={cfg["db_password"]}\nKC_DB_SCHEMA=keycloak\nKC_BOOTSTRAP_ADMIN_USERNAME=drp-bootstrap\nKC_BOOTSTRAP_ADMIN_PASSWORD={cfg["keycloak_admin_password"]}\nKC_HOSTNAME={cfg["public_url"]}/auth\nKC_HTTP_ENABLED=true\nKC_HTTP_RELATIVE_PATH=/auth\nKC_PROXY_HEADERS=xforwarded\nKC_HEALTH_ENABLED=true\n')
    secret_file('app.env',f'DATABASE_URL=postgresql+psycopg://platform:{cfg["db_password"]}@database:5432/platform\nPUBLIC_URL={cfg["public_url"]}\nKEYCLOAK_URL=http://keycloak:8080/auth\nEXECUTOR_URL=http://executor:8090\nHOST_PROJECT_ROOT={ROOT}\nWORKSPACE_RUNTIME={os.environ.get("DRP_RUNTIME","docker")}\n')
    print('Secrets and TLS certificate prepared; JuiceFS metadata connection '+('available' if cfg.get('juicefs_meta_url') else 'MISSING'),flush=True)
    return cfg

def exists(kind,name):return subprocess.run(['docker',kind,'inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
def container(name,image,args,command=None,replace=False):
    name=PREFIX+name[3:]
    if exists('container',name):
        labels=json.loads(run(['docker','inspect','--format','{{json .Config.Labels}}',name],capture=True)) or {}
        if labels.get('io.devresources.infrastructure')!='1':raise RuntimeError('Refusing to replace unowned container '+name)
        if not replace:
            run(['docker','start',name]);return
        run(['docker','rm','-f',name])
    run(['docker','run','-d','--name',name,'--label','io.devresources.infrastructure=1','--restart','unless-stopped','--network',PREFIX+'-control','--log-opt','max-size=10m','--log-opt','max-file=3']+args+[image]+(command or []))

def main():
    global PREFIX,PLATFORM_IMAGE,WORKSPACE_IMAGE,PORT,GPU_ARGS
    parser=argparse.ArgumentParser();parser.add_argument('--phase',choices=['build','services','all'],default='all');parser.add_argument('--runtime',choices=['docker'],default='docker');parser.add_argument('--host',default='172.16.9.20');parser.add_argument('--keep-identity',action='store_true')
    parser.add_argument('--prefix',default='drp');parser.add_argument('--port',type=int,default=8420)
    parser.add_argument('--platform-image',default=PLATFORM_IMAGE);parser.add_argument('--workspace-image',default=WORKSPACE_IMAGE)
    parser.add_argument('--no-gpus',action='store_true',help='CPU-only installation rehearsal')
    args=parser.parse_args()
    if not re.fullmatch(r'drp(?:-[a-z0-9-]{1,32})?',args.prefix) or not 1024<=args.port<=65535:parser.error('Invalid prefix or port')
    PREFIX,PORT,PLATFORM_IMAGE,WORKSPACE_IMAGE=args.prefix,args.port,args.platform_image,args.workspace_image
    GPU_ARGS=[] if args.no_gpus else ['--gpus','all']
    os.environ['DRP_RUNTIME']=args.runtime;os.environ['DRP_HOST']=args.host
    cfg=prepare_secrets()
    if args.phase in ('build','all'):
        for image,file in [('python:3.12-slim','python-3.12-slim.tar'),('postgres:16-alpine','postgres-16-alpine.tar'),('quay.io/keycloak/keycloak:26.7.4','keycloak-26.7.4.tar')]:
            if not exists('image',image):
                print('Loading '+image,flush=True);run(['docker','load','-i',str(ROOT/'vendor'/file)],timeout=600)
        print('Building portal and executor offline',flush=True);run(['docker','build','--network=none','-t',PLATFORM_IMAGE,'.'],timeout=600)
        print('Building developer workspace',flush=True);run(['docker','build','--network=none','-t',WORKSPACE_IMAGE,'workspace'],timeout=300)
    if args.phase in ('services','all'):
        if not exists('network',PREFIX+'-control'):run(['docker','network','create',PREFIX+'-control'])
        container('drp-database','postgres:16-alpine',['--network-alias','database','--cpus','2','--memory','2g','--env-file',str(SECRET/'postgres.env'),'-v',PREFIX+'-postgres:/var/lib/postgresql/data'])
        for _ in range(120):
            if subprocess.run(['docker','exec',PREFIX+'-database','pg_isready','-h','127.0.0.1','-U','platform','-d','platform'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
            time.sleep(1)
        else:raise RuntimeError('Database TCP readiness timed out')
        run(['docker','exec',PREFIX+'-database','psql','-U','platform','-d','platform','-c','CREATE SCHEMA IF NOT EXISTS keycloak;'])
        container('drp-keycloak','quay.io/keycloak/keycloak:26.7.4',['--network-alias','keycloak','--cpus','2','--memory','2g','--env-file',str(SECRET/'keycloak.env'),'-v',str(SECRET/'dev-resources-realm.json')+':/opt/keycloak/data/import/realm.json:ro'],['start','--import-realm'],replace=not args.keep_identity)
        (ROOT/'data/volumes').mkdir(parents=True,exist_ok=True)
        binary=shutil_which('juicefs')
        base=IP_ROOTS.get(cfg['host_ip'])
        if not base:raise RuntimeError('Fixed storage mapping missing')
        run(['mountpoint','-q',base],capture=True)
        container('drp-executor',PLATFORM_IMAGE,['--network-alias','executor','--cpus','2','--memory','2g']+GPU_ARGS+['--env-file',str(SECRET/'app.env'),'-v','/var/run/docker.sock:/var/run/docker.sock','--pid','host','-v','/proc:/host/proc:ro','-v','/:/host/fs:ro','-v',str(ROOT)+':'+str(ROOT),'-v',str(SECRET)+':/run/secrets:ro','-v',binary+':/usr/local/bin/juicefs:ro','-v',base+':'+base+':rw,rslave','-v',SHARE_PATH+':'+SHARE_PATH+':rw,rslave'],['uvicorn','app.executor:app','--host','0.0.0.0','--port','8090'],replace=True)
        container('drp-portal',PLATFORM_IMAGE,['--network-alias','portal','--cpus','2','--memory','2g','--env-file',str(SECRET/'app.env'),'-p',cfg['host_ip']+':'+str(PORT)+':8420','-v',str(SECRET)+':/run/secrets:ro'],replace=True)
        ctx=ssl.create_default_context(cafile=str(SECRET/'server.crt'))
        for attempt in range(90):
            try:
                with urllib.request.urlopen(cfg['public_url']+'/auth/realms/dev-resources/.well-known/openid-configuration',context=ctx,timeout=5) as r:
                    discovery=json.load(r)
                if discovery['issuer']==cfg['public_url']+'/auth/realms/dev-resources':break
            except Exception:pass
            if attempt%10==0:print('Waiting for identity service readiness…',flush=True)
            time.sleep(3)
        else:raise RuntimeError('Identity service did not become ready')
        print('DEPLOYED '+cfg['public_url'],flush=True)

def shutil_which(name):
    import shutil
    value=shutil.which(name)
    if not value:raise RuntimeError('Missing executable '+name)
    return value

if __name__=='__main__':main()

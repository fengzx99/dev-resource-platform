"""Internal executor. No public port; fixed operations and platform-owned resources only."""
import io, json, os, re, secrets, shutil, socket, subprocess, tarfile, threading, time
from pathlib import Path
import docker
from fastapi import FastAPI, HTTPException, Request
from .storage import IP_ROOTS, SHARE_PATH, TARGETS, source_path, home_path, fixed_targets, login_user

app=FastAPI(title='Internal resource executor')
ROOT=Path(os.environ.get('HOST_PROJECT_ROOT','/mnt/juicefs/software/dev-resource-platform'))
VOLUMES=ROOT/'data/volumes'
LABEL='io.devresources.workspace'
INFRA='io.devresources.infrastructure'
lock=threading.RLock()
def cfg(): return json.loads(Path('/run/secrets/platform.json').read_text())
def client():return docker.from_env(version='auto',timeout=120)
def ident(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise HTTPException(422,'无效资源标识')
    return value

@app.middleware('http')
async def authentication(req:Request,call_next):
    if not secrets.compare_digest(req.headers.get('x-executor-key',''),cfg()['executor_secret']):return __import__('fastapi').responses.JSONResponse({'detail':'Forbidden'},status_code=403)
    if (req.headers.get('x-expected-engine') and req.headers['x-expected-engine']!=client().info()['ID']) or (req.headers.get('x-storage-id') and req.headers['x-storage-id']!=cfg().get('storage_id')):
        return __import__('fastapi').responses.JSONResponse({'detail':'执行节点或共享存储身份发生变化'},status_code=409)
    try:return await call_next(req)
    except Exception:return __import__('fastapi').responses.JSONResponse({'detail':'执行服务异常，请查看管理员诊断'},status_code=500)

def gpu_devices():
    try:
        result=subprocess.run(['nvidia-smi','--query-gpu=index,name,uuid,memory.total','--format=csv,noheader,nounits'],text=True,capture_output=True,timeout=10)
        devices=[]
        for line in result.stdout.splitlines():
            i,name,uuid,mem=[p.strip() for p in line.split(',')]
            devices.append({'index':int(i),'name':name,'uuid':uuid,'memory_mib':int(mem)})
        return devices
    except Exception:return []

def allocation(c,all_ids):
    hc=c.attrs['HostConfig'];used=[]
    # Privileged containers can access host device nodes regardless of --gpus.
    if hc.get('Privileged'):return list(all_ids)
    def resolve(values):
        out=[]
        for d in values:
            if str(d).isdigit() and int(d)<len(all_ids):out.append(all_ids[int(d)])
            elif d in all_ids:out.append(d)
            else:return list(all_ids)  # Unknown / MIG assignment: reserve conservatively.
        return out
    for r in hc.get('DeviceRequests') or []:
        if r.get('Count')==-1:return list(all_ids)
        ids=r.get('DeviceIDs') or []
        used.extend(resolve(ids))
        if r.get('Count',0)>0 and not ids:return list(all_ids)
    env=c.attrs.get('Config',{}).get('Env') or []
    for e in env:
        if e.startswith('NVIDIA_VISIBLE_DEVICES=') and hc.get('Runtime')=='nvidia':
            value=e.split('=',1)[1]
            if value=='all':return list(all_ids)
            if value not in ('','none','void'):used.extend(resolve(value.split(',')))
    for device in hc.get('Devices') or []:
        match=re.fullmatch(r'/dev/nvidia(\d+)',device.get('PathOnHost',''))
        if match:used.extend(resolve([match[1]]))
    return sorted(set(used))

def storage_ready():
    try:
        # A bind of a JuiceFS subdirectory retains its filesystem type. Refuse a
        # plain local directory left behind after a missing shared mount.
        mounts=[];probe=str(ROOT/'data')
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left,right=line.split(' - ',1);mount=left.split()[4]
            if probe==mount or probe.startswith(mount.rstrip('/')+'/'):
                mounts.append((len(mount),right.split()[0]))
        filesystem=max(mounts)[1]
        return filesystem=='fuse.juicefs' and (ROOT/'data/.cluster-id').read_text().strip()==cfg()['storage_id']
    except (OSError,ValueError,KeyError): return False

def require_storage():
    if not storage_ready():raise HTTPException(503,'共享 JuiceFS 挂载或存储标识校验失败')

def fixed_storage_check():
    """Check the HOST mount table as well as the executor's writable bind.

    os.path.ismount alone misses same-filesystem bind mounts (server003).
    Never create a directory on a disk left behind by a missing mount.
    """
    require_storage()
    ip=cfg()['host_ip'];base=IP_ROOTS.get(ip)
    if not base:raise HTTPException(409,'机器 IP 尚未配置存储父目录')
    entries=[]
    for line in Path('/host/proc/1/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1);fields=left.split();fs=right.split()
        if fields[4]==base:entries.append((fields[3],fs[0],fs[1]))
    if not entries:raise HTTPException(503,'指定存储父目录未挂载，拒绝使用本地空目录')
    root,fs,source=entries[-1]
    if not ((fs in ('nfs','nfs4') and source==ip+':/data-local') or
            (fs not in ('nfs','nfs4') and root=='/data-local')):
        raise HTTPException(503,'指定存储的实际来源与登记不一致')
    for path in (base,SHARE_PATH):
        local=Path(path);host=Path('/host/fs')/path.lstrip('/')
        if not local.is_dir() or local.is_symlink() or not host.is_dir():
            raise HTTPException(503,'执行器缺少固定存储挂载')
        a=local.stat();b=host.stat()
        if (a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino):
            raise HTTPException(503,'执行器存储与宿主机不一致，可能发生掉挂或重新挂载')
    return {'ready':True,'node_ip':ip,'storage_root':base,'share_path':SHARE_PATH}

@app.post('/storage/check')
def storage_check():return fixed_storage_check()

def fixed_storage_ready():
    try:fixed_storage_check();return True
    except (HTTPException,OSError,ValueError):return False

def bound_path(spec):
    if not spec.get('binding'):return volume_path(spec['id'],spec.get('kind','generic'))
    fixed_storage_check()
    binding=spec['binding'];kind=spec.get('kind')
    if binding.get('mode')=='ip_fixed' and binding.get('node_ip')!=cfg()['host_ip']:
        raise HTTPException(409,'禁止将数据目录静默切换到另一台机器')
    if kind=='share' and spec.get('access')!='global_rw':raise HTTPException(422,'全局共享卷必须全员读写')
    if kind!='share' and spec.get('access')!='private':raise HTTPException(422,'用户目录必须私有')
    try:path=Path(source_path(kind,binding))
    except ValueError as exc:raise HTTPException(422,str(exc))
    for part in [path,*path.parents]:
        if part.is_symlink():raise HTTPException(409,'固定存储路径不能包含符号链接')
    return path

def inventory_data():
    c=client();gpus=gpu_devices();ids=[g['uuid'] for g in gpus];used=set();external=[];external_ids=set()
    for box in c.containers.list():
        if box.labels.get(INFRA)=='1':continue
        alloc=allocation(box,ids);used.update(alloc)
        if not box.labels.get(LABEL):
            hc=box.attrs['HostConfig'];external.append({'name':box.name,'gpus':alloc,'cpu':hc.get('NanoCpus',0)/1e9,'memory_gib':hc.get('Memory',0)/1024**3});external_ids.update(alloc)
    info=c.info()
    for g in gpus:g['reserved']=g['uuid'] in used
    return {'engine_id':info['ID'],'host_ip':cfg()['host_ip'],'storage_id':cfg().get('storage_id',''),'storage_ready':storage_ready(),'fixed_storage_ready':fixed_storage_ready(),'hostname':info.get('Name',socket.gethostname()),'cpu_count':info['NCPU'],'memory_gib':round(info['MemTotal']/1024**3,2),'gpus':gpus,'external':external,'external_gpu_reserved':sorted(external_ids),'runtime':os.environ.get('WORKSPACE_RUNTIME','docker')}

@app.post('/inventory')
def inventory():return inventory_data()

@app.post('/metrics')
def metrics():
    from .monitoring import sample
    c=client();c.api.timeout=2
    return sample(c, ROOT)

def volume_path(vid,kind='generic'):
    require_storage()
    if kind not in ('generic','user_data','user_home','share'):raise HTTPException(422,'无效存储用途')
    root=VOLUMES if kind=='generic' else VOLUMES/kind
    if root.is_symlink():raise HTTPException(409,'存储路径异常')
    path=root/ident(vid)
    if path.is_symlink() or (path.exists() and path.resolve().parent!=root.resolve()):raise HTTPException(409,'存储路径异常')
    return path

def set_quota(vid,size,kind='generic'):
    meta=cfg().get('juicefs_meta_url')
    if not meta:raise HTTPException(503,'未配置 JuiceFS 元数据连接，无法保证存储硬配额')
    relative=str(volume_path(vid,kind).relative_to('/mnt/juicefs'))
    p=subprocess.run(['/usr/local/bin/juicefs','quota','set',meta,'--path','/'+relative,'--capacity',str(size),'--strict'],env={**os.environ,**cfg().get('juicefs_env',{})},capture_output=True,text=True,timeout=90)
    if p.returncode:raise HTTPException(503,'JuiceFS 硬配额设置失败，拒绝创建不受限存储卷')

@app.post('/volume/ensure')
def volume_ensure(body:dict):
    with lock:
        size=body['size_gib'];elastic=body.get('elastic',False)
        if type(elastic)!=bool or type(size)!=int or (size!=0 if elastic else not 1<=size<=10000):raise HTTPException(422,'无效容量或存储模式')
        if body.get('binding'):
            if not elastic or size!=0:raise HTTPException(422,'固定目录只支持弹性容量')
            path=bound_path(body)
            if body['kind']=='share':
                if not path.is_dir():raise HTTPException(503,'全局共享目录不存在')
            else:
                path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
                try:
                    path.mkdir(mode=0o770)
                except FileExistsError:
                    if not path.is_dir():raise HTTPException(409,'存储路径不是目录')
                else:
                    os.chown(path,1000,1000);os.chmod(path,0o770)
            return {'quota_enforced':False,'path':str(path),'elastic':True}
        path=volume_path(body['id'],body.get('kind','generic'));path.mkdir(parents=True,exist_ok=True)
        os.chown(path,1000,1000);os.chmod(path,0o770)
        if elastic:return {'quota_enforced':False,'path':str(path),'elastic':True}
        set_quota(body['id'],size,body.get('kind','generic'))
        # The existing JuiceFS mount reloads quota settings on its 12s heartbeat.
        # Do not expose a newly created directory to users before it has reloaded.
        time.sleep(15)
        return {'quota_enforced':True,'path':str(path)}

@app.post('/volume/check-unused')
def volume_check_unused(body:dict):
    path=volume_path(body['id'],body.get('kind','generic'))
    for c in client().containers.list(all=True):
        if any(m.get('Source')==str(path) for m in c.attrs.get('Mounts',[])):
            raise HTTPException(409,'卷仍被节点容器挂载')
    return {'ok':True}

@app.post('/volume/remove')
def volume_remove(body:dict):
    with lock:
        if body.get('binding'):raise HTTPException(409,'固定目录数据不会随容器删除；请管理员另行核对清理')
        path=volume_path(body['id'],body.get('kind','generic'))
        # Refuse a still-mounted volume even if the control-plane database is stale.
        for c in client().containers.list(all=True,filters={'label':LABEL}):
            if any(m.get('Source')==str(path) for m in c.attrs.get('Mounts',[])):raise HTTPException(409,'卷仍被容器挂载')
        if path.exists():shutil.rmtree(path)
        return {'ok':True}

@app.post('/image/bases')
def image_bases():
    return [{'reference':t,'size_gib':round(i.attrs.get('Size',0)/1024**3,2)} for i in client().images.list() for t in i.tags if not t.startswith(('dev-resources/platform','quay.io/keycloak','postgres:','python:'))]

@app.post('/image/build')
def image_build(body:dict):
    base=body.get('base_reference','');ref=body.get('reference','')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9./_:@-]{0,299}',base) or not re.fullmatch(r'dev-resources/custom:[a-z0-9][a-z0-9_.-]{0,59}',ref):raise HTTPException(422,'无效镜像名称')
    c=client()
    try:c.images.get(base)
    except docker.errors.ImageNotFound:raise HTTPException(422,'请先将基础镜像导入节点；此节点使用离线构建')
    template=(ROOT/'workspace/Dockerfile').read_text().splitlines()
    template[0]='FROM '+base
    payload=io.BytesIO()
    with tarfile.open(fileobj=payload,mode='w') as archive:
        for name,content in [('Dockerfile',('\n'.join(template)+'\n').encode()),('entrypoint.sh',(ROOT/'workspace/entrypoint.sh').read_bytes())]:
            info=tarfile.TarInfo(name);info.size=len(content);info.mode=0o644;archive.addfile(info,io.BytesIO(content))
    payload.seek(0)
    try:
        image,_=c.images.build(fileobj=payload,custom_context=True,tag=ref,network_mode='none',pull=False,rm=True,forcerm=True,timeout=160)
    except Exception:raise HTTPException(422,'基础镜像不满足开发镜像规范或构建失败：需要 Linux、bash、OpenSSH server、UID 1000 工具；请先通过 Dockerfile 安装依赖后重新导入')
    publish_image(image)
    return {'digest':image.id,'home_path':image.labels.get('io.devresources.home-path','/home/developer'),'login_user':login_user(image.labels)}

@app.post('/image/prepare')
def image_prepare(body:dict):
    reference=body.get('reference','')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9./_:@-]{0,299}',reference):raise HTTPException(422,'无效镜像地址')
    c=client()
    try:image=c.images.get(reference)
    except docker.errors.ImageNotFound:
        try:image=c.images.pull(reference)
        except Exception:raise HTTPException(503,'镜像拉取失败，请使用节点已导入的镜像或检查镜像仓库连接')
    if image.labels.get('io.devresources.workspace-image')!='1' or image.labels.get('io.devresources.read-only-root')!='1':raise HTTPException(422,'请先使用 workspace/Dockerfile 为基础镜像制作平台开发镜像')
    publish_image(image)
    return {'digest':image.id,'home_path':image.labels.get('io.devresources.home-path','/home/developer'),'login_user':login_user(image.labels)}

def publish_image(image):
    require_storage()
    folder=ROOT/'data/image-cache';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=folder/(image.id.removeprefix('sha256:')+'.tar')
    with lock:
        if not path.exists():
            tmp=path.with_suffix('.partial')
            with tmp.open('wb') as f:
                for chunk in image.save():f.write(chunk)
            os.chmod(tmp,0o600);os.replace(tmp,path)

@app.post('/image/publish')
def image_publish(body:dict):
    image=client().images.get(body['digest']);publish_image(image)
    return {'digest':image.id,'home_path':image.labels.get('io.devresources.home-path','/home/developer'),'login_user':login_user(image.labels)}

@app.post('/image/inspect')
def image_inspect(body:dict):
    image=client().images.get(body['digest'])
    return {'digest':image.id,'persistent_home':image.labels.get('io.devresources.persistent-home')=='1','home_path':image.labels.get('io.devresources.home-path','/home/developer'),'login_user':login_user(image.labels)}

def ensure_image(digest):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',digest):raise HTTPException(422,'必须使用不可变镜像标识')
    c=client()
    try:image=c.images.get(digest)
    except docker.errors.ImageNotFound:
        require_storage();archive=ROOT/'data/image-cache'/(digest[7:]+'.tar')
        if not archive.is_file():raise HTTPException(409,'共享镜像缓存尚未发布，管理员需先发布此镜像')
        with archive.open('rb') as stream:c.images.load(stream)
        try:image=c.images.get(digest)
        except docker.errors.ImageNotFound:raise HTTPException(409,'导入镜像标识不匹配')
    return image

def get_workspace(wid):
    wid=ident(wid);c=client()
    boxes=c.containers.list(all=True,filters={'label':f'{LABEL}={wid}'})
    return boxes[0] if boxes else None

def gpu_candidates(inventory,active,mode='exclusive'):
    if mode not in ('exclusive','shared'):raise HTTPException(422,'无效 GPU 使用方式')
    ids=[g['uuid'] for g in inventory['gpus']]
    if mode=='exclusive':return [g['uuid'] for g in inventory['gpus'] if not g['reserved']]
    # Sharing is explicit. Existing platform-exclusive allocations stay protected.
    protected=set()
    for box in active:
        if box.labels.get(LABEL) and box.labels.get('io.devresources.gpu-mode','exclusive')!='shared':
            protected.update(allocation(box,ids))
    return [uuid for uuid in ids if uuid not in protected]

@app.post('/workspace/ensure')
def workspace_ensure(body:dict):
    with lock:
        wid=ident(body['id']);box=get_workspace(wid)
        if box:
            box.reload()
            if box.status=='running':return docker_result(box)
            box.remove(force=True)
        inv=inventory_data();mode=body.get('gpu_mode','exclusive')
        active=client().containers.list(filters={'label':LABEL})
        available=gpu_candidates(inv,active,mode)
        cpu_used=sum(c.attrs['HostConfig'].get('NanoCpus',0)/1e9 for c in active)
        mem_used=sum(c.attrs['HostConfig'].get('Memory',0)/1024**3 for c in active)
        if cpu_used+body['cpu']>inv['cpu_count']-8 or mem_used+body['memory_gib']>inv['memory_gib']-16:
            return {'status':'queued','reason':'节点计算容量不足，已为宿主机和管理服务预留 8 核 / 16 GiB'}
        available_mem=next((int(x.split()[1])/1024**2 for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')),0)
        if available_mem<body['memory_gib']+16:return {'status':'queued','reason':'节点实际可用内存不足；平台外容器也可能消耗内存'}
        gpu=int(body['gpu'])
        if gpu>len(available):return {'status':'queued','reason':f'需要 {gpu} 张'+('可共享访问 GPU；平台独占卡不会共享' if mode=='shared' else '可独占 GPU；平台外容器访问权也计入占用')+f'，当前可分配 {len(available)} 张'}
        devices=available[:gpu];binds={}
        image=ensure_image(body['image_ref'])
        volume_specs={v['id']:v for v in body.get('volumes',[])}
        if any(v.get('binding') for v in volume_specs.values()):
            if len(body['mounts'])!=3 or {m['path'] for m in body['mounts']}!=fixed_targets(body['mounts']):
                raise HTTPException(422,'固定环境必须且只能包含三个挂载')
            if any(m['readonly'] for m in body['mounts']):raise HTTPException(422,'固定挂载必须读写')
            if any(not volume_specs.get(m['volume_id'],{}).get('binding') for m in body['mounts']):
                raise HTTPException(422,'固定环境不能混用旧存储卷')
        for m in body['mounts']:
            from .policy import validate_volume_mount
            spec=volume_specs.get(m['volume_id'])
            if not spec:raise HTTPException(422,'缺少存储卷规格')
            kind=spec.get('kind','generic')
            validate_volume_mount(kind,m['path'],spec['access'],m['readonly'])
            if kind=='user_home' and image.labels.get('io.devresources.persistent-home')!='1':raise HTTPException(409,'此镜像不支持持久主目录，请选择新版开发镜像')
            path=bound_path(spec)
            if not path.is_dir():raise HTTPException(409,'存储卷不存在')
            binds[str(path)]={'bind':m['path'],'mode':'ro' if m['readonly'] else 'rw'}
        home=home_path(body['mounts']);user=login_user(image.labels)
        ssh_port=body.get('ssh_port')
        if ssh_port is not None and (type(ssh_port)!=int or not 1024<=ssh_port<=65535):raise HTTPException(422,'无效 SSH 端口')
        if home=='/root' and image.labels.get('io.devresources.home-path')!='/root':
            raise HTTPException(409,'所选镜像未适配 /root 主目录，请重新封装开发镜像')
        temp_mounts={'/tmp':'rw,nosuid,nodev,size=1g,mode=1777','/run':'rw,nosuid,nodev,size=64m,mode=755'}
        if not any(m['path'] in ('/root','/home/developer') for m in body['mounts']):temp_mounts['/home/developer']='rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=755'
        c=client()
        try:c.networks.get('drp-workspaces')
        except docker.errors.NotFound:c.networks.create('drp-workspaces',driver='bridge',options={'com.docker.network.bridge.enable_icc':'false'})
        try:
            box=c.containers.run(body['image_ref'],name='drp-ws-'+wid[:20],detach=True,labels={LABEL:wid,'io.devresources.owner':body['owner_id'],'io.devresources.gpu-mode':mode,'io.devresources.login-user':user},
                environment={'HOME':home,'USER':user,'LOGNAME':user,'DRP_SSH_PUBLIC_KEY':body['public_key'],'NVIDIA_DRIVER_CAPABILITIES':'compute,utility','NVIDIA_VISIBLE_DEVICES':','.join(devices) if devices else 'void'},
                network='drp-workspaces',ports={'22/tcp':(cfg()['host_ip'],ssh_port)},
                volumes=binds,nano_cpus=int(float(body['cpu'])*1e9),mem_limit=int(body['memory_gib'])*1024**3,memswap_limit=int(body['memory_gib'])*1024**3,
                shm_size=min(int(body['memory_gib'])*1024**3//4,8*1024**3),pids_limit=1024,security_opt=['no-new-privileges:true'],cap_drop=['NET_RAW'],
                read_only=True,tmpfs=temp_mounts,
                device_requests=[docker.types.DeviceRequest(device_ids=devices,capabilities=[['gpu']])] if devices else None,
                restart_policy={'Name':'no'},log_config=docker.types.LogConfig(type='json-file',config={'max-size':'10m','max-file':'3'}))
            box.reload();return docker_result(box)
        except Exception as e:
            raise HTTPException(503,'创建容器失败：'+str(e)[:250])

def docker_result(box):
    port=box.attrs.get('NetworkSettings',{}).get('Ports',{}).get('22/tcp') or []
    requests=box.attrs['HostConfig'].get('DeviceRequests') or []
    devs=[d for r in requests for d in r.get('DeviceIDs') or []]
    return {'status':'running' if box.status=='running' else 'starting','runtime_id':box.id,'devices':devs,'access':{'ssh':f'ssh {login_user(box.attrs["Config"].get("Labels"))}@{cfg()["host_ip"]} -p {port[0]["HostPort"]}' if port else ''}}

@app.post('/workspace/remove')
def workspace_remove(body:dict):
    with lock:
        box=get_workspace(body['id'])
        if box:box.stop(timeout=10);box.remove()
        return {'ok':True}

@app.post('/workspace/status')
def workspace_status(body:dict):
    box=get_workspace(body['id'])
    return {'status':'running' if box and box.status=='running' else 'stopped','reason':'运行容器已退出'}

@app.post('/workspace/logs')
def workspace_logs(body:dict):
    box=get_workspace(body['id'])
    return {'output':box.logs(tail=150).decode(errors='replace') if box else '环境未运行'}

@app.post('/workspace/exec')
def workspace_exec(body:dict):
    box=get_workspace(body['id'])
    if not box or box.status!='running':raise HTTPException(409,'环境未运行')
    command=body.get('command','')
    if not isinstance(command,str) or not 1<=len(command)<=4000:raise HTTPException(422,'无效命令')
    user=login_user(box.attrs['Config'].get('Labels'));uid='0:0' if user=='root' else '1000:1000'
    r=box.exec_run(['timeout','30','bash','-lc',command],user=uid,environment={'HOME':home_path(box.attrs['Mounts']),'USER':user,'LOGNAME':user},workdir='/workspace' if any(m.get('Destination')=='/workspace' for m in box.attrs['Mounts']) else ('/data' if any(m.get('Destination')=='/data' for m in box.attrs['Mounts']) else '/tmp'))
    return {'exit_code':r.exit_code,'output':r.output.decode(errors='replace')[:50000]}

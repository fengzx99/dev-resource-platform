import asyncio, hashlib, json, os, re, secrets, threading, time
from contextlib import asynccontextmanager
from urllib.parse import urlencode
import httpx, jwt
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from .models import Base, engine, Session, User, Project, Member, Image, Flavor, Volume, Workspace, Audit, LoginSession, Node, record, data, uid
from . import auth
from . import history
from .storage import IP_ROOTS, SHARE_ID, TARGETS, directory_name, source_path, global_share
from .policy import RESERVED, DEFAULT_QUOTA, check_quota, validate_mount, validate_volume_mount, quota_used

from .nodes import executor, migrate, choose, connections, allowed

def db_session():
    with Session() as db: yield db

def platform_admin(u):
    if u.role!='admin': raise HTTPException(403,'需要平台管理员权限')

def project_access(db,u,pid,manage=False):
    p=db.get(Project,pid)
    if not p: raise HTTPException(404,'项目不存在')
    if u.role=='admin': return p
    m=db.scalar(select(Member).where(Member.project_id==pid,Member.user_id==u.id))
    if not m or (manage and m.role!='manager') or (manage and u.role=='auditor'): raise HTTPException(403,'无项目操作权限')
    return p

def project_ids(db,u):
    return None if u.role in ('admin','auditor') else set(db.scalars(select(Member.project_id).where(Member.user_id==u.id)))

def workspace_access(db,u,wid,operate=False):
    w=db.get(Workspace,wid)
    if not w or w.status=='deleted': raise HTTPException(404,'环境不存在')
    if u.role=='admin' or (not operate and u.role=='auditor'): return w
    project_access(db,u,w.project_id)
    if w.owner_id!=u.id or u.role=='auditor': raise HTTPException(403,'只能访问自己的环境')
    return w

def can_volume(db,u,v,pid,for_mount=False):
    if v and v.status=='ready' and global_share(v):return
    if not v or v.project_id!=pid or v.status!='ready': raise HTTPException(403,'存储卷不属于本项目或尚未就绪')
    if v.owner_id!=u.id and v.access=='private' and u.role!='admin': raise HTTPException(403,'未获得该存储卷授权')

def totals(db,pid,owner=None):
    ws=select(Workspace).where(Workspace.project_id==pid)
    vs=select(Volume).where(Volume.project_id==pid)
    if owner: ws=ws.where(Workspace.owner_id==owner);vs=vs.where(Volume.owner_id==owner)
    return quota_used(db.scalars(ws).all(),db.scalars(vs).all())

def reserve_check(db,u,pid,added,owner=None):
    p=db.scalar(select(Project).where(Project.id==pid).with_for_update().execution_options(populate_existing=True))
    if not p: raise HTTPException(404,'项目不存在')
    check_quota(p.quota,totals(db,pid),added)
    target=owner or u.id
    member=db.scalar(select(Member).where(Member.project_id==pid,Member.user_id==target).execution_options(populate_existing=True))
    if member: check_quota(member.quota,totals(db,pid,target),added)
    elif u.role!='admin' or target!=u.id: raise HTTPException(403,'用户未加入项目')
    return p

def validate_restart(db,w):
    owner=db.get(User,w.owner_id);im=db.get(Image,w.image_id)
    if not owner or not owner.enabled:raise HTTPException(403,'所属用户已停用')
    if not im or not im.enabled or im.status!='ready' or im.project_id not in (None,w.project_id):raise HTTPException(403,'镜像未就绪或未授权')
    project_access(db,owner,w.project_id)
    choose(db,owner,w.node_id,w.project_id)
    mounts=[]
    for m in w.mounts:
        v=db.get(Volume,m['volume_id']);can_volume(db,owner,v,w.project_id,True)
        if v.binding and v.binding.get('mode')=='ip_fixed' and v.binding['node_ip']!=db.get(Node,w.node_id).ip:
            raise HTTPException(409,'环境与数据存储节点不一致，请管理员明确迁移后再启动')
        validate_volume_mount(v.kind,m['path'],v.access,m['readonly'])
        home_lease(db,v,w.id)
        mounts.append({**m,'readonly':m['readonly'] or (v.access=='project_ro' and v.owner_id!=owner.id)})
    w.mounts=mounts
    return owner

def home_lease(db,v,workspace_id=None):
    if v.kind!='user_home':return
    # Same project row lock serializes this check with requests, starts and stops.
    for other in db.scalars(select(Workspace).where(Workspace.project_id==v.project_id,Workspace.status.in_(list(RESERVED)))):
        if other.id!=workspace_id and any(m['volume_id']==v.id for m in other.mounts):
            raise HTTPException(409,'该 user_home 已被运行或等待中的环境占用，请停止原环境或创建独立主目录卷')

def reconcile_workspace(wid,pid):
    with Session() as db:
        # Same project lock as quota/state APIs. A stop/delete cannot be overwritten
        # by an in-flight create result, nor can a quota be released before removal.
        locked=db.scalar(select(Project).where(Project.id==pid).with_for_update(skip_locked=True))
        if not locked:db.rollback();return
        w=db.get(Workspace,wid,populate_existing=True)
        if not w or w.status not in RESERVED:db.rollback();return
        previous_runtime=w.runtime_id
        if w.expires<time.time() and w.status not in ('deleting','stopping'):
            w.desired='stopped';w.status='stopping';w.error='使用期限已到'
        try:
            if w.status in ('queued','starting'):
                validate_restart(db,w)
                w.status='starting';db.flush()
                payload=data(w)
                payload['volumes']=[data(db.get(Volume,m['volume_id'])) for m in w.mounts]
                result=executor('workspace/ensure',payload,w.node_id)
                w.runtime_id=result.get('runtime_id','');w.devices=result.get('devices',[]);w.access_info=result.get('access',{})
                w.status=result.get('status','running');w.error=result.get('reason','')
                if w.status=='queued' and time.time()-w.updated>900:
                    executor('workspace/remove',{'id':w.id},w.node_id)
                    w.status='failed';w.desired='stopped';w.error='等待资源超过 15 分钟，已释放配额；可重新启动'
            elif w.status in ('stopping','deleting'):
                executor('workspace/remove',{'id':w.id},w.node_id)
                w.status='deleted' if w.status=='deleting' else 'stopped';w.devices=[];w.access_info={};w.runtime_id=''
            elif w.status=='running':
                result=executor('workspace/status',{'id':w.id},w.node_id)
                if result['status']!='running': w.status='failed';w.desired='stopped';w.error=result.get('reason','运行实例已退出')
                else:w.error=''
            history.sync_allocation(db,w,started=bool(w.runtime_id) and w.runtime_id!=previous_runtime)
            db.commit()
        except HTTPException as e:
            w.status='stopping';w.desired='stopped';w.error=str(e.detail)[:600];db.commit()
        except Exception as e:
            # Keep allocations reserved if deletion fails; never declare resources freed early.
            w.error=str(e)[:600]
            if w.status=='starting': w.status='queued'
            db.commit()

def node_controller(nid):
    while True:
        try:
            with Session() as db:
                candidates=list(db.execute(select(Workspace.id,Workspace.project_id).where(Workspace.node_id==nid,Workspace.status.in_(list(RESERVED)))))
            for wid,pid in candidates:reconcile_workspace(wid,pid)
        except Exception as e:print('node-controller',nid,type(e).__name__,flush=True)
        time.sleep(3)

def controller():
    workers={}
    while True:
        try:
            with Session() as db:
                for nid in db.scalars(select(Node.id)):
                    if nid not in workers or not workers[nid].is_alive():
                        t=threading.Thread(target=node_controller,args=(nid,),daemon=True);workers[nid]=t;t.start()
                for img in db.scalars(select(Image).where(Image.status.in_(['pending','building']))).all():
                    try:
                        result=executor('image/build' if img.status=='building' else 'image/prepare',{'reference':img.reference,**(img.build_config or {})});img.digest=result['digest'];img.build_config={**(img.build_config or {}),'home_path':result.get('home_path','/home/developer'),'login_user':result.get('login_user','developer')};img.status='ready'
                    except Exception as e:img.status='failed';img.error=str(e)[:600]
                    db.commit()
        except Exception as e:print('controller:',type(e).__name__,flush=True)
        time.sleep(3)

@asynccontextmanager
async def lifespan(app):
    migrate()
    if os.environ.get('DISABLE_CONTROLLER')!='1':
        threading.Thread(target=controller,daemon=True).start()
        history.start_collector()
    yield

app=FastAPI(title='开发资源管理平台',version='1.1.0',lifespan=lifespan)
app.include_router(history.router)

@app.middleware('http')
async def security(req,call_next):
    if req.method in ('POST','PUT','PATCH','DELETE') and req.url.path.startswith('/api/') and not req.headers.get('authorization','').startswith('Bearer '):
        if req.headers.get('origin')!=auth.PUBLIC or req.headers.get('x-drp-request')!='1': return Response('请求来源校验失败',403)
    response=await call_next(req)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='SAMEORIGIN'
    if req.url.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
    return response

@app.get('/healthz')
def health():
    with Session() as db: db.execute(select(1))
    return {'status':'ok','version':'1.1.0'}

@app.get('/login')
def login():
    return login_response()

@app.get('/account/password')
def account_password(u=Depends(auth.current_user)):
    return login_response('UPDATE_PASSWORD',u.username)

def login_response(action='',username=''):
    state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(32)
    verifier=secrets.token_urlsafe(48)
    import base64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    params={'client_id':auth.CLIENT,'response_type':'code','redirect_uri':auth.PUBLIC+'/callback','scope':'openid profile','state':state,'nonce':nonce,'code_challenge':challenge,'code_challenge_method':'S256'}
    if action=='UPDATE_PASSWORD':params.update({'kc_action':action,'prompt':'login','login_hint':username})
    r=RedirectResponse(auth.ISSUER+'/protocol/openid-connect/auth?'+urlencode(params))
    r.set_cookie('drp_login',jwt.encode({'state':state,'nonce':nonce,'verifier':verifier,'action':action,'exp':time.time()+300},auth.config()['session_secret'],algorithm='HS256'),httponly=True,secure=auth.PUBLIC.startswith('https'),samesite='lax',max_age=300)
    return r

@app.get('/callback')
def callback(req:Request,code:str='',state:str=''):
    try:
        saved=jwt.decode(req.cookies.get('drp_login',''),auth.config()['session_secret'],algorithms=['HS256'])
        if not secrets.compare_digest(saved['state'],state): raise ValueError()
        r=httpx.post(f'{auth.KC}/realms/{auth.REALM}/protocol/openid-connect/token',data={'client_id':auth.CLIENT,'client_secret':auth.config()['oidc_secret'],'grant_type':'authorization_code','code':code,'redirect_uri':auth.PUBLIC+'/callback','code_verifier':saved['verifier']},timeout=15)
        r.raise_for_status();tokens=r.json();c=auth.claims(tokens['id_token'])
        if c.get('nonce')!=saved['nonce']: raise ValueError()
    except Exception: raise HTTPException(401,'登录校验失败，请重新登录')
    token=secrets.token_urlsafe(48)
    with Session() as db:
        user=auth.ensure_user(db,c)
        if not user.enabled: raise HTTPException(403,'账号已停用')
        if saved.get('action')=='UPDATE_PASSWORD' and req.query_params.get('kc_action_status')=='success':
            for session in db.scalars(select(LoginSession).where(LoginSession.user_id==user.id)):db.delete(session)
            record(db,user,'user.password-change',user.id)
        db.add(LoginSession(id=hashlib.sha256(token.encode()).hexdigest(),user_id=user.id,expires=time.time()+8*3600))
        record(db,user,'login');db.commit()
    response=RedirectResponse('/#account' if saved.get('action')=='UPDATE_PASSWORD' else '/')
    response.delete_cookie('drp_login');response.set_cookie('drp_session',token,httponly=True,secure=auth.PUBLIC.startswith('https'),samesite='lax',max_age=28800)
    return response

@app.post('/api/logout')
def logout(req:Request,u=Depends(auth.current_user),db=Depends(db_session)):
    s=db.get(LoginSession,hashlib.sha256(req.cookies.get('drp_session','').encode()).hexdigest())
    if s: db.delete(s)
    # End Keycloak sessions as well, so a shared browser cannot immediately re-enter.
    auth.kc_admin('POST','users/'+u.id+'/logout')
    record(db,u,'logout');db.commit();r=Response(status_code=204);r.delete_cookie('drp_session');return r

@app.api_route('/auth/{path:path}',methods=['GET','POST','PUT','DELETE','OPTIONS'])
async def identity_proxy(path:str,req:Request):
    headers={k:v for k,v in req.headers.items() if k.lower() not in ('host','connection','content-length','accept-encoding','x-forwarded-host','x-forwarded-proto','x-forwarded-port')}
    from urllib.parse import urlsplit
    public=urlsplit(auth.PUBLIC)
    headers.update({'x-forwarded-host':public.netloc,'x-forwarded-proto':public.scheme,'x-forwarded-port':str(public.port or 443)})
    async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
        r=await client.request(req.method,auth.KC+'/'+path,params=req.query_params,content=await req.body(),headers=headers)
    response=Response(r.content,status_code=r.status_code)
    response.raw_headers=[(k,v) for k,v in r.headers.raw if k.lower() not in (b'content-length',b'content-encoding',b'transfer-encoding',b'connection')]
    return response

class Quota(BaseModel):
    cpu:float=Field(8,ge=0,le=4096)
    memory_gib:int=Field(32,ge=0,le=16384)
    gpu:int=Field(1,ge=0,le=1024)
    storage_gib:int=Field(100,ge=0,le=1000000)
    workspaces:int=Field(3,ge=0,le=10000)
class NewProject(BaseModel):
    name:str=Field(min_length=1,max_length=60)
    quota:Quota
    approval:bool=False
class NewUser(BaseModel):
    username:str=Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{2,40}$')
    role:str=Field(default='developer',pattern='^(admin|developer|auditor)$')
class NewMember(BaseModel):
    user_id:str
    role:str=Field(default='developer',pattern='^(manager|developer)$')
    quota:Quota
class NewImage(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    reference:str=Field(min_length=1,max_length=300,pattern=r'^[a-zA-Z0-9][a-zA-Z0-9./_:@-]*$')
    project_id:str|None=None
class BuildImage(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    base_reference:str=Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9./_:@-]{0,299}$')
    tag:str=Field(pattern=r'^[a-z0-9][a-z0-9_.-]{0,59}$')
    project_id:str|None=None
class NewFlavor(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    project_id:str|None=None
    cpu:float=Field(ge=.25,le=256)
    memory_gib:int=Field(ge=1,le=2048)
    gpu:int=Field(0,ge=0,le=8)
class NewVolume(BaseModel):
    kind:str=Field('generic',pattern='^(generic|user_data|user_home|share)$')
    name:str=Field(min_length=1,max_length=80)
    project_id:str
    owner_id:str|None=None
    size_gib:int=Field(ge=0,le=10000)
    elastic:bool=False
    access:str=Field('private',pattern='^(private|project_ro|project_rw)$')
class Mount(BaseModel):
    volume_id:str
    path:str
    readonly:bool=False
class NewWorkspace(BaseModel):
    node_id:str=Field('primary',max_length=64)
    name:str=Field(min_length=1,max_length=80)
    project_id:str
    image_id:str
    flavor_id:str
    gpu_mode:str=Field('exclusive',pattern='^(exclusive|shared)$')
    mounts:list[Mount]=Field(default_factory=list,max_length=8)
    public_key:str=Field(min_length=60,max_length=4096)
    duration_hours:int=Field(24,ge=1,le=720)
    request_key:str=Field(min_length=16,max_length=80)

@app.get('/api/me')
def me(u=Depends(auth.current_user)): return data(u)

@app.get('/api/overview')
def overview(u=Depends(auth.current_user),db=Depends(db_session)):
    ids=project_ids(db,u)
    projects=db.scalars(select(Project)).all()
    out=[]
    for p in projects:
        if ids is not None and p.id not in ids: continue
        m=db.scalar(select(Member).where(Member.project_id==p.id,Member.user_id==u.id))
        out.append({**data(p),'used':totals(db,p.id),'my_quota':m.quota if m else p.quota,'my_used':totals(db,p.id,u.id),'my_role':m.role if m else u.role})
    return {'projects':out,'user':data(u),'runtime':os.environ.get('WORKSPACE_RUNTIME','docker')}

@app.get('/api/node')
def node(u=Depends(auth.current_user)):
    platform_admin(u)
    try:return executor('inventory')
    except Exception as e: raise HTTPException(503,str(e))

@app.get('/api/nodes')
def nodes(u=Depends(auth.current_user),db=Depends(db_session)):
    from concurrent.futures import ThreadPoolExecutor
    ids=project_ids(db,u)
    selected=[n for n in db.scalars(select(Node)) if ids is None or not n.project_ids or set(n.project_ids)&ids]
    def inspect_node(n):
        out={**data(n),'storage_root':IP_ROOTS.get(n.ip)}
        try:
            inv=executor('inventory',node_id=n.id)
            out.update({'online':True,'storage_ready':inv.get('storage_ready',False),
                        'fixed_storage_ready':inv.get('fixed_storage_ready',False),
                        'identity_valid':inv.get('engine_id')==n.engine_id and inv.get('storage_id')==n.storage_id,
                        'cpu_count':inv['cpu_count'],'memory_gib':inv['memory_gib'],
                        'gpu_count':len(inv['gpus']),'gpu_available':sum(not g['reserved'] for g in inv['gpus'])})
            if u.role=='admin':out['inventory']=inv
        except Exception as e:out.update({'online':False,'error':str(e)})
        if u.role not in ('admin','auditor'):
            out.pop('engine_id',None);out.pop('storage_id',None)
        return out
    with ThreadPoolExecutor(max_workers=8) as pool:return list(pool.map(inspect_node,selected))

@app.get('/api/node-connections')
def node_connections(u=Depends(auth.current_user)):
    platform_admin(u)
    return [{'id':key,'ip':v['ip']} for key,v in connections().items()]

@app.get('/api/monitoring')
def monitoring(u=Depends(auth.current_user),db=Depends(db_session)):
    if u.role not in ('admin','developer'):raise HTTPException(403,'需要平台管理员或开发者权限')
    from concurrent.futures import ThreadPoolExecutor
    from .monitoring import workspace_metrics, developer_metrics
    selected=list(db.scalars(select(Node)))
    usernames={x.id:x.username for x in db.scalars(select(User))}
    projects={x.id:x.name for x in db.scalars(select(Project))}
    workspaces=[{'id':w.id,'name':w.name,'node_id':w.node_id,'runtime_id':w.runtime_id,
                 'owner_id':w.owner_id,'username':usernames.get(w.owner_id,'用户已删除'),
                 'project_id':w.project_id,'project_name':projects.get(w.project_id,'项目已删除'),
                 'status':w.status,'gpu':w.gpu,'gpu_mode':w.gpu_mode}
                for w in db.scalars(select(Workspace).where(Workspace.status!='deleted'))]
    own_ids=None
    if u.role=='developer':
        memberships=project_ids(db,u)
        own_ids={w['id'] for w in workspaces if w['owner_id']==u.id and w['project_id'] in memberships
                 and w['runtime_id'] and w['status'] in ('running','starting','stopping','deleting','failed')}
        selected=[n for n in selected if any(w['id'] in own_ids and w['node_id']==n.id
                  and (not n.project_ids or w['project_id'] in n.project_ids) for w in workspaces)]
        own_ids={w['id'] for w in workspaces if w['id'] in own_ids and any(n.id==w['node_id']
                 and (not n.project_ids or w['project_id'] in n.project_ids) for n in selected)}
    def read(n):
        out={'id':n.id,'name':n.name,'ip':n.ip,'enabled':n.enabled}
        try:
            metrics=executor('metrics',node_id=n.id)
            metrics=workspace_metrics(metrics,n.id,workspaces) if own_ids is None else developer_metrics(metrics,n.id,workspaces,own_ids)
            out.update(online=True,metrics=metrics)
        except Exception:
            out.update(online=False,error='节点监控暂时不可用，请检查节点连接与执行器版本')
        return out
    with ThreadPoolExecutor(max_workers=8) as pool:
        return {'nodes':list(pool.map(read,selected)),'refresh_seconds':5,'scope':'all' if own_ids is None else 'allocated'}

@app.post('/api/nodes',status_code=201)
def register_node(body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);key=body.get('connection_id','');conf=connections().get(key)
    if not conf:raise HTTPException(422,'请先通过节点部署包配置可信连接')
    if db.get(Node,key):raise HTTPException(409,'节点已登记')
    try:inv=executor('inventory',node_id=key)
    except Exception as e:raise HTTPException(503,str(e))
    primary=db.get(Node,'primary')
    if inv.get('host_ip')!=conf['ip'] or not inv.get('storage_ready') or inv.get('storage_id')!=primary.storage_id:
        raise HTTPException(409,'IP 或共享存储身份不匹配')
    if db.scalar(select(Node).where((Node.engine_id==inv['engine_id']) | (Node.ip==conf['ip']))):
        raise HTTPException(409,'同一 Docker 主机不能重复登记为多个计算节点')
    n=Node(id=key,name=str(body.get('name') or inv['hostname'])[:80],ip=conf['ip'],engine_id=inv['engine_id'],storage_id=inv['storage_id'])
    db.add(n);record(db,u,'node.register',key,detail={'ip':n.ip});db.commit();return data(n)

@app.patch('/api/nodes/{nid}')
def update_node(nid:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);n=db.get(Node,nid)
    if not n:raise HTTPException(404,'节点不存在')
    if 'enabled' in body:
        if type(body['enabled'])!=bool:raise HTTPException(422,'enabled 必须为布尔值')
        n.enabled=body['enabled']
    if 'project_ids' in body:
        ids=body['project_ids']
        if not isinstance(ids,list) or any(not isinstance(x,str) or not db.get(Project,x) for x in ids):raise HTTPException(422,'项目授权列表无效')
        n.project_ids=list(set(ids))
    if 'name' in body:
        if not isinstance(body['name'],str) or not 1<=len(body['name'])<=80:raise HTTPException(422,'名称无效')
        n.name=body['name']
    record(db,u,'node.update',nid,detail=body);db.commit();return data(n)

@app.put('/api/workspaces/{wid}/node')
def move_node(wid:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    w=workspace_access(db,u,wid,True)
    db.scalar(select(Project).where(Project.id==w.project_id).with_for_update().execution_options(populate_existing=True));db.refresh(w)
    if w.status!='stopped' or w.runtime_id:raise HTTPException(409,'须先确认原节点已停止并移除容器，再切换节点')
    n=choose(db,u,body.get('node_id',''),w.project_id)
    if n.id!=w.node_id and any((db.get(Volume,m['volume_id']).binding or {}).get('mode')=='ip_fixed' for m in w.mounts):
        raise HTTPException(409,'该环境的数据按机器 IP 绑定；跨机器使用须由管理员明确迁移，不能直接切换')
    if n.id!='primary':executor('image/publish',{'digest':w.image_ref})
    w.node_id=n.id;record(db,u,'workspace.change-node',wid,w.project_id,{'node_id':n.id});db.commit();return data(w)

@app.get('/api/users')
def users(u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);return [data(x) for x in db.scalars(select(User)).all()]

@app.post('/api/users',status_code=201)
def add_user(body:NewUser,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u)
    if db.scalar(select(User).where(User.username==body.username)): raise HTTPException(409,'用户名已存在')
    password=secrets.token_urlsafe(18)
    user=User(id=auth.create_identity(body.username,password,body.role),username=body.username,role=body.role)
    db.add(user);record(db,u,'user.create',user.id,detail={'username':user.username,'role':user.role});db.commit()
    return {**data(user),'initial_password':password}

@app.patch('/api/users/{user_id}')
def update_user(user_id:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u)
    v=db.get(User,user_id)
    if not v:raise HTTPException(404,'账号不存在')
    if v.id==u.id:raise HTTPException(409,'不能停用当前管理员账号')
    if type(body.get('enabled')) is not bool:raise HTTPException(422,'需要 enabled 布尔值')
    auth.kc_admin('PUT','users/'+v.id,{'enabled':body['enabled']});v.enabled=body['enabled']
    if not v.enabled:
        for pid in sorted(set(db.scalars(select(Workspace.project_id).where(Workspace.owner_id==v.id)))):
            db.scalar(select(Project).where(Project.id==pid).with_for_update().execution_options(populate_existing=True))
        for w in db.scalars(select(Workspace).where(Workspace.owner_id==v.id,Workspace.status.in_(list(RESERVED)))):
            if w.status!='deleting':w.status='stopping';w.desired='stopped';w.error='所属账号已停用'
    record(db,u,'user.enable' if v.enabled else 'user.disable',v.id);db.commit();return data(v)

@app.post('/api/users/{user_id}/reset-password')
def reset_password(user_id:str,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);v=db.get(User,user_id)
    if not v:raise HTTPException(404,'账号不存在')
    password=secrets.token_urlsafe(18)
    auth.kc_admin('PUT','users/'+v.id+'/reset-password',{'type':'password','value':password,'temporary':False})
    for session in db.scalars(select(LoginSession).where(LoginSession.user_id==v.id)):db.delete(session)
    auth.kc_admin('POST','users/'+v.id+'/logout')
    record(db,u,'user.password-reset',v.id);db.commit();return {'username':v.username,'initial_password':password}

@app.put('/api/users/{user_id}/role')
def change_role(user_id:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);v=db.get(User,user_id);role=body.get('role')
    if not v:raise HTTPException(404,'账号不存在')
    if v.id==u.id:raise HTTPException(409,'不能变更当前管理员自身角色')
    if role not in ('admin','developer','auditor'):raise HTTPException(422,'无效角色')
    mapping={'admin':'platform-admin','developer':'developer','auditor':'auditor'}
    old=auth.kc_admin('GET','users/'+v.id+'/role-mappings/realm').json()
    remove=[r for r in old if r['name'] in mapping.values()]
    if remove:auth.kc_admin('DELETE','users/'+v.id+'/role-mappings/realm',remove)
    new=auth.kc_admin('GET','roles/'+mapping[role]).json();auth.kc_admin('POST','users/'+v.id+'/role-mappings/realm',[new])
    v.role=role
    for pid in sorted(set(db.scalars(select(Workspace.project_id).where(Workspace.owner_id==v.id)))):db.scalar(select(Project).where(Project.id==pid).with_for_update().execution_options(populate_existing=True))
    for w in db.scalars(select(Workspace).where(Workspace.owner_id==v.id,Workspace.status.in_(list(RESERVED)))):
        if w.status!='deleting':w.status='stopping';w.desired='stopped';w.error='所属账号角色变更，请重新申请启动'
    record(db,u,'user.role',v.id,detail={'role':role});db.commit();return data(v)

@app.post('/api/projects',status_code=201)
def add_project(body:NewProject,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u)
    if db.scalar(select(Project).where(Project.name==body.name)):raise HTTPException(409,'项目名称已存在')
    p=Project(name=body.name,quota=body.quota.model_dump(),approval=body.approval);db.add(p);db.flush();record(db,u,'project.create',p.id,p.id);db.commit();return data(p)

@app.patch('/api/projects/{pid}')
def update_project(pid:str,body:NewProject,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);p=db.scalar(select(Project).where(Project.id==pid).with_for_update().execution_options(populate_existing=True))
    if not p:raise HTTPException(404,'项目不存在')
    if db.scalar(select(Project).where(Project.name==body.name,Project.id!=pid)):raise HTTPException(409,'项目名称已存在')
    check_quota(body.quota.model_dump(),totals(db,pid),{'cpu':0,'memory_gib':0,'gpu':0,'storage_gib':0,'workspaces':0})
    p.name=body.name;p.quota=body.quota.model_dump();p.approval=body.approval;record(db,u,'project.update',pid,pid);db.commit();return data(p)

@app.get('/api/projects/{pid}/members')
def members(pid:str,u=Depends(auth.current_user),db=Depends(db_session)):
    project_access(db,u,pid,True)
    return [{**data(m),'username':db.get(User,m.user_id).username,'used':totals(db,pid,m.user_id)} for m in db.scalars(select(Member).where(Member.project_id==pid)).all()]

@app.delete('/api/projects/{pid}')
def delete_project(pid:str,confirm:str,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);p=db.scalar(select(Project).where(Project.id==pid).with_for_update().execution_options(populate_existing=True))
    if not p:raise HTTPException(404,'项目不存在')
    if confirm!=p.name:raise HTTPException(422,'需输入项目名称确认')
    if db.scalar(select(Workspace).where(Workspace.project_id==pid,Workspace.status!='deleted')) or db.scalar(select(Volume).where(Volume.project_id==pid,Volume.status!='deleted')):raise HTTPException(409,'需先删除项目内全部开发环境和存储卷')
    for cls in (Member,Image,Flavor):
        for row in db.scalars(select(cls).where(cls.project_id==pid)):db.delete(row)
    record(db,u,'project.delete',pid,pid,{'name':p.name});db.delete(p);db.commit();return {'ok':True}

@app.delete('/api/projects/{pid}/members/{user_id}')
def remove_member(pid:str,user_id:str,u=Depends(auth.current_user),db=Depends(db_session)):
    project_access(db,u,pid,True);db.scalar(select(Project).where(Project.id==pid).with_for_update().execution_options(populate_existing=True))
    m=db.scalar(select(Member).where(Member.project_id==pid,Member.user_id==user_id))
    if not m:raise HTTPException(404,'成员不存在')
    if u.role!='admin' and (m.role=='manager' or m.user_id==u.id):raise HTTPException(403,'仅平台管理员可移除项目管理员')
    for w in db.scalars(select(Workspace).where(Workspace.project_id==pid,Workspace.owner_id==user_id)):
        if w.status in RESERVED and w.status!='deleting':w.status='stopping';w.desired='stopped';w.error='项目成员资格已撤销'
        elif w.status=='pending_approval':w.status='rejected';w.desired='stopped';w.error='项目成员资格已撤销'
    db.delete(m);record(db,u,'member.remove',user_id,pid);db.commit();return {'ok':True}

@app.get('/api/projects/{pid}/eligible-users')
def eligible_users(pid:str,u=Depends(auth.current_user),db=Depends(db_session)):
    project_access(db,u,pid,True)
    return [{'id':x.id,'username':x.username} for x in db.scalars(select(User).where(User.enabled==True,User.role!='auditor'))]

@app.put('/api/projects/{pid}/members')
def set_member(pid:str,body:NewMember,u=Depends(auth.current_user),db=Depends(db_session)):
    project_access(db,u,pid,True)
    if not db.get(User,body.user_id):raise HTTPException(404,'账号不存在')
    # A project manager cannot grant manager privileges or raise their own quota.
    if u.role!='admin' and (body.role=='manager' or body.user_id==u.id):raise HTTPException(403,'仅平台管理员可调整项目管理员')
    p=db.scalar(select(Project).where(Project.id==pid).with_for_update().execution_options(populate_existing=True))
    for k,v in body.quota.model_dump().items():
        if v>p.quota.get(k,0):raise HTTPException(422,'个人上限不能高于项目上限')
    check_quota(body.quota.model_dump(),totals(db,pid,body.user_id),dict.fromkeys(DEFAULT_QUOTA,0))
    m=db.scalar(select(Member).where(Member.project_id==pid,Member.user_id==body.user_id))
    if m and m.role=='manager' and u.role!='admin':raise HTTPException(403,'仅平台管理员可调整其他项目管理员')
    if not m:m=Member(project_id=pid,user_id=body.user_id);db.add(m)
    m.role=body.role;m.quota=body.quota.model_dump();record(db,u,'member.set',body.user_id,pid,{'role':m.role,'quota':m.quota});db.commit();return data(m)

@app.get('/api/images')
def images(u=Depends(auth.current_user),db=Depends(db_session)):
    ids=project_ids(db,u)
    counts=dict(db.execute(select(Workspace.image_id,func.count()).where(Workspace.status!='deleted').group_by(Workspace.image_id)).all()) if u.role=='admin' else {}
    return [{**data(i),**({'workspace_count':counts.get(i.id,0)} if u.role=='admin' else {})}
            for i in db.scalars(select(Image).where(Image.status!='removed')).all()
            if ids is None or ((i.project_id is None or i.project_id in ids) and i.enabled)]

@app.post('/api/images',status_code=201)
def add_image(body:NewImage,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u)
    if body.project_id:project_access(db,u,body.project_id)
    img=Image(**body.model_dump());db.add(img);db.flush();record(db,u,'image.register',img.id,body.project_id,{'reference':img.reference});db.commit();return data(img)

@app.get('/api/image-bases')
def image_bases(u=Depends(auth.current_user)):
    platform_admin(u);return executor('image/bases')

@app.post('/api/images/build',status_code=201)
def build_image(body:BuildImage,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u)
    if body.project_id:project_access(db,u,body.project_id)
    ref='dev-resources/custom:'+body.tag
    if db.scalar(select(Image).where(Image.reference==ref)):raise HTTPException(409,'该镜像标签已登记，请使用新版本标签')
    i=Image(name=body.name,reference=ref,project_id=body.project_id,status='building',build_config={'base_reference':body.base_reference});db.add(i);db.flush();record(db,u,'image.build',i.id,i.project_id,{'base_reference':body.base_reference});db.commit();return data(i)

@app.patch('/api/images/{iid}')
def update_image(iid:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);i=db.scalar(select(Image).where(Image.id==iid).with_for_update().execution_options(populate_existing=True))
    if not i or i.status=='removed':raise HTTPException(404,'镜像不存在')
    if type(body.get('enabled')) is not bool:raise HTTPException(422,'需要 enabled')
    i.enabled=body['enabled'];record(db,u,'image.enable' if i.enabled else 'image.disable',iid,i.project_id);db.commit();return data(i)

@app.delete('/api/images/{iid}')
def remove_image(iid:str,confirm:str,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u)
    # New workspace requests take the same lock before reading the image.
    i=db.scalar(select(Image).where(Image.id==iid).with_for_update().execution_options(populate_existing=True))
    if not i or i.status=='removed':raise HTTPException(404,'镜像不存在')
    if confirm!=i.name:raise HTTPException(422,'需输入镜像名称确认')
    if i.status in ('pending','building'):raise HTTPException(409,'镜像正在准备或构建，请完成后再移除')
    count=db.scalar(select(func.count()).select_from(Workspace).where(Workspace.image_id==iid,Workspace.status!='deleted'))
    if count:raise HTTPException(409,f'仍有 {count} 个开发环境引用此镜像，请先删除这些环境；如只需停止新申请，可禁用镜像')
    # Keep the immutable identity for deleted-workspace history. Do not call Docker.
    i.enabled=False;i.status='removed'
    record(db,u,'image.remove',iid,i.project_id,{'name':i.name,'reference':i.reference,'digest':i.digest})
    db.commit();return {'ok':True}

@app.get('/api/flavors')
def flavors(u=Depends(auth.current_user),db=Depends(db_session)):
    ids=project_ids(db,u)
    return [data(f) for f in db.scalars(select(Flavor)).all() if ids is None or ((f.project_id is None or f.project_id in ids) and f.enabled)]

@app.post('/api/flavors',status_code=201)
def add_flavor(body:NewFlavor,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u)
    if body.project_id:project_access(db,u,body.project_id)
    f=Flavor(**body.model_dump());db.add(f);db.flush();record(db,u,'flavor.create',f.id,f.project_id);db.commit();return data(f)

@app.patch('/api/flavors/{fid}')
def edit_flavor(fid:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    platform_admin(u);f=db.get(Flavor,fid)
    if not f:raise HTTPException(404,'套餐不存在')
    if type(body.get('enabled')) is not bool:raise HTTPException(422,'需要 enabled')
    f.enabled=body['enabled'];record(db,u,'flavor.enable' if f.enabled else 'flavor.disable',fid,f.project_id);db.commit();return data(f)

@app.get('/api/volumes')
def volumes(u=Depends(auth.current_user),db=Depends(db_session)):
    ids=project_ids(db,u)
    return [data(v) for v in db.scalars(select(Volume).where(Volume.status!='deleted')).all() if global_share(v) or ids is None or (v.project_id in ids and (v.owner_id==u.id or v.access!='private'))]

@app.post('/api/volumes',status_code=201)
def add_volume(body:NewVolume,u=Depends(auth.current_user),db=Depends(db_session)):
    if u.role=='auditor':raise HTTPException(403,'审计员不能创建存储')
    if body.elastic:
        platform_admin(u)
        if body.size_gib!=0:raise HTTPException(422,'弹性存储卷的容量须为 0，表示不设置目录配额')
    elif body.size_gib<1:raise HTTPException(422,'定额存储卷容量至少为 1 GiB')
    project_access(db,u,body.project_id)
    owner=body.owner_id or u.id
    if owner!=u.id or body.access!='private':project_access(db,u,body.project_id,True)
    if body.kind=='user_home' and body.access!='private':raise HTTPException(422,'用户主目录只允许私有访问')
    reserve_check(db,u,body.project_id,{'storage_gib':body.size_gib},owner)
    v=Volume(**{**body.model_dump(),'owner_id':owner});db.add(v);db.flush()
    try:
        r=executor('volume/ensure',data(v));v.quota_enforced=r['quota_enforced'];v.status='ready'
    except Exception as e:raise HTTPException(503,str(e))
    record(db,u,'volume.create',v.id,v.project_id,{'size_gib':v.size_gib,'elastic':v.elastic,'owner':owner});db.commit();return data(v)

@app.patch('/api/volumes/{vid}')
def edit_volume(vid:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    v=db.get(Volume,vid)
    if not v or v.status!='ready':raise HTTPException(404,'存储卷不存在')
    if v.binding:raise HTTPException(409,'固定挂载的目录和权限由平台管理，不支持单独修改')
    project_access(db,u,v.project_id,True)
    db.scalar(select(Project).where(Project.id==v.project_id).with_for_update().execution_options(populate_existing=True));db.refresh(v)
    size=body.get('size_gib',v.size_gib);access=body.get('access',v.access)
    if 'elastic' in body and body['elastic']!=v.elastic:raise HTTPException(422,'存储容量模式创建后不可切换')
    if type(size)!=int or size<v.size_gib or size>10000 or (v.elastic and size!=0) or access not in ('private','project_ro','project_rw'):raise HTTPException(422,'容量仅支持扩容；弹性卷不设置容量，访问模式必须有效')
    if v.kind=='user_home' and access!='private':raise HTTPException(422,'用户主目录只允许私有访问')
    # Expanding shared access permits future RW mounts; existing Docker binds stay unchanged.
    expand_shared=v.access=='project_ro' and access=='project_rw'
    if access!=v.access and not expand_shared and any(any(m['volume_id']==vid for m in w.mounts) for w in db.scalars(select(Workspace).where(Workspace.status.in_(list(RESERVED))))):raise HTTPException(409,'变更存储授权前需先停止使用该卷的环境')
    reserve_check(db,u,v.project_id,{'storage_gib':size-v.size_gib},v.owner_id)
    try:executor('volume/ensure',{**data(v),'size_gib':size})
    except Exception as e:raise HTTPException(503,str(e))
    v.size_gib=size;v.access=access;record(db,u,'volume.update',vid,v.project_id,{'size_gib':size,'access':access});db.commit();return data(v)

@app.delete('/api/volumes/{vid}')
def delete_volume(vid:str,confirm:str,u=Depends(auth.current_user),db=Depends(db_session)):
    v=db.get(Volume,vid)
    if not v or v.status=='deleted':raise HTTPException(404,'存储卷不存在')
    if v.binding:raise HTTPException(409,'固定挂载数据永久保留；清理须由管理员另行核对处理')
    project_access(db,u,v.project_id)
    if u.role!='admin' and v.owner_id!=u.id:raise HTTPException(403,'无权删除')
    if u.role=='auditor':raise HTTPException(403,'无权删除')
    if confirm!=v.name:raise HTTPException(422,'需输入卷名称确认永久删除')
    db.scalar(select(Project).where(Project.id==v.project_id).with_for_update().execution_options(populate_existing=True));db.refresh(v)
    if any(any(m['volume_id']==vid for m in w.mounts) for w in db.scalars(select(Workspace).where(Workspace.status!='deleted'))):raise HTTPException(409,'仍被环境引用，需先删除引用环境')
    # Probe every enrolled node before destructive shared-storage removal, including maintenance nodes.
    for n in db.scalars(select(Node)):
        try:executor('volume/check-unused',data(v),n.id)
        except Exception as e:raise HTTPException(409,str(e))
    executor('volume/remove',data(v));v.status='deleted';record(db,u,'volume.delete',vid,v.project_id);db.commit();return {'ok':True}

@app.get('/api/workspaces')
def workspaces(u=Depends(auth.current_user),db=Depends(db_session)):
    q=select(Workspace).where(Workspace.status!='deleted').order_by(Workspace.created.desc())
    if u.role not in ('admin','auditor'):q=q.where(Workspace.owner_id==u.id)
    return [data(w) for w in db.scalars(q).all()]

@app.post('/api/workspaces',status_code=201)
def add_workspace(body:NewWorkspace,u=Depends(auth.current_user),db=Depends(db_session)):
    if u.role=='auditor':raise HTTPException(403,'审计员不能创建环境')
    project_access(db,u,body.project_id)
    db.scalar(select(Project).where(Project.id==body.project_id).with_for_update().execution_options(populate_existing=True))
    old=db.scalar(select(Workspace).where(Workspace.owner_id==u.id,Workspace.request_key==body.request_key))
    if old:return data(old)
    image=db.scalar(select(Image).where(Image.id==body.image_id).with_for_update().execution_options(populate_existing=True));flavor=db.get(Flavor,body.flavor_id)
    if not image or not image.enabled or image.status!='ready' or image.project_id not in (None,body.project_id):raise HTTPException(403,'镜像未就绪或未授权')
    if not flavor or not flavor.enabled or flavor.project_id not in (None,body.project_id):raise HTTPException(403,'套餐未授权')
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key
    try:load_ssh_public_key(body.public_key.encode())
    except Exception:raise HTTPException(422,'SSH 公钥格式错误；请输入 ssh-ed25519 或 ssh-rsa 公钥')
    if body.mounts:raise HTTPException(422,'新环境固定挂载 /data、/root、/share，不接受自定义挂载')
    node=choose(db,u,body.node_id,body.project_id)
    if node.ip not in IP_ROOTS:raise HTTPException(409,'该机器尚未配置固定存储父目录')
    if body.node_id!='primary':executor('image/publish',{'digest':image.digest})
    image_info=executor('image/inspect',{'digest':image.digest},body.node_id)
    if not image_info['persistent_home'] or image_info.get('home_path')!='/root' or image_info.get('login_user')!='root':
        raise HTTPException(422,'所选镜像未适配 root 登录和 /root 持久主目录，请选择新版开发镜像或重新封装')
    executor('storage/check',node_id=body.node_id)
    mounts=[]
    resources={'cpu':flavor.cpu,'memory_gib':flavor.memory_gib,'gpu':flavor.gpu,'workspaces':1}
    p=reserve_check(db,u,body.project_id,resources)
    wid=uid()
    directory=directory_name(body.name,wid)
    # Serialize the single global share across projects, without changing project ACLs.
    from sqlalchemy import text
    if db.bind.dialect.name=='postgresql':db.execute(text('SELECT pg_advisory_xact_lock(781230941)'))
    shared=db.get(Volume,SHARE_ID)
    if shared and not global_share(shared):raise HTTPException(409,'全局共享卷登记冲突')
    if not shared:
        shared=Volume(id=SHARE_ID,name='global-share',project_id=None,owner_id=None,kind='share',access='global_rw',binding={'mode':'global_share'},size_gib=0,elastic=True,status='ready')
        db.add(shared);db.flush()
    for kind in ('user_data','user_home','share'):
        if kind=='share':v=shared
        else:
            v=Volume(id=uid(),name=body.name+' · '+kind,project_id=p.id,owner_id=u.id,kind=kind,access='private',
                     binding={'mode':'ip_fixed','node_ip':node.ip,'directory':directory},size_gib=0,elastic=True,status='ready')
            db.add(v);db.flush()
        executor('volume/ensure',data(v),body.node_id)
        mounts.append({'volume_id':v.id,'path':TARGETS[kind],'readonly':False})
    w=Workspace(id=wid,node_id=body.node_id,name=body.name,owner_id=u.id,project_id=p.id,image_id=image.id,image_ref=image.digest or image.reference,cpu=flavor.cpu,memory_gib=flavor.memory_gib,gpu=flavor.gpu,gpu_mode=body.gpu_mode,mounts=mounts,public_key=body.public_key.strip(),expires=time.time()+body.duration_hours*3600,request_key=body.request_key,status='pending_approval' if p.approval and u.role!='admin' else 'queued')
    db.add(w);db.flush();record(db,u,'workspace.request',w.id,p.id,{**resources,'gpu_mode':body.gpu_mode});db.commit();return data(w)

@app.get('/api/approvals')
def approvals(u=Depends(auth.current_user),db=Depends(db_session)):
    managed=None if u.role=='admin' else set(db.scalars(select(Member.project_id).where(Member.user_id==u.id,Member.role=='manager')))
    if u.role=='auditor':managed=set()
    return [data(w) for w in db.scalars(select(Workspace).where(Workspace.status=='pending_approval')) if managed is None or w.project_id in managed]

@app.post('/api/workspaces/{wid}/action')
def action(wid:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    act=body.get('action')
    w=db.get(Workspace,wid)
    if not w or w.status=='deleted':raise HTTPException(404,'环境不存在')
    if act in ('approve','reject'):project_access(db,u,w.project_id,True)
    else:workspace_access(db,u,wid,True)
    # Serialize every state transition on the same project lock used by quota checks.
    db.scalar(select(Project).where(Project.id==w.project_id).with_for_update().execution_options(populate_existing=True));db.refresh(w)
    if act=='approve':
        if w.status!='pending_approval':raise HTTPException(409,'申请已处理')
        if w.expires<=time.time():raise HTTPException(409,'申请已到期，请先续期')
        owner=validate_restart(db,w)
        reserve_check(db,owner,w.project_id,{'cpu':w.cpu,'memory_gib':w.memory_gib,'gpu':w.gpu})
        w.status='queued';w.desired='running'
    elif act=='reject':
        if w.status!='pending_approval':raise HTTPException(409,'申请已处理')
        w.status='rejected';w.desired='stopped';w.error=str(body.get('reason','申请被拒绝'))[:200]
    elif act=='stop':
        if w.status not in ('queued','starting','running','failed'):raise HTTPException(409,'当前状态无法停止')
        w.status='stopping';w.desired='stopped'
    elif act=='start':
        if w.status not in ('stopped','failed'):raise HTTPException(409,'当前状态无法启动')
        if w.expires<=time.time():raise HTTPException(409,'请先续期')
        owner=validate_restart(db,w)
        reserve_check(db,owner,w.project_id,{'cpu':w.cpu,'memory_gib':w.memory_gib,'gpu':w.gpu})
        p=db.get(Project,w.project_id)
        w.status='pending_approval' if p.approval and u.role!='admin' else 'queued';w.desired='running';w.error=''
    elif act=='delete':w.status='deleting';w.desired='deleted'
    elif act=='renew':
        hours=body.get('hours',24)
        if type(hours)!=int or not 1<=hours<=720:raise HTTPException(422,'续期时长须为 1–720 小时')
        p=db.get(Project,w.project_id)
        if p.approval and u.role!='admin':project_access(db,u,w.project_id,True)
        w.expires=time.time()+hours*3600
    else:raise HTTPException(422,'未知操作')
    w.updated=time.time();record(db,u,'workspace.'+act,w.id,w.project_id);db.commit();return data(w)

@app.get('/api/workspaces/{wid}/logs')
def logs(wid:str,u=Depends(auth.current_user),db=Depends(db_session)):
    w=workspace_access(db,u,wid,True)
    return executor('workspace/logs',{'id':w.id},w.node_id)

@app.post('/api/workspaces/{wid}/exec')
def execute(wid:str,body:dict,u=Depends(auth.current_user),db=Depends(db_session)):
    w=workspace_access(db,u,wid,True)
    if w.status!='running':raise HTTPException(409,'环境未运行')
    command=body.get('command','')
    if not isinstance(command,str) or not command or len(command)>4000:raise HTTPException(422,'命令长度无效')
    record(db,u,'workspace.exec',wid,w.project_id,{'command_sha256':hashlib.sha256(command.encode()).hexdigest()});db.commit()
    return executor('workspace/exec',{'id':wid,'command':command},w.node_id)

@app.get('/api/audit')
def audit(u=Depends(auth.current_user),db=Depends(db_session)):
    q=select(Audit).order_by(Audit.created.desc()).limit(200)
    if u.role not in ('admin','auditor'):q=q.where(Audit.actor_id==u.id)
    return [data(a) for a in db.scalars(q)]

app.mount('/static',StaticFiles(directory=os.path.join(os.path.dirname(__file__),'static')),name='static')
@app.get('/')
def index():return FileResponse(os.path.join(os.path.dirname(__file__),'static/index.html'))

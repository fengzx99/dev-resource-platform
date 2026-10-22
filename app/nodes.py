"""Node routing uses an operator-installed allowlist, never a request-supplied URL."""
import json, os, ssl, time
from pathlib import Path
import httpx
from fastapi import HTTPException
from sqlalchemy import select, inspect, text
from . import auth
from .models import Base, engine, Session, Node, Volume

def connections():
    path=Path('/run/secrets/nodes.json')
    return json.loads(path.read_text()) if path.exists() else {
        'primary':{'url':os.environ.get('EXECUTOR_URL','http://executor:8090'),
                   'key':auth.config()['executor_secret'],'ip':auth.config()['host_ip']}}

def executor(action,payload=None,node_id='primary'):
    conf=connections().get(node_id)
    if not conf: raise RuntimeError('节点连接尚未配置，请联系管理员')
    if node_id!='primary' and not conf['url'].startswith('https://'):
        raise RuntimeError('远程执行节点必须使用 HTTPS')
    verify=ssl.create_default_context(cafile=conf['ca_file']) if conf.get('ca_file') else True
    headers={'X-Executor-Key':conf['key']}
    with Session() as db:
        n=db.get(Node,node_id)
        if n:headers.update({'X-Expected-Engine':n.engine_id,'X-Storage-Id':n.storage_id})
    try:
        r=httpx.post(conf['url'].rstrip('/')+'/'+action,json=payload or {},
                     headers=headers,verify=verify,
                     timeout=httpx.Timeout(12 if action=='metrics' else 10 if action=='inventory' else 180,connect=3),trust_env=False)
    except httpx.HTTPError:
        raise RuntimeError('节点连接失败；保留资源预留，请检查节点网络、证书和执行服务') from None
    if r.status_code>=400:
        try: message=r.json().get('detail','执行失败')
        except Exception: message='执行服务异常'
        raise RuntimeError(str(message)[:600])
    return r.json()

def allowed(node,user,project_id=None):
    return user.role=='admin' or not node.project_ids or project_id in node.project_ids

def choose(db,user,node_id,project_id,check_online=True):
    node=db.get(Node,node_id)
    if not node or not node.enabled or not allowed(node,user,project_id):
        raise HTTPException(403,'节点不存在、已停止接收申请或未授权给此项目')
    if check_online:
        try: inv=executor('inventory',node_id=node.id)
        except Exception as e: raise HTTPException(503,str(e))
        if inv.get('engine_id')!=node.engine_id or inv.get('storage_id')!=node.storage_id or not inv.get('storage_ready'):
            raise HTTPException(409,'节点身份或共享存储校验失败，禁止分配')
    return node

def migrate():
    """Additive, idempotent upgrade. Existing volume paths and running containers stay intact."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        if 'kind' not in {c['name'] for c in inspect(conn).get_columns('volumes')}:
            conn.execute(text("ALTER TABLE volumes ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'generic'"))
        if 'node_id' not in {c['name'] for c in inspect(conn).get_columns('workspaces')}:
            conn.execute(text("ALTER TABLE workspaces ADD COLUMN node_id VARCHAR NOT NULL DEFAULT 'primary'"))
        if 'gpu_mode' not in {c['name'] for c in inspect(conn).get_columns('workspaces')}:
            conn.execute(text("ALTER TABLE workspaces ADD COLUMN gpu_mode VARCHAR NOT NULL DEFAULT 'exclusive'"))
        if 'binding' not in {c['name'] for c in inspect(conn).get_columns('volumes')}:
            conn.execute(text("ALTER TABLE volumes ADD COLUMN binding JSON NOT NULL DEFAULT '{}'"))
        if 'elastic' not in {c['name'] for c in inspect(conn).get_columns('volumes')}:
            conn.execute(text("ALTER TABLE volumes ADD COLUMN elastic BOOLEAN NOT NULL DEFAULT FALSE"))
    with Session() as db:
        from .storage import SHARE_ID
        if not db.get(Volume,SHARE_ID):
            db.add(Volume(id=SHARE_ID,name='global-share',kind='share',project_id=None,owner_id=None,access='global_rw',
                          binding={'mode':'global_share'},size_gib=0,elastic=True,status='ready'))
        if not db.get(Node,'primary'):
            conf=connections()['primary'];inv=executor('inventory')
            if not inv.get('storage_ready'):raise RuntimeError('主节点共享存储未就绪')
            db.add(Node(id='primary',name=inv['hostname'],ip=conf['ip'],engine_id=inv['engine_id'],storage_id=inv['storage_id']))
        db.commit()

"""Control-plane regressions with independent mocked node endpoints; not physical-node acceptance."""
import time
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException
from app import main, nodes
from app.models import Base, Node, Project, Workspace, Volume, User
from app.policy import validate_volume_mount, quota_used

@pytest.fixture
def database(monkeypatch):
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine);factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(main,'Session',factory);monkeypatch.setattr(nodes,'Session',factory)
    with factory() as db:
        db.add_all([Node(id='primary',name='a',ip='10.0.0.1',engine_id='engine-a',storage_id='shared'),
                    Node(id='second',name='b',ip='10.0.0.2',engine_id='engine-b',storage_id='shared'),
                    Project(id='p',name='project',quota={}),User(id='u',username='test',role='admin')]);db.commit()
    yield factory
    engine.dispose()

@pytest.mark.parametrize('kind,path,access,readonly',[
    ('user_home','/home/user','private',False),('user_home','/home/developer','project_rw',False),
    ('user_home','/home/developer','private',True),('generic','/home/developer','private',False),
    ('user_data','/share','private',False),('share','/data','project_ro',True)])
def test_storage_semantic_restrictions(kind,path,access,readonly):
    with pytest.raises(HTTPException):validate_volume_mount(kind,path,access,readonly)

def test_node_authorization_storage_identity_and_maintenance(database,monkeypatch):
    monkeypatch.setattr(nodes,'executor',lambda *a,**k:{'engine_id':'engine-b','storage_id':'shared','storage_ready':True})
    user=SimpleNamespace(role='developer')
    with database() as db:
        n=db.get(Node,'second');n.project_ids=['p'];db.commit()
        assert nodes.choose(db,user,'second','p').ip=='10.0.0.2'
        with pytest.raises(HTTPException):nodes.choose(db,user,'second','other')
        n.enabled=False;db.commit()
        with pytest.raises(HTTPException):nodes.choose(db,user,'second','p')
        n.enabled=True;db.commit()
        monkeypatch.setattr(nodes,'executor',lambda *a,**k:{'engine_id':'WRONG','storage_id':'shared','storage_ready':True})
        with pytest.raises(HTTPException):nodes.choose(db,user,'second','p')

def test_offline_node_keeps_resources_and_operations_stay_on_target(database,monkeypatch):
    calls=[]
    def fake(action,payload=None,node_id='primary'):
        calls.append((action,node_id))
        if node_id=='second':raise RuntimeError('offline')
        return {'status':'running'}
    monkeypatch.setattr(main,'executor',fake)
    with database() as db:
        for wid,nid,status in [('w1','primary','running'),('w2','second','stopping')]:
            db.add(Workspace(id=wid,name=wid,owner_id='u',project_id='p',node_id=nid,cpu=2,memory_gib=4,gpu=1,mounts=[],status=status,expires=time.time()+300,request_key=wid))
        db.commit()
    main.reconcile_workspace('w2','p');main.reconcile_workspace('w1','p')
    with database() as db:
        w=db.get(Workspace,'w2');assert w.status=='stopping' and w.error=='offline'
        assert quota_used([w],[])['gpu']==1
        assert db.get(Workspace,'w1').status=='running'
        with pytest.raises(HTTPException):main.move_node('w2',{'node_id':'primary'},db.get(User,'u'),db)
    assert calls==[('workspace/remove','second'),('workspace/status','primary')]

def test_home_lease_cross_node(database):
    with database() as db:
        v=Volume(id='v',name='home',project_id='p',owner_id='u',kind='user_home',size_gib=1)
        w=Workspace(id='w',name='w',node_id='second',project_id='p',owner_id='u',cpu=1,memory_gib=1,gpu=0,status='stopping',mounts=[{'volume_id':'v'}],request_key='w')
        db.add_all([v,w]);db.commit()
        with pytest.raises(HTTPException):main.home_lease(db,v)
        main.home_lease(db,v,'w')
        w.status='stopped';db.commit();main.home_lease(db,v)

def test_duplicate_docker_engine_rejected(database,monkeypatch):
    monkeypatch.setattr(main,'connections',lambda:{'duplicate':{'ip':'10.0.0.9'}})
    monkeypatch.setattr(main,'executor',lambda *a,**k:{'host_ip':'10.0.0.9','hostname':'dup','storage_ready':True,'storage_id':'shared','engine_id':'engine-a'})
    with database() as db:
        with pytest.raises(HTTPException) as e:main.register_node({'connection_id':'duplicate'},db.get(User,'u'),db)
        assert e.value.status_code==409

def test_remote_node_requires_tls(database,monkeypatch):
    monkeypatch.setattr(nodes,'connections',lambda:{'second':{'url':'http://10.0.0.2:8421','key':'unused'}})
    with pytest.raises(RuntimeError,match='HTTPS'):nodes.executor('workspace/remove',{'id':'w'},'second')

def test_monitoring_isolates_offline_nodes_and_enforces_role(database,monkeypatch):
    def metrics(action,node_id):
        assert action=='metrics'
        if node_id=='second':raise RuntimeError('private connection details')
        return {'collected_at':1,'gpus':[]}
    monkeypatch.setattr(main,'executor',metrics)
    with database() as db:
        result=main.monitoring(SimpleNamespace(role='admin'),db)
        rows={n['id']:n for n in result['nodes']}
        assert rows['primary']['online'] and not rows['second']['online']
        assert 'private' not in rows['second']['error']
        assert result['refresh_seconds']==5
    from fastapi.testclient import TestClient
    main.app.dependency_overrides[main.auth.current_user]=lambda:SimpleNamespace(role='auditor')
    try:
        assert TestClient(main.app).get('/api/monitoring').status_code==403
    finally:
        main.app.dependency_overrides.clear()


def test_monitoring_enriches_actual_workspace_owner_from_database(database,monkeypatch):
    metrics={'collected_at':1,'gpus':[{'index':0,'uuid':'GPU-a','utilization_percent':30,
        'allocations':[{'container_id':'runtime','workspace_id':'w'}],
        'processes':[{'pid':123,'container_id':'runtime','workspace_id':'w','user':'root','memory_mib':25}]}]}
    monkeypatch.setattr(main,'executor',lambda *a,**k:metrics)
    with database() as db:
        db.add(Workspace(id='w',name='training',owner_id='u',project_id='p',node_id='primary',
            cpu=2,memory_gib=4,gpu=1,status='running',runtime_id='runtime',request_key='w'))
        db.commit()
        result=main.monitoring(SimpleNamespace(role='admin'),db)
    rows={n['id']:n['metrics'] for n in result['nodes']}
    process=rows['primary']['gpus'][0]['processes'][0]
    assert process['workspace']['username']=='test'
    assert process['workspace']['project_name']=='project' and process['user']=='root'
    assert rows['primary']['workspaces'][0]['process_memory_mib']==25
    assert rows['second']['gpus'][0]['processes'][0]['workspace'] is None
    assert 'workspace' not in metrics['gpus'][0]['processes'][0]


def test_elastic_volume_requires_admin_and_zero_capacity(database,monkeypatch):
    body=main.NewVolume(name='elastic',project_id='p',kind='user_data',size_gib=0,elastic=True)
    with pytest.raises(HTTPException) as error:main.add_volume(body,SimpleNamespace(id='u',role='developer'),None)
    assert error.value.status_code==403
    with pytest.raises(HTTPException) as error:main.add_volume(main.NewVolume(name='bad',project_id='p',size_gib=0),SimpleNamespace(id='u',role='developer'),None)
    assert error.value.status_code==422
    calls=[]
    def ensure(action,payload):
        calls.append(payload)
        assert action=='volume/ensure' and payload['elastic'] and payload['size_gib']==0
        return {'quota_enforced':False}
    monkeypatch.setattr(main,'executor',ensure)
    with database() as db:
        admin=db.get(User,'u');result=main.add_volume(body,admin,db)
        assert result['elastic'] and result['size_gib']==0 and not result['quota_enforced']
        assert main.totals(db,'p')['storage_gib']==0
        with pytest.raises(HTTPException):main.edit_volume(result['id'],{'elastic':False},admin,db)
    assert len(calls)==1


def test_expiry_of_shared_environment_removes_container_but_keeps_elastic_data(database,monkeypatch):
    calls=[]
    monkeypatch.setattr(main,'executor',lambda action,payload,node_id:(calls.append(action) or {'ok':True}))
    with database() as db:
        db.add(Volume(id='elastic',name='data',project_id='p',owner_id='u',kind='user_data',size_gib=0,elastic=True,status='ready'))
        db.add(Workspace(id='expired',name='fullnode',owner_id='u',project_id='p',node_id='primary',
            cpu=248,memory_gib=2048,gpu=8,gpu_mode='shared',status='running',runtime_id='runtime',devices=['GPU-a'],
            expires=time.time()-1,mounts=[{'volume_id':'elastic','path':'/data','readonly':False}],request_key='expired'))
        db.commit()
    main.reconcile_workspace('expired','p')
    with database() as db:
        w=db.get(Workspace,'expired')
        assert w.status=='stopped' and not w.runtime_id and not w.devices
        assert db.get(Volume,'elastic').status=='ready' and db.get(Volume,'elastic').elastic
    assert calls==['workspace/remove']

def test_stopped_workspace_move_and_command_route(database,monkeypatch):
    calls=[]
    monkeypatch.setattr(main,'choose',lambda db,u,nid,pid:db.get(Node,nid))
    monkeypatch.setattr(main,'executor',lambda action,payload,node_id="primary":calls.append((action,node_id)) or {'output':'ok'})
    with database() as db:
        w=Workspace(id='w',name='w',node_id='primary',project_id='p',owner_id='u',cpu=1,memory_gib=1,gpu=0,status='stopped',mounts=[],runtime_id='',request_key='w')
        db.add(w);db.commit();u=db.get(User,'u')
        assert main.move_node('w',{'node_id':'second'},u,db)['node_id']=='second'
        w.status='running';db.commit()
        main.execute('w',{'command':'pwd'},u,db);main.logs('w',u,db)
    assert calls==[('image/publish','primary'),('workspace/exec','second'),('workspace/logs','second')]


def test_expand_share_access_preserves_running_readonly_bind(database, monkeypatch):
    calls=[]
    monkeypatch.setattr(main,'executor',lambda action,payload=None,*a,**k:calls.append((action,payload)) or {})
    with database() as db:
        v=Volume(id='shared',name='project-share',project_id='p',owner_id='u',kind='share',access='project_ro',size_gib=10,status='ready')
        db.get(Project,'p').quota={'storage_gib':100}
        w=Workspace(id='running',name='existing',project_id='p',owner_id='u',cpu=1,memory_gib=1,gpu=0,status='running',runtime_id='unchanged',
                    mounts=[{'volume_id':'shared','path':'/share','readonly':True}],request_key='share-test')
        db.add_all([v,w]);db.commit()
        result=main.edit_volume(v.id,{'access':'project_rw'},db.get(User,'u'),db)
        assert result['access']=='project_rw'
        assert w.status=='running' and w.runtime_id=='unchanged' and w.mounts[0]['readonly'] is True
        assert [c[0] for c in calls]==['volume/ensure']
        with pytest.raises(HTTPException) as e:main.edit_volume(v.id,{'access':'project_ro'},db.get(User,'u'),db)
        assert e.value.status_code==409
        with pytest.raises(HTTPException):main.edit_volume(v.id,{'access':'private'},db.get(User,'u'),db)
        assert v.access=='project_rw'


def test_share_access_expansion_still_requires_project_manager(database):
    with database() as db:
        db.add(Volume(id='shared',name='project-share',project_id='p',owner_id='u',kind='share',access='project_ro',size_gib=10,status='ready'))
        db.commit()
        with pytest.raises(HTTPException) as e:main.edit_volume('shared',{'access':'project_rw'},SimpleNamespace(id='outsider',role='developer'),db)
        assert e.value.status_code==403

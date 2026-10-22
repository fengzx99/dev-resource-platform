import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import history as h, main, nodes
from app.models import Base, User, Member, Project, Node, Workspace, ResourceAllocation, ResourceMetric


@pytest.fixture
def database(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (h, main, nodes):
        monkeypatch.setattr(module, 'Session', factory)
    with factory() as db:
        db.add_all([User(id='a', username='admin', role='admin'), User(id='u', username='alice'),
                    User(id='v', username='bob'), Project(id='p', name='research'), Project(id='q', name='private'),
                    Member(user_id='u', project_id='p', role='developer'),
                    Node(id='n', name='B300', ip='test', engine_id='e', storage_id='s')])
        db.commit()
    yield factory
    engine.dispose()


def ws(ident='w', owner='u', project='p', runtime='runtime', mode='shared'):
    return Workspace(id=ident, name='workspace-' + ident, owner_id=owner, project_id=project, node_id='n',
                     runtime_id=runtime, image_ref='image:1', cpu=4, memory_gib=32, gpu=1, gpu_mode=mode,
                     devices=['GPU-0'], mounts=[], status='running', request_key=ident, expires=time.time() + 3600)


def test_lifecycle_preserves_old_runtime_and_does_not_duplicate_after_portal_restart(database):
    with database() as db:
        w = ws(); db.add(w); db.flush()
        h.sync_allocation(db, w, now=100)
        h.sync_allocation(db, w, now=120); db.commit()
        assert db.scalar(select(func.count()).select_from(ResourceAllocation)) == 1
        w.status = 'stopped'; w.runtime_id = ''; w.devices = []
        h.sync_allocation(db, w, now=200); db.commit()
        a = db.scalar(select(ResourceAllocation))
        assert a.started_at == 100 and a.ended_at == 200 and a.start_source == 'observed'
        assert a.snapshot['devices'] == ['GPU-0']
        w.runtime_id = 'runtime-2'; w.devices = ['GPU-1']; w.status = 'running'
        h.sync_allocation(db, w, started=True, now=300); db.commit()
        assert db.scalar(select(func.count()).select_from(ResourceAllocation)) == 2
        w.name = 'changed'; db.commit()
        assert a.snapshot['name'] == 'workspace-w'


@pytest.mark.parametrize('initial_runtime', ['', 'stale-runtime-from-failed-run'])
def test_reconcile_records_start_and_successful_release_without_changing_executor_calls(database, monkeypatch, initial_runtime):
    calls = []
    def execute(action, *args):
        calls.append(action)
        return {'status':'running', 'runtime_id':'created', 'devices':['GPU-0']} if action.endswith('ensure') else {'ok':True}
    monkeypatch.setattr(main, 'executor', execute)
    monkeypatch.setattr(main, 'validate_restart', lambda *args: None)
    with database() as db:
        w = ws(); w.node_id='n'; w.status='queued'; w.runtime_id=initial_runtime; db.add(w); db.commit()
    main.reconcile_workspace('w', 'p')
    with database() as db:
        a = db.scalar(select(ResourceAllocation)); assert a.start_source == 'started' and a.ended_at is None
        db.get(Workspace, 'w').status = 'deleting'; db.commit()
    main.reconcile_workspace('w', 'p')
    with database() as db:
        assert db.scalar(select(ResourceAllocation)).ended_at is not None
        assert db.get(Workspace, 'w').status == 'deleted'
    assert calls == ['workspace/ensure', 'workspace/remove']


def test_failed_removal_does_not_release_history_early(database, monkeypatch):
    with database() as db:
        w=ws(); db.add(w);db.flush();h.sync_allocation(db,w);w.status='stopping';db.commit()
    monkeypatch.setattr(main,'executor',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('offline')))
    main.reconcile_workspace('w','p')
    with database() as db:
        assert db.scalar(select(ResourceAllocation)).ended_at is None
        assert db.get(Workspace,'w').status=='stopping'


def seed(database, now):
    with database() as db:
        for w in [ws(), ws('w2', runtime='runtime2'), ws('w3', owner='v', project='q', runtime='private')]:
            db.add(w);db.flush();h.sync_allocation(db,w,started=True,now=now-7200)
        refs=[{'id':'w','owner_id':'u','username':'alice','project_id':'p','project_name':'research'},
              {'id':'w2','owner_id':'u','username':'alice','project_id':'p','project_name':'research'},
              {'id':'w3','owner_id':'v','username':'bob','project_id':'q','project_name':'private'}]
        for i,v in enumerate([0,80,None]):
            db.add(ResourceMetric(node_id='n', bucket=int(now//60)-i, collected_at=now-i*60,
                payload={'node_name':'B300','gpus':[{'uuid':'GPU-0','index':0,'name':'B300',
                         'utilization_percent':v,'memory_used_mib':20,'workspaces':refs}]}))
        db.commit()


def test_period_clipping_shared_hours_and_gpu_deduplication(database):
    now=time.time()-1;seed(database,now)
    with database() as db:
        r=h.report(db,db.get(User,'a'),now-3600,now+0.5)
        assert r['summary']['sessions']==3 and r['summary']['gpu_samples']==2
        assert r['summary']['gpu_avg_percent']==40
        assert r['users'][0]['gpu_samples']==2  # two same-user containers share one card
        assert 2.99 < r['summary']['shared_gpu_access_hours'] < 3.01
        assert r['summary']['exclusive_gpu_hours']==0
        assert r['gpus'][0]['gpu_memory_peak_mib']==20
        assert any(p['gpu_avg_percent'] is None for p in r['timeline'])


def test_user_and_manager_scope_applies_to_records_metrics_choices_and_exports(database):
    now=time.time()-1;seed(database,now)
    with database() as db:
        u=db.get(User,'u');r=h.report(db,u,now-3600,now+0.5)
        assert r['total']==2 and {x['name'] for x in r['users']}=={'alice'}
        assert [x['id'] for x in r['choices']['projects']]==['p']
        with pytest.raises(HTTPException):h.report(db,u,owner_id='v')
        with pytest.raises(HTTPException):h.report(db,u,project_id='q')
        db.add(Member(user_id='u',project_id='q',role='manager'));db.commit()
        r=h.report(db,u,now-3600,now+0.5,owner_id='v',project_id='q')
        assert r['total']==1 and r['users'][0]['name']=='bob'


def test_api_export_auth_csv_injection_pagination_and_time_validation(database):
    now=time.time()-1;seed(database,now)
    with database() as db:
        u=db.get(User,'u')
        a=db.scalar(select(ResourceAllocation).where(ResourceAllocation.owner_id=='u'))
        a.snapshot={**a.snapshot,'name':'=HYPERLINK("evil")'};db.commit()
    client=TestClient(main.app)
    assert client.get('/api/resource-history').status_code==401
    assert client.get('/api/resource-history/export').status_code==401
    main.app.dependency_overrides[main.auth.current_user]=lambda:u
    try:
        r=client.get('/api/resource-history',params={'page_size':1})
        assert r.status_code==200 and r.json()['total']==2 and len(r.json()['allocations'])==1
        assert '_all_allocations' not in r.json()
        for kind in ('allocations','users','projects','gpu'):
            r=client.get('/api/resource-history/export',params={'kind':kind})
            assert r.status_code==200 and r.content.startswith(b'\xef\xbb\xbf')
            assert 'bob' not in r.text and 'attachment;' in r.headers['content-disposition']
        assert "'=HYPERLINK" in client.get('/api/resource-history/export').text
        assert client.get('/api/resource-history',params={'start':'nan'}).status_code==422
        assert client.get('/api/resource-history',params={'start':1,'end':32*86400+2}).status_code==422
        assert client.get('/api/resource-history',params={'page':0}).status_code==422
        assert client.get('/api/resource-history/export',params={'kind':'bad'}).status_code==422
    finally:
        main.app.dependency_overrides.clear()


def test_collector_is_read_only_idempotent_and_marks_failures(database):
    now=time.time()
    with database() as db:db.add(ws());db.commit()
    calls=[]
    def execute(action,**kwargs):
        calls.append(action)
        return {'collected_at':now,'gpus':[{'uuid':'GPU-0','index':0,'utilization_percent':75,
            'allocations':[{'container_id':'runtime','workspace_id':'w'}],
            'processes':[{'container_id':'runtime','workspace_id':'w','pid':1,'command':'secret','memory_mib':10}]}]}
    h.collect_once(execute,now);h.collect_once(execute,now)
    with database() as db:
        assert db.scalar(select(func.count()).select_from(ResourceMetric))==1
        p=db.scalar(select(ResourceMetric)).payload
        assert 'secret' not in str(p) and p['gpus'][0]['workspaces'][0]['username']=='alice'
    assert calls==['metrics']
    h.collect_once(lambda *a,**k: (_ for _ in ()).throw(RuntimeError()),now+60)
    with database() as db:
        p=list(db.scalars(select(ResourceMetric).order_by(ResourceMetric.bucket)))[-1].payload
        assert p['gpu_error'] and p['gpus']==[]


def test_retention_only_removes_old_metrics_and_preserves_allocations(database):
    now=(int(time.time()//3600)+1)*3600
    with database() as db:
        w=ws();db.add(w);db.flush();h.sync_allocation(db,w,now=now-40*86400)
        db.add(ResourceMetric(node_id='old',bucket=int(now//60)-31*1440,collected_at=now-31*86400,payload={}))
        db.commit()
    h.collect_once(lambda *a,**k:{'collected_at':now,'gpus':[]},now)
    with database() as db:
        assert db.scalar(select(func.count()).select_from(ResourceAllocation))==1
        assert db.scalar(select(func.count()).select_from(ResourceMetric))==1

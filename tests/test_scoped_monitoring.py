"""Developers see every task on their allocated cards, without access to other cards."""
import copy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main,auth
from app.models import Base,User,Project,Member,Node,Workspace
from app.monitoring import command_text


@pytest.fixture
def portal(monkeypatch):
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine);factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(main,'Session',factory)
    with factory() as db:
        db.add_all([User(id='u',username='alice',role='developer'),User(id='b',username='bob',role='developer'),
                    User(id='a',username='admin',role='admin'),User(id='v',username='auditor',role='auditor'),
                    User(id='empty',username='unallocated',role='developer'),Project(id='p',name='Alice project'),Project(id='q',name='Bob project'),
                    Member(project_id='p',user_id='u',role='developer'),Member(project_id='q',user_id='b',role='developer'),
                    Node(id='primary',name='Node A',ip='10.0.0.1',enabled=False,project_ids=[]),
                    Node(id='second',name='Node B',ip='10.0.0.2',project_ids=[]),
                    Workspace(id='own',name='Alice dev',owner_id='u',project_id='p',node_id='primary',gpu=2,gpu_mode='shared',
                              status='running',runtime_id='runtime-own',devices=['GPU-hidden'],request_key='own'),
                    Workspace(id='other',name='Bob dev',owner_id='b',project_id='q',node_id='primary',gpu=2,
                              status='running',runtime_id='runtime-other',request_key='other')]);db.commit()
    def allocation(cid,wid):return {'container_id':cid,'workspace_id':wid,'container':cid}
    def process(pid,cid,wid,command):
        return {**allocation(cid,wid),'pid':pid,'user':'root','user_uid':0,'command':command,'memory_mib':10}
    metrics={'collected_at':1,'cpu':{'cores':64,'used_percent':30},'memory':{'used_percent':50},'storage':[],
             'allocation_error':None,'process_error':None,'gpus':[
                 {'index':0,'uuid':'GPU-shared','utilization_percent':90,
                  'allocations':[allocation('runtime-own','own'),allocation('runtime-other','other'),allocation('external',None)],
                  'processes':[process(100,'runtime-own','own','python mine.py'),process(200,'runtime-other','other','python shared.py'),
                               process(300,'external',None,command_text(['python','external.py','--token','credential-hidden']))]},
                 {'index':1,'uuid':'GPU-hidden','utilization_percent':50,
                  'allocations':[allocation('runtime-other','other'),allocation('spoofed-runtime','own')],
                  'processes':[process(400,'runtime-other','other','private-unallocated-card-job')]}]}
    calls=[]
    def rpc(action,payload=None,node_id='primary'):
        assert action=='metrics';calls.append(node_id)
        return metrics if node_id=='primary' else {'collected_at':1,'gpus':[{'index':9,'uuid':'other-node-private','processes':[],'allocations':[]}]}
    monkeypatch.setattr(main,'executor',rpc)
    def client(uid='u'):
        with factory() as db:user=db.get(User,uid)
        main.app.dependency_overrides[auth.current_user]=lambda:user
        return TestClient(main.app)
    yield factory,client,metrics,calls
    main.app.dependency_overrides.clear();engine.dispose()


def test_developer_sees_own_shared_card_and_all_its_tasks_without_leaking_other_cards(portal):
    factory,client,metrics,calls=portal;original=copy.deepcopy(metrics)
    response=client().get('/api/monitoring');assert response.status_code==200
    result=response.json();assert result['scope']=='allocated' and result['refresh_seconds']==5
    assert [n['id'] for n in result['nodes']]==['primary'] and calls==['primary']
    sample=result['nodes'][0]['metrics'];assert [g['uuid'] for g in sample['gpus']]==['GPU-shared']
    assert [p['pid'] for p in sample['gpus'][0]['processes']]==[100,200,300]
    assert sample['gpus'][0]['processes'][1]['workspace']['username']=='bob'
    assert sample['gpus'][0]['processes'][2]['workspace'] is None
    assert 'credential-hidden' not in response.text and 'external.py' in response.text
    assert 'private-unallocated-card-job' not in response.text and 'GPU-hidden' not in response.text
    assert 'other-node-private' not in response.text
    assert [w['id'] for w in sample['workspaces']]==['own']
    assert [p['pid'] for p in sample['workspaces'][0]['processes']]==[100]
    assert [g['uuid'] for g in sample['workspaces'][0]['devices']]==['GPU-shared']
    assert sample['cpu']['used_percent']==30 and not result['nodes'][0]['enabled']
    assert metrics==original,'A user-specific response must not mutate the node sample cache'


@pytest.mark.parametrize('mutation',['wrong-runtime','wrong-label','missing-allocation','allocation-error'])
def test_actual_runtime_allocation_is_required_and_missing_data_fails_closed(portal,mutation):
    factory,client,metrics,calls=portal
    if mutation=='wrong-runtime':metrics['gpus'][0]['allocations'][0]['container_id']='old-runtime'
    elif mutation=='wrong-label':metrics['gpus'][0]['allocations'][0]['workspace_id']='not-own'
    elif mutation=='missing-allocation':metrics['gpus'][0]['allocations']=[]
    else:metrics['allocation_error']='Allocation sample unavailable'
    result=client().get('/api/monitoring').json();sample=result['nodes'][0]['metrics']
    assert sample['gpus']==[] and sample['workspaces'][0]['devices']==[]
    assert all(not w['processes'] for w in sample['workspaces'])
    assert 'private-unallocated-card-job' not in json.dumps(result) and 'shared.py' not in json.dumps(result)


@pytest.mark.parametrize('status',['pending_approval','queued','stopped','rejected','deleted'])
def test_non_running_environments_do_not_grant_gpu_visibility(portal,status):
    factory,client,metrics,calls=portal
    with factory() as db:db.get(Workspace,'own').status=status;db.commit()
    assert client().get('/api/monitoring').json()['nodes']==[] and calls==[]


@pytest.mark.parametrize('restriction',['no-runtime','membership-removed','node-project-denied'])
def test_stale_or_revoked_access_cannot_read_node_metrics(portal,restriction):
    factory,client,metrics,calls=portal
    with factory() as db:
        if restriction=='no-runtime':db.get(Workspace,'own').runtime_id=''
        elif restriction=='membership-removed':db.delete(db.scalar(select(Member).where(Member.user_id=='u')))
        else:db.get(Node,'primary').project_ids=['q']
        db.commit()
    assert client().get('/api/monitoring').json()['nodes']==[] and calls==[]


def test_unallocated_user_gets_empty_view_and_auditor_remains_forbidden(portal):
    factory,client,metrics,calls=portal
    result=client('empty').get('/api/monitoring');assert result.status_code==200 and result.json()['nodes']==[]
    assert client('v').get('/api/monitoring').status_code==403 and calls==[]


def test_cpu_only_environment_does_not_grant_access_to_gpu_tasks(portal):
    factory,client,metrics,calls=portal
    with factory() as db:db.get(Workspace,'own').gpu=0;db.commit()
    sample=client().get('/api/monitoring').json()['nodes'][0]['metrics']
    assert sample['gpus']==[] and sample['workspaces']==[] and sample['cpu']['cores']==64


def test_admin_keeps_all_nodes_and_cards_after_developer_request(portal):
    factory,client,metrics,calls=portal
    client().get('/api/monitoring');calls.clear()
    result=client('a').get('/api/monitoring').json()
    assert result['scope']=='all' and set(calls)=={'primary','second'}
    assert len(result['nodes'])==2 and 'private-unallocated-card-job' in json.dumps(result)
    assert 'other-node-private' in json.dumps(result)


def test_idle_allocation_and_process_collection_failure_keep_assigned_cards(portal):
    factory,client,metrics,calls=portal
    metrics['gpus'][0]['processes']=[];metrics['process_error']='Process collection unavailable'
    result=client().get('/api/monitoring').json()['nodes'][0]['metrics']
    assert [g['uuid'] for g in result['gpus']]==['GPU-shared']
    assert len(result['workspaces'][0]['devices'])==1 and result['workspaces'][0]['process_memory_mib'] is None

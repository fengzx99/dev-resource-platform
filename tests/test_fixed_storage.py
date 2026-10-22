"""Storage policy regressions: automatic bindings, legacy compatibility and fail closed."""
import time
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main, executor
from app.models import Base, Node, Project, Member, User, Image, Flavor, Volume, Workspace
from app.storage import SHARE_ID, IP_ROOTS, source_path, home_path, fixed_targets
from app.policy import validate_volume_mount, validate_mount


@pytest.fixture
def database(monkeypatch):
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine);factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(main,'Session',factory)
    monkeypatch.setattr(main,'choose',lambda db,u,nid,pid:db.get(Node,nid))
    with factory() as db:
        db.add_all([User(id='u',username='alice',role='developer'),User(id='v',username='bob',role='developer'),
                    Node(id='primary',name='server003',ip='172.16.9.20',engine_id='a',storage_id='shared'),
                    Node(id='second',name='server002',ip='172.16.9.11',engine_id='b',storage_id='shared'),
                    Image(id='im',name='image',digest='sha256:image',status='ready',enabled=True),
                    Flavor(id='fl',name='small',cpu=1,memory_gib=1,gpu=0,enabled=True)])
        for pid,owner in [('p','u'),('q','v')]:
            quota={'cpu':8,'memory_gib':32,'gpu':0,'storage_gib':0,'workspaces':8}
            db.add(Project(id=pid,name=pid,quota=quota,approval=True))
            db.add(Member(project_id=pid,user_id=owner,role='developer',quota=quota))
        db.commit()
    yield factory
    engine.dispose()


def request(**kwargs):
    key=Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.OpenSSH,PublicFormat.OpenSSH).decode()
    return main.NewWorkspace(**({'name':'My dev','project_id':'p','image_id':'im','flavor_id':'fl',
                                'public_key':key,'request_key':'request-abcdefgh1234'}|kwargs))


def test_automatic_binding_cross_project_global_share_and_idempotency(database,monkeypatch):
    calls=[]
    def rpc(action,payload=None,node_id='primary'):
        calls.append((action,payload,node_id));return {'persistent_home':True,'home_path':'/root','login_user':'root','quota_enforced':False}
    monkeypatch.setattr(main,'executor',rpc)
    with database() as db:
        first=main.add_workspace(request(),db.get(User,'u'),db)
        assert first['status']=='pending_approval'
        assert [m['path'] for m in first['mounts']]==['/data','/root','/share']
        assert all(m['readonly'] is False for m in first['mounts'])
        data=db.get(Volume,first['mounts'][0]['volume_id']);home=db.get(Volume,first['mounts'][1]['volume_id'])
        assert source_path(data.kind,data.binding).startswith('/mnt/server003/user_data/My-dev-')
        assert data.binding['directory']==home.binding['directory']
        assert data.elastic and data.size_gib==0 and data.owner_id=='u' and data.access=='private'
        previous=len(calls)
        assert main.add_workspace(request(),db.get(User,'u'),db)['id']==first['id']
        assert len(calls)==previous
        second=main.add_workspace(request(project_id='q',node_id='second'),db.get(User,'v'),db)
        other=db.get(Volume,second['mounts'][0]['volume_id'])
        assert source_path(other.kind,other.binding).startswith('/mnt/server002/user_data/')
        assert first['mounts'][2]['volume_id']==second['mounts'][2]['volume_id']==SHARE_ID
        global_v=db.get(Volume,SHARE_ID)
        assert global_v.project_id is None and global_v.access=='global_rw'
        main.can_volume(db,db.get(User,'v'),global_v,'q',True)
        with pytest.raises(HTTPException):main.can_volume(db,db.get(User,'v'),data,'q',True)
        assert SHARE_ID in {v['id'] for v in main.volumes(db.get(User,'v'),db)}
        assert data.id not in {v['id'] for v in main.volumes(db.get(User,'v'),db)}
        assert len(db.scalars(select(Volume)).all())==5
        with pytest.raises(HTTPException):main.delete_volume(data.id,data.name,db.get(User,'u'),db)
        with pytest.raises(HTTPException):main.edit_volume(data.id,{},db.get(User,'u'),db)
        w=db.get(Workspace,first['id']);w.status='stopped';w.runtime_id='';db.commit()
        with pytest.raises(HTTPException):main.move_node(w.id,{'node_id':'second'},db.get(User,'u'),db)


def test_custom_mount_rejected_before_storage_mutation(database,monkeypatch):
    monkeypatch.setattr(main,'executor',lambda *a,**k:pytest.fail('must reject before RPC'))
    with database() as db:
        with pytest.raises(HTTPException) as err:
            main.add_workspace(request(mounts=[{'volume_id':'other','path':'/data','readonly':False}]),db.get(User,'u'),db)
        assert err.value.status_code==422
        assert db.scalars(select(Volume)).all()==[]


def test_root_home_and_legacy_home_keep_distinct_bindings():
    for path in ('/root','/home/developer'):
        validate_volume_mount('user_home',path,'private',False)
        assert home_path([{'path':path}])==path
        assert home_path([{'Destination':path}])==path
        assert fixed_targets([{'path':path}])=={'/data',path,'/share'}
        with pytest.raises(HTTPException):validate_volume_mount('generic',path,'private',False)
        with pytest.raises(HTTPException):validate_volume_mount('user_home',path,'private',True)
    with pytest.raises(HTTPException):validate_mount('/root/.ssh')


def test_old_image_cannot_start_new_root_home_environment(database,monkeypatch):
    calls=[]
    def rpc(action,payload=None,node_id='primary'):
        calls.append(action)
        return {'persistent_home':True,'home_path':'/home/developer'}
    monkeypatch.setattr(main,'executor',rpc)
    with database() as db:
        with pytest.raises(HTTPException) as err:main.add_workspace(request(),db.get(User,'u'),db)
        assert err.value.status_code==422 and db.scalars(select(Volume)).all()==[]
    assert calls==['image/inspect']


def test_fixed_expiry_keeps_directory_bindings(database,monkeypatch):
    calls=[]
    monkeypatch.setattr(main,'executor',lambda action,payload,node_id:calls.append(action) or {'ok':True})
    with database() as db:
        db.add(Volume(id='fixed',project_id='p',owner_id='u',kind='user_data',status='ready',size_gib=0,elastic=True,
                      binding={'mode':'ip_fixed','node_ip':'172.16.9.20','directory':'retained'}))
        db.add(Workspace(id='expired',name='expired',project_id='p',owner_id='u',node_id='primary',cpu=1,memory_gib=1,gpu=0,
                         status='running',runtime_id='runtime',expires=time.time()-1,request_key='expired',
                         mounts=[{'volume_id':'fixed','path':'/data','readonly':False}]))
        db.commit()
    main.reconcile_workspace('expired','p')
    with database() as db:
        assert db.get(Workspace,'expired').status=='stopped'
        assert db.get(Volume,'fixed').binding['directory']=='retained'
    assert calls==['workspace/remove']


def test_executor_pins_node_rejects_symlinks_and_preserves_existing_modes(monkeypatch,tmp_path):
    monkeypatch.setitem(IP_ROOTS,'172.16.9.20',str(tmp_path))
    monkeypatch.setattr(executor,'fixed_storage_check',lambda:{'ready':True})
    monkeypatch.setattr(executor,'cfg',lambda:{'host_ip':'172.16.9.20'})
    monkeypatch.setattr(executor.os,'chown',lambda *a:None)
    body={'id':'abc','kind':'user_home','access':'private','elastic':True,'size_gib':0,
          'binding':{'mode':'ip_fixed','node_ip':'172.16.9.20','directory':'space'}}
    executor.volume_ensure(body)
    path=tmp_path/'user_home/space';assert path.is_dir()
    (path/'retained').write_text('keep');path.chmod(0o750)
    executor.volume_ensure(body)
    assert (path/'retained').read_text()=='keep' and path.stat().st_mode&0o777==0o750
    with pytest.raises(HTTPException):executor.bound_path(body|{'binding':body['binding']|{'node_ip':'172.16.9.11'}})
    with pytest.raises(HTTPException):executor.bound_path(body|{'binding':body['binding']|{'directory':'../../etc'}})
    (tmp_path/'user_home/link').symlink_to(path)
    with pytest.raises(HTTPException):executor.bound_path(body|{'binding':body['binding']|{'directory':'link'}})
    with pytest.raises(HTTPException):executor.volume_remove(body)


@pytest.mark.parametrize('mount_line', ['', '1 0 0:1 / /mnt/server003 rw - nfs4 172.16.9.99:/data-local rw'])
def test_missing_or_wrong_host_mount_fails_closed(monkeypatch,mount_line):
    monkeypatch.setattr(executor,'require_storage',lambda:None)
    monkeypatch.setattr(executor,'cfg',lambda:{'host_ip':'172.16.9.20'})
    monkeypatch.setattr(Path,'read_text',lambda *a,**k:mount_line)
    with pytest.raises(HTTPException) as err:executor.fixed_storage_check()
    assert err.value.status_code==503

"""Catalog removal preserves runtime images and enforces platform-admin access."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main,auth
from app.models import Base,User,Image,Workspace,Project,Member,Audit


@pytest.fixture
def portal(monkeypatch):
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine);factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(main,'Session',factory)
    monkeypatch.setattr(main,'executor',lambda *a,**k:pytest.fail('Catalog removal must never call the executor'))
    with factory() as db:
        db.add_all([User(id='admin',username='admin',role='admin'),User(id='developer',username='developer',role='developer'),
                    User(id='auditor',username='auditor',role='auditor'),User(id='manager',username='manager',role='developer'),
                    Project(id='p',name='Project'),Member(project_id='p',user_id='manager',role='manager'),
                    Image(id='im',name='Ubuntu <test>',reference='local/workspace:v1',digest='sha256:immutable',status='ready',enabled=True)])
        db.commit()
    def client(role='admin'):
        with factory() as db:user=db.get(User,role)
        main.app.dependency_overrides[auth.current_user]=lambda:user
        return TestClient(main.app,base_url=auth.PUBLIC,headers={'Origin':auth.PUBLIC,'X-DRP-Request':'1'})
    yield factory,client
    main.app.dependency_overrides.clear();engine.dispose()


@pytest.mark.parametrize('role',['developer','auditor','manager'])
def test_non_admin_cannot_remove_even_with_direct_api_request(portal,role):
    factory,client=portal
    assert client(role).delete('/api/images/im',params={'confirm':'Ubuntu <test>'}).status_code==403
    with factory() as db:
        assert db.get(Image,'im').status=='ready' and db.get(Image,'im').enabled
        assert db.scalars(select(Audit)).all()==[]


def test_confirmed_removal_retains_deleted_history_and_audit(portal):
    factory,client=portal;c=client()
    with factory() as db:
        db.add(Workspace(id='history',image_id='im',image_ref='sha256:immutable',status='deleted',runtime_id='old-runtime'))
        db.commit()
    assert c.delete('/api/images/im').status_code==422
    assert c.delete('/api/images/im',params={'confirm':'wrong'}).status_code==422
    assert c.get('/api/images').json()[0]['workspace_count']==0
    assert c.delete('/api/images/im',params={'confirm':'Ubuntu <test>'}).json()=={'ok':True}
    assert c.get('/api/images').json()==[]
    assert c.patch('/api/images/im',json={'enabled':True}).status_code==404
    assert c.delete('/api/images/im',params={'confirm':'Ubuntu <test>'}).status_code==404
    for role in ('developer','auditor'):
        assert client(role).get('/api/images').json()==[]
    with factory() as db:
        image=db.get(Image,'im');history=db.get(Workspace,'history')
        assert image.status=='removed' and not image.enabled and image.digest=='sha256:immutable'
        assert history.image_id=='im' and history.image_ref==image.digest and history.runtime_id=='old-runtime'
        audit=db.scalar(select(Audit).where(Audit.action=='image.remove'))
        assert audit.actor_id=='admin' and audit.object_id=='im'
        assert audit.detail=={'name':'Ubuntu <test>','reference':'local/workspace:v1','digest':'sha256:immutable'}


@pytest.mark.parametrize('status',['running','stopped','pending_approval','queued','starting','stopping','failed','deleting','rejected'])
def test_existing_workspace_blocks_removal_without_mutation(portal,status):
    factory,client=portal
    with factory() as db:
        db.add(Workspace(id='w',image_id='im',status=status,runtime_id='unchanged-runtime'));db.commit()
    c=client()
    assert c.get('/api/images').json()[0]['workspace_count']==1
    result=c.delete('/api/images/im',params={'confirm':'Ubuntu <test>'})
    assert result.status_code==409 and '1 个开发环境' in result.json()['detail']
    with factory() as db:
        assert db.get(Image,'im').status=='ready' and db.get(Image,'im').enabled
        assert db.get(Workspace,'w').status==status and db.get(Workspace,'w').runtime_id=='unchanged-runtime'
        assert db.scalars(select(Audit)).all()==[]


@pytest.mark.parametrize('status',['pending','building'])
def test_in_progress_preparation_cannot_race_removal(portal,status):
    factory,client=portal
    with factory() as db:db.get(Image,'im').status=status;db.commit()
    assert client().delete('/api/images/im',params={'confirm':'Ubuntu <test>'}).status_code==409
    with factory() as db:assert db.get(Image,'im').status==status


def test_removed_image_cannot_be_used_for_new_workspace(portal):
    factory,client=portal
    c=client();assert c.delete('/api/images/im',params={'confirm':'Ubuntu <test>'}).status_code==200
    response=c.post('/api/workspaces',json={'name':'New workspace','project_id':'p','image_id':'im','flavor_id':'fl',
                                           'public_key':'ssh-ed25519 '+'A'*64,'request_key':'image-remove-test-request'})
    assert response.status_code==403 and response.json()['detail']=='镜像未就绪或未授权'
    with factory() as db:assert db.scalars(select(Workspace)).all()==[]

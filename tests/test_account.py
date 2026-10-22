import hashlib
import time
from urllib.parse import urlparse,parse_qs
import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main,executor,auth
from app.models import Base,User,LoginSession,Workspace
from app.storage import login_user


@pytest.fixture
def database(monkeypatch):
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine);factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(main,'Session',factory)
    monkeypatch.setattr(auth,'config',lambda:{'session_secret':'s'*64,'oidc_secret':'test-secret'})
    with factory() as db:
        db.add_all([User(id='u',username='developer',role='developer',enabled=True),User(id='a',username='admin',role='admin',enabled=True),
                    LoginSession(id='old-user-session',user_id='u',expires=time.time()+300),
                    LoginSession(id='admin-session',user_id='a',expires=time.time()+300)])
        db.commit()
    yield factory
    main.app.dependency_overrides.clear();engine.dispose()


def test_developer_password_action_and_callback_only_revoke_own_sessions(database,monkeypatch):
    with database() as db:user=db.get(User,'u')
    main.app.dependency_overrides[auth.current_user]=lambda:user
    client=TestClient(main.app,follow_redirects=False,base_url=auth.PUBLIC)
    response=client.get('/account/password');assert response.status_code==307
    params=parse_qs(urlparse(response.headers['location']).query)
    assert params['kc_action']==['UPDATE_PASSWORD'] and params['login_hint']==['developer']
    assert params['prompt']==['login'] and params['code_challenge_method']==['S256']
    saved=jwt.decode(response.cookies['drp_login'],'s'*64,algorithms=['HS256'])
    assert saved['action']=='UPDATE_PASSWORD'
    class Reply:
        def raise_for_status(self):pass
        def json(self):return {'id_token':'signed-token'}
    monkeypatch.setattr(main.httpx,'post',lambda *a,**k:Reply())
    monkeypatch.setattr(auth,'claims',lambda t:{'sub':'u','nonce':saved['nonce'],'preferred_username':'developer'})
    response=client.get('/callback',params={'code':'code','state':saved['state'],'kc_action_status':'success'})
    assert response.status_code==307 and response.headers['location']=='/#account'
    with database() as db:
        assert db.get(LoginSession,'old-user-session') is None
        assert db.get(LoginSession,'admin-session') is not None
        cookie=response.cookies['drp_session']
        assert db.get(LoginSession,hashlib.sha256(cookie.encode()).hexdigest()).user_id=='u'


def test_admin_reset_only_target_sessions_and_never_stops_tasks(database,monkeypatch):
    calls=[];monkeypatch.setattr(auth,'kc_admin',lambda *a:calls.append(a))
    with database() as db:
        user=db.get(User,'u');admin=db.get(User,'a')
        with pytest.raises(HTTPException) as error:main.reset_password('a',user,db)
        assert error.value.status_code==403 and calls==[]
        db.add(Workspace(id='w',name='training',owner_id='u',project_id='p',status='running',runtime_id='same-runtime',
                         node_id='primary',cpu=1,memory_gib=1,gpu=1,request_key='w'))
        db.commit()
        result=main.reset_password('u',admin,db)
        assert result['username']=='developer' and len(result['initial_password'])>=18
        assert db.get(LoginSession,'old-user-session') is None and db.get(LoginSession,'admin-session') is not None
        assert db.get(Workspace,'w').status=='running' and db.get(Workspace,'w').runtime_id=='same-runtime'
    assert [(x[0],x[1]) for x in calls]==[('PUT','users/u/reset-password'),('POST','users/u/logout')]


@pytest.mark.parametrize('labels,uid,username',[({'io.devresources.login-user':'root'},'0:0','root'),({},'1000:1000','developer')])
def test_console_identity_and_legacy_compatibility(monkeypatch,labels,uid,username):
    calls=[]
    class Box:
        status='running'
        attrs={'Config':{'Labels':labels},'Mounts':[{'Destination':'/root'}]}
        def exec_run(self,*args,**kwargs):
            calls.append(kwargs)
            return type('Result',(),{'exit_code':0,'output':b'ok'})()
    monkeypatch.setattr(executor,'get_workspace',lambda wid:Box())
    assert executor.workspace_exec({'id':'w','command':'whoami'})['exit_code']==0
    assert calls[0]['user']==uid and calls[0]['environment']['USER']==username
    assert login_user(labels)==username

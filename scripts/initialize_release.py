"""Initialize fresh deployment via the supported APIs; never creates a workspace."""
import argparse,json,pathlib,subprocess
ROOT=pathlib.Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--prefix',default='drp');a=p.parse_args()
    metadata=json.loads((ROOT/'RELEASE.json').read_text())
    source='IMAGE_REF='+repr(metadata['workspace_image'])+'\n'+'''
import json,time,httpx
from app import auth
cfg=auth.config()
def login(username):
    user=next(u for u in cfg['users'] if u['username']==username)
    r=httpx.post(auth.KC+'/realms/dev-resources/protocol/openid-connect/token',data={'grant_type':'password','client_id':auth.CLIENT,'client_secret':cfg['oidc_secret'],'username':username,'password':user['password']},timeout=20)
    r.raise_for_status();return r.json()['access_token']
def call(token,method,path,body=None):
    r=httpx.request(method,cfg['public_url']+'/api'+path,headers={'Authorization':'Bearer '+token},json=body,timeout=30,verify=__import__('ssl').create_default_context(cafile='/run/secrets/server.crt'))
    if r.status_code>=400:raise RuntimeError('Initialization API '+path+' HTTP '+str(r.status_code))
    return r.json()
admin=login('admin');developer=login('developer')
u=call(developer,'GET','/me');call(admin,'GET','/me')
if call(admin,'GET','/workspaces') or call(admin,'GET','/images') or call(admin,'GET','/flavors') or call(admin,'GET','/overview')['projects']:
    raise RuntimeError('Initialization requires an empty application database')
quota={'cpu':8,'memory_gib':32,'gpu':1,'storage_gib':1000,'workspaces':3}
project=call(admin,'POST','/projects',{'name':'开发资源项目','quota':quota,'approval':False})
call(admin,'PUT','/projects/'+project['id']+'/members',{'user_id':u['id'],'role':'developer','quota':quota})
image=call(admin,'POST','/images',{'name':'Ubuntu 24.04 · root 登录 · 三类持久存储','reference':IMAGE_REF})
for name,cpu,mem,gpu in [('轻量开发 · 2C4G',2,4,0),('GPU 开发 · 8C32G1卡',8,32,1)]:
    call(admin,'POST','/flavors',{'name':name,'cpu':cpu,'memory_gib':mem,'gpu':gpu})
for attempt in range(150):
    im=next(i for i in call(admin,'GET','/images') if i['id']==image['id'])
    if im['status']=='ready':break
    if im['status']=='failed':raise RuntimeError('Workspace image validation failed')
    if attempt%15==0:print('Waiting for workspace catalog validation…',flush=True)
    time.sleep(2)
else:raise RuntimeError('Workspace image registration timed out')
assert im['build_config']['home_path']=='/root' and im['build_config']['login_user']=='root'
assert call(admin,'GET','/nodes')[0]['fixed_storage_ready']
# Keycloak default roles may grant account access through composites. Check
# effective roles, and repair only missing account roles on this fresh instance.
r=httpx.post(auth.KC+'/realms/master/protocol/openid-connect/token',data={'grant_type':'password','client_id':'admin-cli','username':'drp-bootstrap','password':cfg['keycloak_admin_password']},timeout=20)
r.raise_for_status();master=r.json()['access_token']
def identity(method,path,body=None):
    r=httpx.request(method,auth.KC+'/admin/realms/dev-resources/'+path,headers={'Authorization':'Bearer '+master},json=body,timeout=20)
    r.raise_for_status();return r.json() if r.content else None
account=identity('GET','clients?clientId=account')
assert len(account)==1 and account[0]['enabled']
client_id=account[0]['id']
required=[identity('GET','clients/'+client_id+'/roles/'+name) for name in ('manage-account','view-profile')]
for username in ('admin','developer'):
    rows=identity('GET','users?username='+username+'&exact=true')
    path='users/'+rows[0]['id']+'/role-mappings/'
    before=identity('GET',path+'realm')
    effective=identity('GET',path+'clients/'+client_id+'/composite')
    missing=[role for role in required if role['name'] not in {x['name'] for x in effective}]
    if missing:identity('POST',path+'clients/'+client_id,missing)
    effective=identity('GET',path+'clients/'+client_id+'/composite')
    assert {'manage-account','view-profile'}<={x['name'] for x in effective},'Missing account self-service access'
    assert {x['id'] for x in identity('GET',path+'realm')}=={x['id'] for x in before},'Business roles changed'
print(json.dumps({'initialization':'PASS','accounts_login':2,'image_ready':True,'root_login':True,'fixed_storage_ready':True,'workspaces_created':0},ensure_ascii=False))
'''
    subprocess.run(['docker','exec','-i',a.prefix+'-portal','python','-'],input=source,text=True,check=True)


if __name__=='__main__':main()

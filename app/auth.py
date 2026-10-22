import hashlib, json, os, secrets, time
from pathlib import Path
from urllib.parse import urlencode
import httpx, jwt
from fastapi import HTTPException, Request
from .models import Session, User, LoginSession

PUBLIC=os.environ.get('PUBLIC_URL','https://172.16.9.20:8420').rstrip('/')
KC=os.environ.get('KEYCLOAK_URL','http://keycloak:8080/auth').rstrip('/')
REALM='dev-resources'
CLIENT='resource-portal'
ISSUER=PUBLIC+'/auth/realms/'+REALM
SECRET_FILE=os.environ.get('SECRETS_FILE','/run/secrets/platform.json')

def config(): return json.loads(Path(SECRET_FILE).read_text())
_jwks=(0,{})
def claims(token):
    global _jwks
    try:
        kid=jwt.get_unverified_header(token)['kid']
        if time.time()-_jwks[0]>300 or kid not in _jwks[1]:
            r=httpx.get(f'{KC}/realms/{REALM}/protocol/openid-connect/certs',timeout=10);r.raise_for_status()
            # Keycloak also publishes RSA-OAEP encryption keys. Only signature keys
            # for the explicitly allowed algorithm belong in the verifier cache.
            _jwks=(time.time(),{k['kid']:jwt.PyJWK.from_dict(k).key for k in r.json()['keys'] if k.get('use')=='sig' and k.get('alg')=='RS256'})
        return jwt.decode(token,_jwks[1][kid],algorithms=['RS256'],audience=CLIENT,issuer=ISSUER,options={'require':['exp','sub','iss','aud']})
    except Exception:
        raise HTTPException(401,'登录凭证无效或已过期')

def ensure_user(db,c):
    u=db.get(User,c['sub'])
    if not u:
        roles=c.get('realm_access',{}).get('roles',[])
        role='admin' if 'platform-admin' in roles else ('auditor' if 'auditor' in roles else 'developer')
        u=User(id=c['sub'],username=c.get('preferred_username',c['sub']),role=role,enabled=True);db.add(u);db.commit()
    return u

def current_user(req:Request):
    with Session() as db:
        bearer=req.headers.get('Authorization','')
        if bearer.startswith('Bearer '): u=ensure_user(db,claims(bearer[7:]))
        else:
            cookie=req.cookies.get('drp_session','')
            s=db.get(LoginSession,hashlib.sha256(cookie.encode()).hexdigest()) if cookie else None
            if not s or s.expires<time.time(): raise HTTPException(401,'请先登录')
            u=db.get(User,s.user_id)
        if not u or not u.enabled: raise HTTPException(403,'账号已停用')
        return u

def admin_token():
    cfg=config()
    r=httpx.post(f'{KC}/realms/{REALM}/protocol/openid-connect/token',data={'grant_type':'client_credentials','client_id':CLIENT,'client_secret':cfg['oidc_secret']},timeout=15)
    r.raise_for_status(); return r.json()['access_token']

def kc_admin(method,path,payload=None):
    r=httpx.request(method,f'{KC}/admin/realms/{REALM}/{path}',headers={'Authorization':'Bearer '+admin_token()},json=payload,timeout=20)
    if r.status_code>=400: raise HTTPException(502,'身份服务操作失败：'+str(r.status_code))
    return r

def create_identity(username,password,role):
    r=kc_admin('POST','users',{'username':username,'enabled':True,'emailVerified':True,'firstName':username,'lastName':'User','email':username+'@local.invalid','credentials':[{'type':'password','value':password,'temporary':False}]})
    user_id=r.headers['location'].rsplit('/',1)[1]
    if role in ('admin','auditor'):
        krole=kc_admin('GET','roles/'+('platform-admin' if role=='admin' else 'auditor')).json()
        kc_admin('POST',f'users/{user_id}/role-mappings/realm',[krole])
    return user_id

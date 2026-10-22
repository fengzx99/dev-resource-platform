import json,time
import jwt,pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives.asymmetric import rsa
from app import auth

def test_keycloak_mixed_signing_encryption_jwks(monkeypatch):
    signing=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    pub=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key()))
    jwks={'keys':[{**pub,'kid':'encryption','alg':'RSA-OAEP','use':'enc'},{**pub,'kid':'signature','alg':'RS256','use':'sig'}]}
    class Reply:
        def raise_for_status(self):pass
        def json(self):return jwks
    monkeypatch.setattr(auth.httpx,'get',lambda *a,**k:Reply())
    monkeypatch.setattr(auth,'_jwks',(0,{}))
    claims={'sub':'test-user','aud':auth.CLIENT,'iss':auth.ISSUER,'exp':time.time()+60}
    token=jwt.encode(claims,signing,algorithm='RS256',headers={'kid':'signature'})
    assert auth.claims(token)['sub']=='test-user'
    for field,value in [('aud','another-app'),('iss','https://attacker.invalid'),('exp',time.time()-60)]:
        bad=jwt.encode({**claims,field:value},signing,algorithm='RS256',headers={'kid':'signature'})
        with pytest.raises(HTTPException) as e:auth.claims(bad)
        assert e.value.status_code==401

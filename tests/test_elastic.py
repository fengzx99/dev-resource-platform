from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import executor


def test_elastic_directory_skips_quota_but_fixed_volume_still_enforces_it(monkeypatch,tmp_path):
    monkeypatch.setattr(executor,'volume_path',lambda vid,kind:tmp_path/vid)
    monkeypatch.setattr(executor.os,'chown',lambda *args:None)
    quotas=[];sleeps=[]
    monkeypatch.setattr(executor,'set_quota',lambda *args:quotas.append(args))
    monkeypatch.setattr(executor.time,'sleep',lambda seconds:sleeps.append(seconds))
    result=executor.volume_ensure({'id':'elastic','kind':'user_data','size_gib':0,'elastic':True})
    assert not result['quota_enforced'] and (tmp_path/'elastic').is_dir()
    assert quotas==[] and sleeps==[]
    result=executor.volume_ensure({'id':'fixed','kind':'user_data','size_gib':20})
    assert result['quota_enforced'] and quotas==[('fixed',20,'user_data')] and sleeps==[15]
    with pytest.raises(HTTPException):executor.volume_ensure({'id':'bad','size_gib':0})
    assert not (tmp_path/'bad').exists()

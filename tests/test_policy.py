from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.policy import check_quota, validate_mount, quota_used

def test_exact_quota_and_over_limit():
    check_quota({'gpu':2},{'gpu':1},{'gpu':1})
    with pytest.raises(HTTPException) as e:check_quota({'gpu':2},{'gpu':1},{'gpu':2})
    assert e.value.status_code==409

@pytest.mark.parametrize('path',['/','/etc','/workspace/../etc','/workspace2','/datax','/data//a','relative','/proc','/workspace/'])
def test_forbidden_mount_paths(path):
    with pytest.raises(HTTPException):validate_mount(path)

@pytest.mark.parametrize('path',['/workspace','/workspace/project','/data','/data/shared'])
def test_valid_mount_paths(path):validate_mount(path)

def test_stopping_and_deleting_keep_compute_reserved():
    ws=[SimpleNamespace(cpu=2,memory_gib=4,gpu=1,status=s) for s in ['running','stopping','deleting','stopped','pending_approval','failed']]
    vs=[SimpleNamespace(size_gib=10,status=s) for s in ['ready','creating','failed','deleted']]
    result=quota_used(ws,vs)
    assert result=={'cpu':6,'memory_gib':12,'gpu':3,'storage_gib':20,'workspaces':6}

def test_zero_quota_disallows_resources():
    with pytest.raises(HTTPException):check_quota({'gpu':0},{'gpu':0},{'gpu':1})

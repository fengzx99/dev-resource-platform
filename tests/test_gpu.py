from types import SimpleNamespace
from app.executor import allocation,gpu_candidates,LABEL

def box(**hc):return SimpleNamespace(attrs={'HostConfig':hc,'Config':{'Env':[]}})

def test_privileged_and_legacy_gpu_allocations_are_reserved():
    ids=['GPU-a','GPU-b']
    assert allocation(box(Privileged=True,DeviceRequests=[{'DeviceIDs':['0']}]),ids)==ids
    legacy=box(Runtime='nvidia');legacy.attrs['Config']['Env']=['NVIDIA_VISIBLE_DEVICES=1']
    assert allocation(legacy,ids)==['GPU-b']
    assert allocation(box(Devices=[{'PathOnHost':'/dev/nvidia0'}]),ids)==['GPU-a']
    assert allocation(box(DeviceRequests=[{'DeviceIDs':['MIG-unknown']}]),ids)==ids
    assert allocation(box(DeviceRequests=[{'DeviceIDs':['GPU-b']}]),ids)==['GPU-b']


def test_shared_gpu_access_protects_existing_platform_exclusive_allocations():
    inv={'gpus':[{'uuid':'GPU-a','reserved':True},{'uuid':'GPU-b','reserved':True}]}
    exclusive=box(DeviceRequests=[{'DeviceIDs':['GPU-a']}]);exclusive.labels={LABEL:'w1'}
    shared=box(DeviceRequests=[{'DeviceIDs':['GPU-b']}]);shared.labels={LABEL:'w2','io.devresources.gpu-mode':'shared'}
    assert gpu_candidates(inv,[exclusive,shared])==[]
    assert gpu_candidates(inv,[exclusive,shared],'shared')==['GPU-b']
    assert gpu_candidates(inv,[],'shared')==['GPU-a','GPU-b']

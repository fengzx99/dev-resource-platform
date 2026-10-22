from fastapi import HTTPException

RESERVED={'queued','starting','running','stopping','deleting'}
DEFAULT_QUOTA={'cpu':8,'memory_gib':32,'gpu':1,'storage_gib':100,'workspaces':3}
def check_quota(quota,used,added):
    for key,amount in added.items():
        if used.get(key,0)+amount>quota.get(key,0):
            raise HTTPException(409,f'{key} 配额不足：已分配 {used.get(key,0)}，申请 {amount}，上限 {quota.get(key,0)}')

VOLUME_PATHS={'user_data':'/data','user_home':'/root','share':'/share'}
def validate_mount(path):
    from pathlib import PurePosixPath
    p=PurePosixPath(path)
    if path in ('/root','/home/developer'): return
    if len(p.parts)<2 or p.parts[1] not in ('workspace','data','share') or '..' in p.parts or str(p)!=path:
        raise HTTPException(422,'挂载目录须为 /data、/share、/workspace 或其子目录，主目录支持 /root；兼容旧 /home/developer')

def validate_volume_mount(kind,path,access,readonly):
    validate_mount(path)
    legacy_home=kind=='user_home' and path=='/home/developer'
    if kind in VOLUME_PATHS and path!=VOLUME_PATHS[kind] and not legacy_home:
        raise HTTPException(422,f'{kind} 必须挂载到 {VOLUME_PATHS[kind]}')
    if kind=='generic' and path in ('/root','/home/developer'):
        raise HTTPException(422,'主目录必须使用 user_home 类型存储卷')
    if kind=='user_home' and (access!='private' or readonly):
        raise HTTPException(422,'用户主目录必须为私有读写卷')

def quota_used(workspaces,volumes):
    active=[w for w in workspaces if w.status in RESERVED]
    return {'cpu':sum(w.cpu for w in active),'memory_gib':sum(w.memory_gib for w in active),'gpu':sum(w.gpu for w in active),
            'storage_gib':sum(v.size_gib for v in volumes if v.status not in ('deleted','failed')),
            'workspaces':sum(w.status!='deleted' for w in workspaces)}

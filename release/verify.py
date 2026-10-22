#!/usr/bin/env python3
"""Offline verifier: hashes, safe archive paths and image/config identity."""
import argparse, hashlib, json, pathlib, tarfile


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()


def members(archive, prefix):
    names=[]
    for m in archive.getmembers():
        p=pathlib.PurePosixPath(m.name)
        if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!=prefix:
            raise ValueError('Unsafe archive member: '+m.name)
        if not (m.isfile() or m.isdir()):raise ValueError('Unsupported archive entry: '+m.name)
        names.append(m.name)
    if len(names)!=len(set(names)):raise ValueError('Duplicate archive entries')
    return names


def verify(directory,allow_incomplete=False):
    directory=pathlib.Path(directory).resolve()
    if (directory/'INCOMPLETE').exists() and not allow_incomplete:raise ValueError('Incomplete release; do not deploy')
    manifest=json.loads((directory/'manifest.json').read_text())
    checked=[]
    for line in (directory/'SHA256SUMS').read_text().splitlines():
        expected,name=line.split('  ',1)
        if pathlib.Path(name).name!=name:raise ValueError('Invalid checksum path')
        path=directory/name
        if path.is_symlink() or not path.is_file() or sha256(path)!=expected:raise ValueError('Checksum mismatch: '+name)
        checked.append(name)
    required={'manifest.json','deploy.py','verify.py','部署文档.html','README.txt',manifest['source'],manifest['wheels'],manifest['images_archive']}
    if not required.issubset(checked):raise ValueError('Incomplete checksum list')
    with tarfile.open(directory/manifest['source']) as archive:
        names=members(archive,'dev-resource-platform')
        for name in names:
            if set(pathlib.PurePosixPath(name).parts)&{'runtime','data','secrets','.remote-dev','node_modules','vendor'}:
                raise ValueError('Excluded runtime path in source: '+name)
        for name in ['app/main.py','app/history.py','app/monitoring.py','app/storage.py','workspace/entrypoint.sh','scripts/bootstrap.py','scripts/initialize_release.py','RELEASE.json']:
            if 'dev-resource-platform/'+name not in names:raise ValueError('Missing source: '+name)
        for name,expected in manifest['source_sha256'].items():
            content=archive.extractfile('dev-resource-platform/'+name).read()
            if hashlib.sha256(content).hexdigest()!=expected:raise ValueError('Source hash mismatch: '+name)
    with tarfile.open(directory/manifest['wheels']) as archive:
        names=members(archive,'dev-resource-platform')
        wheels=[x for x in names if x.endswith('.whl')]
        if sorted(pathlib.PurePosixPath(x).name for x in wheels)!=manifest['python_wheels']:raise ValueError('Wheel inventory mismatch')
        for name in names:
            p=pathlib.PurePosixPath(name)
            if p.parts[:3]!=('dev-resource-platform','vendor','wheels') and name not in ('dev-resource-platform','dev-resource-platform/vendor'):
                raise ValueError('Unexpected wheel archive path')
    actual,configs,docker_manifest,objects=image_inventory(directory/manifest['images_archive'])
    expected={}
    for item in manifest['images']:
        expected_id=item['id']
        obj=objects.get(expected_id,{})
        config_id=obj.get('config',{}).get('digest',expected_id)
        if config_id!=item.get('config_id',config_id):raise ValueError('Image config identity mismatch')
        expected[item['tag']]=config_id
    if actual!=expected:raise ValueError('Docker tags/config IDs do not match manifest')
    workspace=next(x for x in docker_manifest if manifest['workspace_image'] in (x.get('RepoTags') or []))
    labels=configs[workspace['Config']][1].get('config',{}).get('Labels',{})
    if labels.get('io.devresources.home-path')!='/root' or labels.get('io.devresources.login-user')!='root':raise ValueError('Wrong workspace login/home')
    return {'status':'PASS','release':manifest['version'],'checked_files':len(checked),'docker_images':len(actual),'python_wheels':len(wheels),'safe_archive_paths':True,'workspace_root_login':True}


def image_inventory(path):
    configs={};objects={};docker_manifest=None
    with tarfile.open(path,'r|gz') as archive:
        for m in archive:
            if m.isfile() and (m.name=='manifest.json' or m.size<100000):
                content=archive.extractfile(m).read()
                if m.name=='manifest.json':docker_manifest=json.loads(content)
                else:
                    try:cfg=json.loads(content)
                    except (ValueError,UnicodeError):continue
                    if not isinstance(cfg,dict):continue
                    digest=hashlib.sha256(content).hexdigest()
                    if m.name.startswith('blobs/sha256/') and m.name.rsplit('/',1)[1]!=digest:raise ValueError('OCI object hash mismatch')
                    objects['sha256:'+digest]=cfg
                    if 'rootfs' in cfg:configs[m.name]=(digest,cfg)
    if docker_manifest is None:raise ValueError('Docker manifest missing')
    actual={}
    for item in docker_manifest:
        digest,cfg=configs[item['Config']]
        if cfg.get('architecture')!='amd64' or cfg.get('os')!='linux':raise ValueError('Wrong image architecture')
        for tag in item.get('RepoTags') or []:actual[tag]='sha256:'+digest
    return actual,configs,docker_manifest,objects


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',nargs='?',default=str(pathlib.Path(__file__).resolve().parent));args=p.parse_args()
    print(json.dumps(verify(args.directory),ensure_ascii=False,indent=2))

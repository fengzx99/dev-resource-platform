"""Create an immutable, credential-free offline release in a new directory."""
import argparse, datetime, gzip, hashlib, importlib.util, json, os, pathlib, shutil, subprocess, sys, tarfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
SCRIPTS=['bootstrap.py','deploy_node.py','backup.py','initialize_release.py','package_release.py','history_backup.py','upgrade_history.py','upgrade_monitoring.py']
NAMES=['app','tests','workspace','requirements.txt','requirements.in','Dockerfile','.dockerignore','.gitignore','LICENSE','THIRD_PARTY.md','THIRD_PARTY_LICENSES','package.json','package-lock.json','install.sh']


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()


def safe(info):
    if any(x in pathlib.PurePosixPath(info.name).parts for x in ('__pycache__','.pytest_cache','.DS_Store')) or info.name.endswith('.pyc'):return None
    if not (info.isdir() or info.isfile()):raise RuntimeError('Source may not contain links: '+info.name)
    info.uid=info.gid=0;info.uname=info.gname='root'
    return info


def main():
    p=argparse.ArgumentParser();p.add_argument('--version',required=True);p.add_argument('--platform-image',required=True);p.add_argument('--workspace-image',required=True);p.add_argument('--resume',action='store_true',help='Resume only an incomplete release, retaining the image archive');a=p.parse_args()
    import re
    if not re.fullmatch(r'[0-9][0-9A-Za-z.-]{0,40}',a.version):p.error('Invalid version')
    out=ROOT.parent/('dev-resource-platform-release-'+a.version)
    if out.exists():
        if not a.resume or not (out/'INCOMPLETE').is_file():raise RuntimeError('Release already exists; refusing overwrite')
    else:out.mkdir(mode=0o700)
    try:
        build(out,a)
    except BaseException:
        (out/'INCOMPLETE').write_text('Packaging failed; do not deploy.\n')
        raise


def build(out,a):
    platform_tag='dev-resources/platform:'+a.version
    workspace_tag='dev-resources/workspace:'+a.version
    images=[platform_tag,workspace_tag,'ubuntu_24.04_template:v1','python:3.12-slim','postgres:16-alpine','quay.io/keycloak/keycloak:26.7.4']
    for src,dst in [(a.platform_image,platform_tag),(a.workspace_image,workspace_tag)]:
        if subprocess.run(['docker','image','inspect',dst],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
            src_id=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',src],text=True).strip()
            dst_id=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',dst],text=True).strip()
            if not a.resume or src_id!=dst_id:raise RuntimeError('Release image tag already exists: '+dst)
        subprocess.run(['docker','tag',src,dst],check=True)
    image_meta=[]
    for tag in images:
        row=json.loads(subprocess.check_output(['docker','image','inspect',tag],text=True))[0]
        if row['Os']!='linux' or row['Architecture']!='amd64':raise RuntimeError('Wrong image architecture')
        image_meta.append({'tag':tag,'id':row['Id'],'docker_reported_size_bytes':row['Size']})
    metadata={'version':a.version,'application_baseline':'1.1.0','platform_image':platform_tag,'workspace_image':workspace_tag,'build_date':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    source=out/('source-'+a.version+'.tar.gz')
    stage=out/'.source';stage.mkdir()
    try:
        for name in NAMES:
            path=ROOT/name
            if path.is_dir():shutil.copytree(path,stage/name,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','*.pyc','.DS_Store'))
            elif path.is_file():shutil.copy2(path,stage/name)
        (stage/'scripts').mkdir()
        for name in SCRIPTS:shutil.copy2(ROOT/'scripts'/name,stage/'scripts'/name)
        (stage/'RELEASE.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
        shutil.copytree(ROOT/'release',stage/'release',ignore=shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store'))
        shutil.copy2(ROOT/'release/README.txt',stage/'README.txt')
        # The archived code must be exactly the code in the running portal image.
        app_hashes={str(x.relative_to(stage)):sha(x) for x in (stage/'app').rglob('*') if x.is_file()}
        script="import hashlib,json,pathlib; r=pathlib.Path('/opt/platform'); print(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (r/'app').rglob('*') if p.is_file() and '__pycache__' not in str(p)}))"
        live=json.loads(subprocess.check_output(['docker','exec','drp-portal','python','-c',script],text=True))
        if live!=app_hashes:raise RuntimeError('Source differs from current running portal')
        source_hashes={str(x.relative_to(stage)):sha(x) for x in stage.rglob('*') if x.is_file()}
        with tarfile.open(source,'w:gz') as archive:archive.add(stage,arcname='dev-resource-platform',filter=safe)
    finally:shutil.rmtree(stage)
    print('SOURCE_AND_LIVE_CODE_MATCH '+str(source),flush=True)
    wheels=out/'python-wheels-cp312-linux-amd64.tar.gz'
    with tarfile.open(wheels,'w:gz') as archive:archive.add(ROOT/'vendor/wheels',arcname='dev-resource-platform/vendor/wheels',filter=safe)
    wheel_names=sorted(x.name for x in (ROOT/'vendor/wheels').glob('*.whl'))
    archive=out/'images-linux-amd64.tar.gz'
    if not (a.resume and archive.is_file()):
        print('Exporting six offline Docker images…',flush=True)
        with gzip.open(str(archive)+'.partial','wb',compresslevel=1) as f:
            process=subprocess.Popen(['docker','save']+images,stdout=subprocess.PIPE)
            while True:
                block=process.stdout.read(8*1024*1024)
                if not block:break
                f.write(block)
            if process.wait()!=0:raise RuntimeError('docker save failed')
        os.replace(str(archive)+'.partial',archive)
    for name in ('verify.py','deploy.py','部署文档.html','README.txt'):shutil.copy2(ROOT/'release'/name,out/name)
    spec=importlib.util.spec_from_file_location('release_verify',out/'verify.py');verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
    configs,_,_,_=verifier.image_inventory(archive)
    for item in image_meta:item['config_id']=configs[item['tag']]
    sys.path.insert(0,str(ROOT))
    from app.storage import IP_ROOTS
    manifest={**metadata,'platform':'linux/amd64','source':source.name,'wheels':wheels.name,'images_archive':archive.name,'images':image_meta,'storage_roots':IP_ROOTS,'python_wheels':wheel_names,'source_sha256':source_hashes,'secrets_included':False,'database_included':False,'workspace_data_included':False,'live_source_match':True}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    # Scan all shipped source/text against known local service credentials.
    cfg=json.loads((ROOT/'runtime/secrets/platform.json').read_text())
    credentials=[str(cfg[k]).encode() for k in ('db_password','keycloak_admin_password','oidc_secret','session_secret','executor_secret')]+[u['password'].encode() for u in cfg['users']]
    with tarfile.open(source) as t:
        for member in t.getmembers():
            if member.isfile():
                content=t.extractfile(member).read()
                if any(s and s in content for s in credentials):raise RuntimeError('Credential detected in source archive: '+member.name)
    for name in ('verify.py','deploy.py','部署文档.html','README.txt','manifest.json'):
        if any(s and s in (out/name).read_bytes() for s in credentials):raise RuntimeError('Credential detected in release text')
    files=sorted(x for x in out.iterdir() if x.is_file() and x.name not in ('SHA256SUMS','PACKAGE-VERIFICATION.json','INCOMPLETE','ACCEPTANCE.json','ACCEPTANCE.sha256'))
    (out/'SHA256SUMS').write_text(''.join(sha(x)+'  '+x.name+'\n' for x in files))
    spec=importlib.util.spec_from_file_location('release_verify',out/'verify.py');verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
    result=verifier.verify(out,allow_incomplete=True);result.update({'known_service_secrets_excluded':True,'live_source_match':True})
    (out/'PACKAGE-VERIFICATION.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    with (out/'SHA256SUMS').open('a') as f:f.write(sha(out/'PACKAGE-VERIFICATION.json')+'  PACKAGE-VERIFICATION.json\n')
    (out/'INCOMPLETE').unlink(missing_ok=True)
    print(json.dumps({'release':str(out),'version':a.version,'bytes':sum(x.stat().st_size for x in out.iterdir() if x.is_file()),'verification':result},ensure_ascii=False),flush=True)


if __name__=='__main__':main()

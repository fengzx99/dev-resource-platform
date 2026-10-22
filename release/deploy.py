#!/usr/bin/env python3
"""Fresh installation only. Existing installations are never overwritten."""
import argparse, importlib.util, ipaddress, json, os, pathlib, platform, shutil, socket, subprocess, sys, tarfile
from verify import verify, members
HERE=pathlib.Path(__file__).resolve().parent


def run(args,**kwargs):return subprocess.run(args,check=True,**kwargs)

def exists(kind,name):
    return subprocess.run(['docker',kind,'inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0


def preflight(args):
    if os.geteuid()!=0:raise RuntimeError('Run as root')
    if platform.system()!='Linux' or platform.machine() not in ('x86_64','amd64'):raise RuntimeError('Linux amd64 required')
    for tool in ('docker','openssl','juicefs','ip','mountpoint'):
        if not shutil.which(tool):raise RuntimeError('Missing executable: '+tool)
    info=json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'],text=True))
    if info.get('OSType')!='linux' or info.get('Architecture') not in ('x86_64','amd64'):raise RuntimeError('Wrong Docker platform')
    host=str(ipaddress.IPv4Address(args.host))
    addresses=json.loads(subprocess.check_output(['ip','-j','addr'],text=True))
    if host not in {a['local'] for n in addresses for a in n.get('addr_info',[])}:raise RuntimeError('Host IP is not assigned locally')
    mapping=json.loads((HERE/'manifest.json').read_text())['storage_roots']
    base=mapping.get(host)
    if not base:raise RuntimeError('Host IP has no storage mapping; review app/storage.py and rebuild release')
    if not pathlib.Path('/mnt/juicefs').is_mount():raise RuntimeError('JuiceFS is not mounted')
    run(['mountpoint','-q',base])
    mount=[]
    for line in pathlib.Path('/proc/1/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1);a=left.split();b=right.split()
        if a[4]==base:mount.append((a[3],b[0],b[1]))
    if not mount:raise RuntimeError('Fixed storage mount missing')
    root,fs,source=mount[-1]
    if not ((fs in ('nfs','nfs4') and source==host+':/data-local') or (fs not in ('nfs','nfs4') and root=='/data-local')):
        raise RuntimeError('Fixed storage mount source does not match '+host+':/data-local')
    share=pathlib.Path('/mnt/juicefs/share')
    if not share.is_dir() or share.is_symlink():raise RuntimeError('Shared directory missing or symlinked')
    target=args.target.absolute()
    if target.exists():raise RuntimeError('Target already exists; fresh installation refuses to overwrite it')
    if not target.parent.is_dir():raise RuntimeError('Target parent does not exist')
    probe=str(target.parent.resolve());mounts=[]
    for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1);mount=left.split()[4]
        if probe==mount or probe.startswith(mount.rstrip('/')+'/'):mounts.append((len(mount),right.split()[0]))
    if not mounts or max(mounts)[1]!='fuse.juicefs':raise RuntimeError('Target parent must be on JuiceFS')
    if shutil.disk_usage(target.parent).free<10*1024**3:raise RuntimeError('At least 10 GiB project storage required')
    docker_root=pathlib.Path(info['DockerRootDir'])
    if shutil.disk_usage(docker_root).free<10*1024**3:raise RuntimeError('At least 10 GiB Docker storage required')
    for suffix in ('database','keycloak','executor','portal'):
        if exists('container',args.prefix+'-'+suffix):raise RuntimeError('Container already exists: '+args.prefix+'-'+suffix)
    if exists('volume',args.prefix+'-postgres') or exists('network',args.prefix+'-control'):raise RuntimeError('Deployment volume/network already exists; use a fresh prefix or inspect the previous attempt')
    if not args.no_gpus:
        if not shutil.which('nvidia-smi'):raise RuntimeError('NVIDIA driver and nvidia-smi required')
        run(['nvidia-smi','-L'],stdout=subprocess.DEVNULL)
        if 'nvidia' not in (info.get('Runtimes') or {}):raise RuntimeError('NVIDIA Container Toolkit Docker runtime required')
    with socket.socket() as s:s.bind((host,args.port))
    return target


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',required=True);p.add_argument('--target',type=pathlib.Path,required=True)
    p.add_argument('--install',action='store_true',help='Execute installation after checks; default checks only')
    p.add_argument('--prefix',default='drp');p.add_argument('--port',type=int,default=8420)
    p.add_argument('--no-gpus',action='store_true',help='CPU-only rehearsal; GPU allocation is unavailable')
    args=p.parse_args()
    import re
    if not re.fullmatch(r'drp(?:-[a-z0-9-]{1,32})?',args.prefix) or not 1024<=args.port<=65535:p.error('Invalid prefix/port')
    print(json.dumps(verify(HERE),ensure_ascii=False),flush=True)
    target=preflight(args)
    print('PREFLIGHT PASS; target='+str(target),flush=True)
    if not args.install:return
    m=json.loads((HERE/'manifest.json').read_text())
    # Tags in the bundle are versioned. Do not silently replace a different image.
    missing_images=False
    for image in m['images']:
        if exists('image',image['tag']):
            current=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image['tag']],text=True).strip()
            if current not in (image['id'],image['config_id']):raise RuntimeError('Image tag conflicts with installed image: '+image['tag'])
        else:missing_images=True
    if missing_images:run(['docker','load','-i',str(HERE/m['images_archive'])],stdout=subprocess.DEVNULL)
    else:print('All image IDs already verified locally; import skipped',flush=True)
    for image in m['images']:
        actual=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image['tag']],text=True).strip()
        if actual not in (image['id'],image['config_id']):raise RuntimeError('Loaded image ID mismatch')
    staging=target.parent/('.'+target.name+'-extract')
    staging.mkdir(mode=0o700)
    try:
        for name in (m['source'],m['wheels']):
            with tarfile.open(HERE/name) as archive:
                members(archive,'dev-resource-platform');archive.extractall(staging)
        os.rename(staging/'dev-resource-platform',target)
    finally:shutil.rmtree(staging,ignore_errors=True)
    # Populate the shared image cache before API registration to avoid the
    # executor's HTTP timeout while exporting a large workspace image.
    image_id=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',m['workspace_image']],text=True).strip()
    cache=target/'data/image-cache';cache.mkdir(parents=True,mode=0o700)
    cache_file=cache/(image_id.split(':',1)[1]+'.tar')
    temporary=cache_file.with_suffix('.partial')
    print('Preparing offline workspace image cache…',flush=True)
    run(['docker','image','save','-o',str(temporary),m['workspace_image']])
    os.chmod(temporary,0o600);os.replace(temporary,cache_file)
    command=[sys.executable,str(target/'scripts/bootstrap.py'),'--phase','services','--host',args.host,'--prefix',args.prefix,'--port',str(args.port),'--platform-image',m['platform_image'],'--workspace-image',m['workspace_image']]
    if args.no_gpus:command.append('--no-gpus')
    run(command)
    run([sys.executable,str(target/'scripts/initialize_release.py'),'--prefix',args.prefix])
    print('INSTALL PASS '+str(target),flush=True)
    print('Initial credentials are local only: '+str(target/'runtime/secrets/initial-access.txt'),flush=True)


if __name__=='__main__':main()

"""Fixed storage layout. Only operator-owned mappings determine host paths."""
import re
import uuid
from pathlib import PurePosixPath

IP_ROOTS = {
    '172.16.9.10': '/mnt/server001',
    '172.16.9.11': '/mnt/server002',
    '172.16.9.20': '/mnt/server003',
    '172.16.9.21': '/mnt/server004',
}
SHARE_PATH = '/mnt/juicefs/share'
SHARE_ID = uuid.uuid5(uuid.NAMESPACE_URL, 'dev-resources:global-share').hex
TARGETS = {'user_data': '/data', 'user_home': '/root', 'share': '/share'}


def home_path(mounts):
    return '/root' if any(m.get('path',m.get('Destination'))=='/root' for m in mounts) else '/home/developer'


def fixed_targets(mounts):
    # Existing environments retain their original home when rebuilt.
    return {'/data',home_path(mounts),'/share'}


def login_user(labels):
    return 'root' if (labels or {}).get('io.devresources.login-user')=='root' else 'developer'


def directory_name(name, workspace_id):
    slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', name).strip('-_')[:48] or 'workspace'
    return slug + '-' + workspace_id


def source_path(kind, binding):
    if kind == 'share' and binding == {'mode': 'global_share'}:
        return SHARE_PATH
    if kind not in ('user_data', 'user_home') or binding.get('mode') != 'ip_fixed':
        raise ValueError('无效的固定存储绑定')
    base = IP_ROOTS.get(binding.get('node_ip'))
    directory = binding.get('directory', '')
    if not base or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}', directory):
        raise ValueError('存储节点或目录名无效')
    return str(PurePosixPath(base) / kind / directory)


def global_share(volume):
    return volume.kind == 'share' and volume.access == 'global_rw' and volume.binding == {'mode': 'global_share'}

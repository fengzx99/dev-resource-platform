"""Read-only host samples and viewer-scoped GPU monitoring."""
import csv
import copy
import io
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOST_PROC = Path('/host/proc')
HOST_FS = Path('/host/fs')
_lock = threading.Lock()
_cached = None
_cpu_previous = None


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def command_text(args):
    """Keep task arguments useful without displaying command-line credentials."""
    result = []
    hide_next = False
    sensitive = re.compile(r'password|passwd|token|secret|api[_-]?key|access[_-]?key', re.I)
    for arg in args:
        if hide_next:
            result.append('[已隐藏]')
            hide_next = False
            continue
        key = arg.split('=', 1)[0]
        if sensitive.search(key) and (arg.startswith('-') or '=' in arg):
            if '=' in arg:
                result.append(key + '=[已隐藏]')
            else:
                result.append(arg)
                hide_next = True
            continue
        arg = re.sub(r'(\w+://)[^\s/@]+:[^\s/@]+@', r'\1[已隐藏]@', arg)
        arg = re.sub(r'(?i)([?&](?:token|password|secret|api_key|access_token)=)[^&\s]+', r'\1[已隐藏]', arg)
        result.append(arg)
    return shlex.join(result)[:1500]


def cpu_sample():
    global _cpu_previous
    def read():
        lines = (HOST_PROC / 'stat').read_text().splitlines()
        values = [int(x) for x in lines[0].split()[1:9]]
        return sum(values), values[3] + values[4], sum(bool(re.match(r'cpu\d+ ', x)) for x in lines)
    current = read()
    previous = _cpu_previous
    if previous is None:
        time.sleep(0.15)
        previous, current = current, read()
    _cpu_previous = current
    delta = current[0] - previous[0]
    percent = round(max(0, min(100, (1 - (current[1] - previous[1]) / delta) * 100)), 1) if delta > 0 else None
    return {'used_percent': percent, 'cores': current[2],
            'load_average': [float(x) for x in (HOST_PROC / 'loadavg').read_text().split()[:3]]}


def memory_sample():
    fields = {}
    for line in (HOST_PROC / 'meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        fields[key] = int(value.split()[0]) * 1024
    total = fields['MemTotal']
    available = fields['MemAvailable']
    return {'total_bytes': total, 'available_bytes': available, 'used_bytes': total - available,
            'used_percent': round((total - available) / total * 100, 1),
            'swap_total_bytes': fields.get('SwapTotal', 0),
            'swap_used_bytes': fields.get('SwapTotal', 0) - fields.get('SwapFree', 0)}


def mount_path(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def storage_sample(shared_root):
    devices = {}
    for line in (HOST_PROC / '1/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        parts, fs = left.split(), right.split()
        point, root = mount_path(parts[4]), mount_path(parts[3])
        if fs[0] not in ('ext4', 'xfs', 'btrfs', 'zfs', 'fuse.juicefs') or root != '/':
            continue
        key = (parts[2], fs[0])
        if key not in devices or len(point) < len(devices[key]['mountpoint']):
            devices[key] = {'mountpoint': point, 'filesystem': fs[0], 'shared': fs[0] == 'fuse.juicefs'}
    rows = []
    for entry in sorted(devices.values(), key=lambda x: x['mountpoint']):
        try:
            # Use the executor's current shared bind; local disks use the host view.
            path = shared_root if entry['shared'] else HOST_FS / entry['mountpoint'].lstrip('/')
            usage = shutil.disk_usage(path)
            entry.update(total_bytes=usage.total, used_bytes=usage.used, available_bytes=usage.free,
                         used_percent=round(usage.used / (usage.used + usage.free) * 100, 1) if usage.used + usage.free else None, available=True)
        except OSError:
            entry.update(available=False, error='文件系统暂时不可读')
        rows.append(entry)
    return rows


def csv_query(kind, fields):
    p = subprocess.run(['nvidia-smi', '--query-' + kind + '=' + fields,
                        '--format=csv,noheader,nounits'], text=True, capture_output=True, timeout=8)
    if p.returncode:
        raise RuntimeError('GPU query failed')
    return [[value.strip() for value in row] for row in csv.reader(io.StringIO(p.stdout)) if row]


def gpu_queries():
    # NVML initialization can be slow on a busy eight-GPU host. Run the two
    # independent queries together so their timeout budgets do not accumulate.
    queries = {'gpu': 'index,name,uuid,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw,power.limit',
               'compute-apps': 'gpu_uuid,pid,process_name,used_gpu_memory'}
    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {kind: pool.submit(csv_query, kind, fields) for kind, fields in queries.items()}
        for kind, future in futures.items():
            try:
                results[kind] = future.result()
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                results[kind] = error
    return results


def process_detail(pid, process_name, containers, users):
    out = {'pid': pid, 'process_name': process_name, 'command': process_name,
           'container': None, 'container_id': None, 'workspace_id': None,
           'user': None, 'user_uid': None, 'started_at': None}
    path = HOST_PROC / str(pid)
    try:
        args = [x.decode(errors='replace') for x in (path / 'cmdline').read_bytes().split(b'\0') if x]
        if args:
            out['command'] = command_text(args)
        status = (path / 'status').read_text()
        uid = re.search(r'^Uid:\s+(\d+)', status, re.M)
        if uid:
            out['user'] = users.get(uid[1], uid[1])
            out['user_uid'] = int(uid[1])
        group = (path / 'cgroup').read_text()
        match = re.search(r'(?:docker[-/])([a-f0-9]{64})', group)
        if match and match[1] in containers:
            box = containers[match[1]]
            out['container'] = box.name
            out['container_id'] = box.id
            out['workspace_id'] = box.labels.get('io.devresources.workspace')
        stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        uptime = float((HOST_PROC / 'uptime').read_text().split()[0])
        out['started_at'] = round(time.time() - uptime + int(stat[19]) / os.sysconf('SC_CLK_TCK'))
    except (OSError, ValueError, IndexError):
        # Processes may exit between the GPU sample and /proc reads.
        pass
    return out


def gpu_sample(docker_client):
    result = {'gpus': [], 'gpu_error': None, 'process_error': None, 'allocation_error': None}
    snapshot = gpu_queries()
    try:
        rows = snapshot['gpu']
        if isinstance(rows, Exception):
            raise rows
        for row in rows:
            if len(row) != 9:
                raise ValueError('Unexpected GPU sample')
            i, name, uuid, total, used, util, temperature, power, limit = row
            result['gpus'].append({'index': int(i), 'name': name, 'uuid': uuid,
                'memory_total_mib': number(total), 'memory_used_mib': number(used),
                'utilization_percent': number(util), 'temperature_c': number(temperature),
                'power_w': number(power), 'power_limit_w': number(limit), 'processes': [], 'allocations': []})
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        result['gpu_error'] = 'GPU 指标采集不可用'
        result['process_error'] = 'GPU 指标不可用，无法关联计算进程'
        result['allocation_error'] = 'GPU 指标不可用，无法确认分配信息'
        return result
    try:
        boxes = {box.id: box for box in docker_client.containers.list()}
    except Exception:
        boxes = {}
        result['allocation_error'] = '容器分配信息采集不可用'
    by_uuid = {gpu['uuid']: gpu for gpu in result['gpus']}
    # Share the executor's device-access rules, including privileged/--gpus all.
    # GPU access remains allocated even when no compute process is running.
    from .executor import allocation
    for box in boxes.values():
        if box.labels.get('io.devresources.infrastructure') == '1':
            continue
        try:
            devices = allocation(box, list(by_uuid))
            for uuid in devices:
                by_uuid[uuid]['allocations'].append({'container_id': box.id, 'container': box.name,
                    'workspace_id': box.labels.get('io.devresources.workspace'),
                    'gpu_mode':box.labels.get('io.devresources.gpu-mode','exclusive')})
        except (KeyError, TypeError, AttributeError):
            result['allocation_error'] = '部分容器的 GPU 分配信息不可用'
    users = {}
    try:
        users = {line.split(':')[2]: line.split(':')[0] for line in (HOST_FS / 'etc/passwd').read_text().splitlines() if len(line.split(':')) >= 3}
    except OSError:
        pass
    try:
        details = {}
        process_rows = snapshot['compute-apps']
        if isinstance(process_rows, Exception):
            raise process_rows
        for row in process_rows:
            if len(row) != 4 or row[0] not in by_uuid:
                continue
            uuid, raw_pid, name, memory = row
            pid = int(raw_pid)
            if pid not in details:
                details[pid] = process_detail(pid, name, boxes, users)
            by_uuid[uuid]['processes'].append({**details[pid], 'memory_mib': number(memory)})
        for gpu in result['gpus']:
            gpu['processes'].sort(key=lambda x: -(x['memory_mib'] or 0))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        result['process_error'] = 'GPU 计算进程采集不可用'
    return result


def workspace_metrics(metrics, node_id, workspaces):
    """Join observed containers to platform owners without trusting names or labels alone."""
    result = copy.deepcopy(metrics)  # Never add request-specific users to the sample cache.
    known = {w['id']: w for w in workspaces if w['node_id'] == node_id}
    runtimes = {w['runtime_id']: w for w in known.values() if w['runtime_id']}

    def resolve(item):
        workspace = runtimes.get(item.get('container_id'))
        if not workspace or item.get('workspace_id') not in (None, workspace['id']):
            return None
        return {**{key: workspace[key] for key in ('id', 'name', 'owner_id', 'username',
                                               'project_id', 'project_name', 'status')},
                'gpu_mode':workspace.get('gpu_mode','exclusive')}

    gpu_rows = result.get('gpus', [])
    for gpu in gpu_rows:
        for item in gpu.get('processes', []) + gpu.get('allocations', []):
            item['workspace'] = resolve(item)

    rows = []
    for workspace in known.values():
        if not workspace['gpu']:
            continue
        devices, processes = [], []
        for gpu in gpu_rows:
            assigned = any(a.get('workspace', {}).get('id') == workspace['id']
                           for a in gpu.get('allocations', []) if a.get('workspace'))
            tasks = [p for p in gpu.get('processes', [])
                     if p.get('workspace') and p['workspace']['id'] == workspace['id']]
            if assigned:
                devices.append({key: gpu.get(key) for key in ('index', 'uuid', 'utilization_percent',
                    'memory_used_mib', 'memory_total_mib')})
            processes.extend({**p, 'gpu_index': gpu['index'], 'gpu_uuid': gpu['uuid']} for p in tasks)
        measured = [p['memory_mib'] for p in processes if p.get('memory_mib') is not None]
        rows.append({key: workspace[key] for key in ('id', 'name', 'owner_id', 'username',
            'project_id', 'project_name', 'status', 'gpu')})
        rows[-1].update(devices=devices, processes=processes,gpu_mode=workspace.get('gpu_mode','exclusive'),
                        process_memory_mib=None if result.get('process_error') or len(measured) != len(processes) else sum(measured),
                        process_count=len({p['pid'] for p in processes}))
    result['workspaces'] = rows
    return result


def developer_metrics(metrics, node_id, workspaces, own_ids):
    """Expose all tasks on allocated cards; trust current node/runtime identity only."""
    own={w['id']:w for w in workspaces if w['id'] in own_ids and w['node_id']==node_id
         and w['runtime_id'] and w['gpu']>0}
    runtimes={w['runtime_id']:w['id'] for w in own.values()}
    def allocated(gpu):
        return any(a.get('container_id') in runtimes and a.get('workspace_id') in (None,runtimes[a['container_id']])
                   for a in gpu.get('allocations',[]))
    # An incomplete allocation sample is not sufficient to grant visibility.
    gpus=[] if metrics.get('allocation_error') else [g for g in metrics.get('gpus',[]) if allocated(g)]
    result=workspace_metrics({**metrics,'gpus':gpus},node_id,workspaces)
    result['workspaces']=[w for w in result['workspaces'] if w['id'] in own]
    return result


def sample(docker_client, shared_root):
    global _cached
    with _lock:
        if _cached and time.time() - _cached['collected_at'] < 3:
            return _cached
        out = {'collected_at': time.time(), 'cpu': None, 'memory': None, 'storage': [], 'warnings': []}
        for key, read in [('cpu', cpu_sample), ('memory', memory_sample), ('storage', lambda: storage_sample(shared_root))]:
            try:
                out[key] = read()
            except (OSError, ValueError, KeyError):
                out['warnings'].append({'cpu': 'CPU 指标不可用', 'memory': '内存指标不可用', 'storage': '存储指标不可用'}[key])
        out.update(gpu_sample(docker_client))
        _cached = out
        return out

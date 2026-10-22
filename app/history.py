"""Additive accounting and low-frequency history using the existing database/collector."""
import csv
import io
import math
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select, delete, func, or_, and_, text

from . import auth
from .models import Session, User, Project, Member, Node, Workspace, Volume, ResourceAllocation, ResourceMetric, data

INTERVAL = 60
RETENTION_DAYS = 30
router = APIRouter(prefix='/api/resource-history')
NOTE = 'GPU 利用率为关联物理卡采样均值，共享卡不等于容器独立算力；汇总按节点、GPU UUID 和采样分钟去重。'


def sync_allocation(db, w, started=False, now=None):
    """Called under the existing project lock, in the lifecycle transaction."""
    now = time.time() if now is None else now
    active = list(db.scalars(select(ResourceAllocation).where(
        ResourceAllocation.workspace_id == w.id, ResourceAllocation.ended_at.is_(None))))
    running = bool(w.runtime_id) and w.status in ('running', 'starting', 'stopping', 'deleting')
    for row in active:
        if not running or row.runtime_id != w.runtime_id or row.node_id != w.node_id:
            row.ended_at = now
            row.end_reason = (w.error or w.status)[:600]
    if running and not any(r.runtime_id == w.runtime_id and r.node_id == w.node_id for r in active):
        # A runtime is one session even across portal restarts. Never reopen an ended session.
        if db.scalar(select(ResourceAllocation.id).where(ResourceAllocation.node_id == w.node_id,
                                                         ResourceAllocation.runtime_id == w.runtime_id)):
            return
        owner, project, node = db.get(User, w.owner_id), db.get(Project, w.project_id), db.get(Node, w.node_id)
        mounts = []
        for mount in w.mounts or []:
            volume = db.get(Volume, mount['volume_id'])
            mounts.append({'volume_id': mount['volume_id'], 'path': mount['path'],
                           'readonly': mount.get('readonly', False),
                           'size_gib': volume.size_gib if volume else None,
                           'elastic': bool(volume and volume.elastic)})
        db.add(ResourceAllocation(workspace_id=w.id, node_id=w.node_id, runtime_id=w.runtime_id,
            owner_id=w.owner_id, project_id=w.project_id, started_at=now,
            start_source='started' if started else 'observed', snapshot={
                'name': w.name, 'username': owner.username if owner else w.owner_id,
                'project_name': project.name if project else w.project_id,
                'node_name': node.name if node else w.node_id, 'image_ref': w.image_ref,
                'cpu': w.cpu, 'memory_gib': w.memory_gib, 'gpu': w.gpu,
                'gpu_mode': w.gpu_mode, 'devices': list(w.devices or []), 'mounts': mounts}))


def compact_metrics(raw, node, workspaces):
    from .monitoring import workspace_metrics
    joined = workspace_metrics(raw, node.id, workspaces)
    out = {'node_name': node.name, 'gpus': [], 'gpu_error': raw.get('gpu_error'),
           'process_error': raw.get('process_error'), 'allocation_error': raw.get('allocation_error')}
    for gpu in joined.get('gpus', []):
        g = {key: gpu.get(key) for key in ('uuid', 'index', 'name', 'utilization_percent',
             'memory_used_mib', 'memory_total_mib', 'temperature_c', 'power_w')}
        refs = {}
        for item in gpu.get('allocations', []) + gpu.get('processes', []):
            w = item.get('workspace')
            if not w:
                continue
            refs[w['id']] = {k: w[k] for k in ('id', 'name', 'owner_id', 'username', 'project_id', 'project_name', 'gpu_mode')}
        g['workspaces'] = list(refs.values())
        # Deliberately exclude process commands, environment variables and SSH keys.
        out['gpus'].append(g)
    return out


def collect_once(execute=None, now=None):
    from .nodes import executor
    execute = execute or executor
    now = time.time() if now is None else now
    bucket = int(now // INTERVAL)
    with Session() as db:
        # Multiple web workers must not multiply node probes or retention jobs.
        if db.bind.dialect.name == 'postgresql' and not db.scalar(text('SELECT pg_try_advisory_xact_lock(84202160)')):
            return
        usernames = {u.id: u.username for u in db.scalars(select(User))}
        projects = {p.id: p.name for p in db.scalars(select(Project))}
        ws = [{**{k: getattr(w, k) for k in ('id', 'name', 'node_id', 'runtime_id', 'owner_id', 'project_id', 'status', 'gpu', 'gpu_mode')},
               'username': usernames.get(w.owner_id, w.owner_id), 'project_name': projects.get(w.project_id, w.project_id)}
              for w in db.scalars(select(Workspace).where(Workspace.runtime_id != ''))]
        for node in db.scalars(select(Node)):
            if db.scalar(select(ResourceMetric.id).where(ResourceMetric.node_id == node.id, ResourceMetric.bucket == bucket)):
                continue
            sampled = now
            try:
                raw = execute('metrics', node_id=node.id)
                sampled = raw.get('collected_at', now)
                if not math.isfinite(sampled) or abs(now - sampled) > 120:
                    raise ValueError('stale sample')
                payload = compact_metrics(raw, node, ws)
            except Exception:
                payload = {'node_name': node.name, 'gpus': [], 'gpu_error': '节点指标采集失败'}
            db.add(ResourceMetric(node_id=node.id, bucket=bucket, collected_at=sampled, payload=payload))
        if bucket % 60 == 0:
            db.execute(delete(ResourceMetric).where(ResourceMetric.bucket < bucket - RETENTION_DAYS * 1440))
        db.commit()


def collector():
    while True:
        try:
            collect_once()
        except Exception as e:
            print('history-collector:', type(e).__name__, flush=True)
        time.sleep(max(1, INTERVAL - time.time() % INTERVAL))


def start_collector():
    threading.Thread(target=collector, name='resource-history', daemon=True).start()


def access_scope(db, user):
    if user.role in ('admin', 'auditor'):
        return None
    return [(m.project_id, None if m.role == 'manager' else user.id)
            for m in db.scalars(select(Member).where(Member.user_id == user.id))]


def allowed(ref, scope, owner_id='', project_id=''):
    return (not owner_id or ref['owner_id'] == owner_id) and (not project_id or ref['project_id'] == project_id) and (
        scope is None or any(ref['project_id'] == p and (u is None or ref['owner_id'] == u) for p, u in scope))


def scoped(query, scope):
    if scope is None:
        return query
    conditions = [and_(ResourceAllocation.project_id == p, ResourceAllocation.owner_id == u) if u else
                  ResourceAllocation.project_id == p for p, u in scope]
    return query.where(or_(*conditions) if conditions else text('1=0'))


def window(start, end):
    now = time.time()
    end = now if end is None else end
    start = end - 7 * 86400 if start is None else start
    if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end <= start or end - start > 31 * 86400 or end > now + 60:
        raise HTTPException(422, '请选择不超过 31 天且不晚于当前时间的有效区间')
    return start, min(end, now)


def accumulator():
    return {'sessions': 0, 'running_hours': 0., 'cpu_core_hours': 0., 'memory_gib_hours': 0.,
            'exclusive_gpu_hours': 0., 'shared_gpu_access_hours': 0., 'gpu_sum': 0., 'gpu_samples': 0,
            'gpu_max_percent': None, 'gpu_memory_peak_mib': None}


def add_gpu(stat, gpu):
    v = gpu.get('utilization_percent')
    if isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 100:
        stat['gpu_sum'] += v
        stat['gpu_samples'] += 1
        stat['gpu_max_percent'] = max(stat['gpu_max_percent'] or 0, v)
    mem = gpu.get('memory_used_mib')
    if isinstance(mem, (int, float)) and math.isfinite(mem) and mem >= 0:
        stat['gpu_memory_peak_mib'] = max(stat['gpu_memory_peak_mib'] or 0, mem)


def finish(stat):
    out = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in stat.items() if k != 'gpu_sum'}
    out['gpu_avg_percent'] = round(stat['gpu_sum'] / stat['gpu_samples'], 2) if stat['gpu_samples'] else None
    return out


def report(db, user, start=None, end=None, owner_id='', project_id='', page=1, page_size=50):
    start, end = window(start, end)
    scope = access_scope(db, user)
    # Filtering never expands access; forbidden filters cannot reveal other identities.
    if scope is not None and project_id and not any(p == project_id for p, _ in scope):
        raise HTTPException(403, '无权查看该项目的统计')
    if scope is not None and owner_id and owner_id != user.id and not any(u is None and (not project_id or p == project_id) for p, u in scope):
        raise HTTPException(403, '无权查看该用户的统计')
    base = scoped(select(ResourceAllocation), scope)
    choices = {'users': {}, 'projects': {}}
    for a in db.scalars(base.execution_options(yield_per=200)):
        choices['users'][a.owner_id] = a.snapshot.get('username', a.owner_id)
        choices['projects'][a.project_id] = a.snapshot.get('project_name', a.project_id)
    query = base.where(ResourceAllocation.started_at < end, or_(ResourceAllocation.ended_at.is_(None), ResourceAllocation.ended_at > start))
    if owner_id:
        query = query.where(ResourceAllocation.owner_id == owner_id)
    if project_id:
        query = query.where(ResourceAllocation.project_id == project_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    if total > 50000:
        raise HTTPException(422, '记录超过 50000 条，请缩短区间或增加筛选条件')
    summary = accumulator()
    users, projects = {}, {}
    allocations = []
    for a in db.scalars(query.order_by(ResourceAllocation.started_at.desc())):
        snap = a.snapshot
        hours = max(0, min(a.ended_at if a.ended_at is not None else end, end) - max(start, a.started_at)) / 3600
        groups = [summary,
            users.setdefault(a.owner_id, {**accumulator(), 'id': a.owner_id, 'name': snap.get('username', a.owner_id)}),
            projects.setdefault(a.project_id, {**accumulator(), 'id': a.project_id, 'name': snap.get('project_name', a.project_id)})]
        for g in groups:
            g['sessions'] += 1
            g['running_hours'] += hours
            g['cpu_core_hours'] += (snap.get('cpu') or 0) * hours
            g['memory_gib_hours'] += (snap.get('memory_gib') or 0) * hours
            key = 'shared_gpu_access_hours' if snap.get('gpu_mode') == 'shared' else 'exclusive_gpu_hours'
            g[key] += len(snap.get('devices', [])) * hours
        allocations.append({**data(a), 'period_hours': round(hours, 4)})
    timeline = defaultdict(accumulator)
    gpus = {}
    sample_rows = failures = 0
    first_sample = last_sample = None
    # Stream compact samples, bounding memory even for a full month.
    metric_query = select(ResourceMetric).where(ResourceMetric.collected_at >= start, ResourceMetric.collected_at <= end).order_by(ResourceMetric.bucket)
    step = max(INTERVAL, math.ceil((end - start) / 240 / INTERVAL) * INTERVAL)
    for sample in db.scalars(metric_query.execution_options(yield_per=100)):
        payload = sample.payload
        if scope is None and not owner_id and not project_id:
            failures += bool(payload.get('gpu_error'))
        matched = False
        for gpu in payload.get('gpus', []):
            refs = [w for w in gpu.get('workspaces', []) if allowed(w, scope, owner_id, project_id)]
            if not refs and (scope is not None or owner_id or project_id):
                continue
            matched = True
            key = (sample.node_id, gpu['uuid'])
            stat = gpus.setdefault(key, {**accumulator(), 'node_id': sample.node_id,
                'node_name': payload.get('node_name', sample.node_id), 'uuid': gpu['uuid'], 'index': gpu.get('index'), 'name': gpu.get('name')})
            for group in [summary, stat, timeline[int(sample.collected_at // step) * step]]:
                add_gpu(group, gpu)
            # Several containers in the same user/project sharing one card count once per sample.
            for collection, idkey, namekey in [(users, 'owner_id', 'username'), (projects, 'project_id', 'project_name')]:
                distinct = {w[idkey]: w[namekey] for w in refs}
                for ident, name in distinct.items():
                    add_gpu(collection.setdefault(ident, {**accumulator(), 'id': ident, 'name': name}), gpu)
        if matched:
            sample_rows += 1
            first_sample = min(first_sample or sample.collected_at, sample.collected_at)
            last_sample = max(last_sample or sample.collected_at, sample.collected_at)
    points = [{'time': b, **finish(timeline[b])} for b in range(int(start // step) * step, int(end // step) * step + 1, step)]
    return {'start': start, 'end': end, 'summary': finish(summary),
        'users': [finish(g) for g in users.values()], 'projects': [finish(g) for g in projects.values()],
        'gpus': [finish(g) for g in gpus.values()], 'timeline': points,
        'allocations': allocations[(page - 1) * page_size:page * page_size], '_all_allocations': allocations,
        'total': total, 'page': page, 'page_size': page_size,
        'choices': {k: [{'id': i, 'name': n} for i, n in values.items()] for k, values in choices.items()},
        'collection': {'interval_seconds': INTERVAL, 'retention_days': RETENTION_DAYS, 'sample_rows': sample_rows,
                       'failed_node_samples': failures, 'first_sample': first_sample, 'last_sample': last_sample,
                       'enabled_at': db.scalar(select(func.min(ResourceAllocation.started_at))) if scope is None else None},
        'note': NOTE}


@router.get('')
def get_history(start: float | None = None, end: float | None = None, owner_id: str = '', project_id: str = '',
                page: int = 1, page_size: int = 50, user=Depends(auth.current_user)):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, '分页参数无效')
    with Session() as db:
        result = report(db, user, start, end, owner_id, project_id, page, page_size)
        result.pop('_all_allocations')
        return result


def csv_cell(value):
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else ''


@router.get('/export')
def export_history(kind: str = 'allocations', start: float | None = None, end: float | None = None,
                   owner_id: str = '', project_id: str = '', user=Depends(auth.current_user)):
    if kind not in ('allocations', 'users', 'projects', 'gpu'):
        raise HTTPException(422, '导出类型无效')
    with Session() as db:
        result = report(db, user, start, end, owner_id, project_id)
    if kind == 'allocations':
        headers = ['记录ID', '环境ID', '环境名称', '用户', '项目', '节点', '容器ID', '镜像', 'CPU核数', '内存GiB',
                   '申请GPU数', '实际分配GPU数', 'GPU使用方式', 'GPU UUID', '分配起点UTC', '释放时间UTC', '起点来源', '释放原因', '区间占用小时', '挂载及容量']
        rows = []
        for a in result['_all_allocations']:
            s = a['snapshot']
            rows.append([a['id'], a['workspace_id'], s.get('name'), s.get('username'), s.get('project_name'), s.get('node_name'), a['runtime_id'],
                s.get('image_ref'), s.get('cpu'), s.get('memory_gib'), s.get('gpu'), len(s.get('devices', [])), s.get('gpu_mode'),
                ';'.join(s.get('devices', [])), iso(a['started_at']), iso(a['ended_at']),
                '上线时开始观测' if a['start_source'] == 'observed' else '启动时记录', a['end_reason'], a['period_hours'],
                ';'.join(m['path'] + ':' + ('弹性不限额' if m.get('elastic') else str(m.get('size_gib')) + 'GiB') for m in s.get('mounts', []))])
    elif kind == 'gpu':
        headers = ['节点', 'GPU UUID', '卡序号', '型号', '关联整卡平均利用率%', '整卡峰值利用率%', '整卡峰值显存MiB', '有效卡样本数', '统计起点UTC', '统计终点UTC', '口径']
        rows = [[g['node_name'], g['uuid'], g['index'], g['name'], g['gpu_avg_percent'], g['gpu_max_percent'],
                 g['gpu_memory_peak_mib'], g['gpu_samples'], iso(result['start']), iso(result['end']), NOTE] for g in result['gpus']]
    else:
        headers = ['ID', '用户' if kind == 'users' else '项目', '会话数', '容器占用小时', 'CPU核时', '内存GiB时',
                   '独占GPU卡时', '共享GPU访问卡时', '关联整卡平均利用率%', '有效卡样本数', '统计起点UTC', '统计终点UTC', '口径']
        rows = [[g[k] for k in ('id', 'name', 'sessions', 'running_hours', 'cpu_core_hours', 'memory_gib_hours',
                    'exclusive_gpu_hours', 'shared_gpu_access_hours', 'gpu_avg_percent', 'gpu_samples')] +
                [iso(result['start']), iso(result['end']), NOTE] for g in result[kind]]
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows([[csv_cell(c) for c in row] for row in rows])
    return Response(('\ufeff' + output.getvalue()).encode('utf-8'), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="resource-{kind}-{int(result["end"])}.csv"'})

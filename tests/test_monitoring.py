from pathlib import Path
from types import SimpleNamespace
import subprocess
import threading

import pytest
from app import monitoring as m


def test_cpu_and_memory_are_actual_host_usage(monkeypatch, tmp_path):
    (tmp_path / 'stat').write_text('cpu 100 0 0 100 0 0 0 0 99 0\ncpu0 100 0\n')
    (tmp_path / 'loadavg').write_text('1.5 1.0 0.5 1/10 1')
    (tmp_path / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 600 kB\nMemFree: 100 kB\n')
    monkeypatch.setattr(m, 'HOST_PROC', tmp_path)
    monkeypatch.setattr(m, '_cpu_previous', (180, 90, 1))
    assert m.cpu_sample() == {'used_percent': 50, 'cores': 1, 'load_average': [1.5, 1, 0.5]}
    assert m.memory_sample()['used_percent'] == 40


def test_storage_deduplicates_mounts_and_marks_disconnected_juicefs(monkeypatch, tmp_path):
    (tmp_path / '1').mkdir()
    (tmp_path / '1/mountinfo').write_text(
        '1 0 8:2 / / rw - ext4 /dev/sda2 rw\n'
        '2 0 8:2 / /duplicate rw - ext4 /dev/sda2 rw\n'
        '3 0 8:2 /data /bind rw - ext4 /dev/sda2 rw\n'
        '4 0 0:9 / /mnt/juicefs rw - fuse.juicefs JuiceFS:data rw\n')
    monkeypatch.setattr(m, 'HOST_PROC', tmp_path)
    monkeypatch.setattr(m, 'HOST_FS', tmp_path / 'host')
    def usage(path):
        if str(path) == '/shared': raise OSError(107, 'Disconnected')
        return SimpleNamespace(total=1000, used=250, free=750)
    monkeypatch.setattr(m.shutil, 'disk_usage', usage)
    rows = m.storage_sample(Path('/shared'))
    assert len(rows) == 2 and rows[0]['used_percent'] == 25
    assert rows[1]['shared'] and not rows[1]['available']


def test_gpu_tasks_map_to_host_process_and_container_on_each_card(monkeypatch, tmp_path):
    host = tmp_path / 'host'
    (host / 'etc').mkdir(parents=True)
    (host / 'etc/passwd').write_text('trainer:x:1000:1000::/home/trainer:/bin/bash\n')
    proc = tmp_path / 'proc'
    p = proc / '123'; p.mkdir(parents=True)
    (p / 'cmdline').write_bytes(b'python\0train.py\0--api-key\0private-value\0')
    (p / 'status').write_text('Uid:\t1000 1000 1000 1000\n')
    cid = 'a' * 64
    (p / 'cgroup').write_text('0::/system.slice/docker-' + cid + '.scope\n')
    (p / 'stat').write_text('123 (python name) ' + ' '.join(['S'] + ['0'] * 18 + ['100']))
    (proc / 'uptime').write_text('1000.0 0.0')
    monkeypatch.setattr(m, 'HOST_PROC', proc); monkeypatch.setattr(m, 'HOST_FS', host)
    def query(kind, fields):
        if kind == 'gpu':
            return [['0', 'B300', 'GPU-a', '100', '50', '70', '40', '200', '1000'],
                    ['1', 'B300', 'GPU-b', '100', '25', 'N/A', '40', '200', '1000']]
        return [['GPU-a', '123', 'python', '50'], ['GPU-b', '123', 'python', '25']]
    monkeypatch.setattr(m, 'csv_query', query)
    box = SimpleNamespace(id=cid, name='training', labels={'io.devresources.workspace': 'workspace1'},
                          attrs={'HostConfig':{'DeviceRequests':[{'DeviceIDs':['GPU-a']}]},'Config':{'Env':[]}})
    result = m.gpu_sample(SimpleNamespace(containers=SimpleNamespace(list=lambda: [box])))
    task = result['gpus'][0]['processes'][0]
    assert task['container'] == 'training' and task['user'] == 'trainer'
    assert task['container_id'] == cid and task['user_uid'] == 1000
    assert task['workspace_id'] == 'workspace1' and 'private-value' not in task['command']
    assert 'train.py' in task['command'] and task['started_at']
    assert result['gpus'][1]['processes'][0]['memory_mib'] == 25
    assert result['gpus'][1]['utilization_percent'] is None
    assert result['gpus'][0]['allocations'][0]['workspace_id'] == 'workspace1'


def test_idle_gpu_allocations_and_external_access_are_visible(monkeypatch, tmp_path):
    def query(kind, fields):
        return [['0','B300','GPU-a','100','0','0','30','50','1000'],
                ['1','B300','GPU-b','100','0','0','30','50','1000']] if kind == 'gpu' else []
    monkeypatch.setattr(m,'csv_query',query)
    def box(cid, devices, labels):
        return SimpleNamespace(id=cid,name=cid,labels=labels,
            attrs={'HostConfig':devices,'Config':{'Env':[]}})
    boxes=[box('platform',{'DeviceRequests':[{'DeviceIDs':['GPU-a']}]},{'io.devresources.workspace':'w1'}),
           box('external',{'Privileged':True},{}),
           box('executor',{'DeviceRequests':[{'Count':-1}]},{'io.devresources.infrastructure':'1'})]
    result=m.gpu_sample(SimpleNamespace(containers=SimpleNamespace(list=lambda:boxes)))
    assert {a['container_id'] for a in result['gpus'][0]['allocations']} == {'platform','external'}
    assert [a['container_id'] for a in result['gpus'][1]['allocations']] == ['external']
    assert all(not g['processes'] for g in result['gpus'])
    assert result['allocation_error'] is None


def test_container_lookup_failure_is_not_reported_as_unallocated(monkeypatch):
    monkeypatch.setattr(m,'csv_query',lambda kind,fields:[['0','B300','GPU-a','100','0','0','30','50','1000']] if kind=='gpu' else [])
    client=SimpleNamespace(containers=SimpleNamespace(list=lambda:(_ for _ in ()).throw(RuntimeError('offline'))))
    assert m.gpu_sample(client)['allocation_error']


def test_gpu_queries_overlap_and_process_timeout_keeps_gpu_metrics(monkeypatch):
    barrier=threading.Barrier(2)
    def query(kind, fields):
        barrier.wait(timeout=2)
        if kind=='compute-apps':raise subprocess.TimeoutExpired('nvidia-smi',8)
        return [['0','B300','GPU-a','100','0','0','30','50','1000']]
    monkeypatch.setattr(m,'csv_query',query)
    result=m.gpu_sample(SimpleNamespace(containers=SimpleNamespace(list=lambda:[])))
    assert len(result['gpus'])==1 and result['gpu_error'] is None
    assert result['process_error'] and result['allocation_error'] is None


def test_workspace_ownership_matches_node_and_runtime_and_does_not_mutate_cache():
    workspace={'id':'w1','name':'my-training','node_id':'n1','runtime_id':'container-a',
        'owner_id':'u1','username':'alice','project_id':'p1','project_name':'research','status':'running','gpu':2}
    def task(pid, cid, wid, memory):
        return {'pid':pid,'container_id':cid,'workspace_id':wid,'user':'root','memory_mib':memory}
    metrics={'gpus':[
        {'index':0,'uuid':'GPU-a','utilization_percent':80,'allocations':[task(0,'container-a','w1',0)],
         'processes':[task(123,'container-a','w1',20),task(999,'external','w1',70)]},
        {'index':1,'uuid':'GPU-b','utilization_percent':40,'allocations':[task(0,'container-a','w1',0)],
         'processes':[task(123,'container-a','w1',30)]}], 'process_error':None}
    result=m.workspace_metrics(metrics,'n1',[workspace])
    row=result['workspaces'][0]
    assert row['username']=='alice' and len(row['devices'])==2
    assert row['process_count']==1 and row['process_memory_mib']==50
    assert result['gpus'][0]['processes'][0]['workspace']['username']=='alice'
    assert result['gpus'][0]['processes'][1]['workspace'] is None
    assert 'workspace' not in metrics['gpus'][0]['processes'][0]
    assert m.workspace_metrics(metrics,'n2',[workspace])['gpus'][0]['processes'][0]['workspace'] is None


def test_idle_workspace_keeps_allocation_and_missing_memory_stays_unknown():
    workspace={'id':'w1','name':'idle','node_id':'n1','runtime_id':'container-a','owner_id':'u1',
        'username':'alice','project_id':'p1','project_name':'research','status':'running','gpu':1}
    metrics={'gpus':[{'index':0,'uuid':'GPU-a','utilization_percent':0,
        'allocations':[{'container_id':'container-a','workspace_id':'w1'}], 'processes':[]}], 'process_error':None}
    row=m.workspace_metrics(metrics,'n1',[workspace])['workspaces'][0]
    assert len(row['devices'])==1 and row['process_count']==0 and row['process_memory_mib']==0
    metrics['gpus'][0]['processes']=[{'pid':1,'container_id':'container-a','workspace_id':'w1','memory_mib':None}]
    assert m.workspace_metrics(metrics,'n1',[workspace])['workspaces'][0]['process_memory_mib'] is None


def test_command_credentials_are_redacted():
    value = m.command_text(['python', '--password=abc', '--token', 'def', 'API_KEY=ghi',
                            'redis://user:pw@host/db', 'https://host/?access_token=jkl'])
    for secret in ['=abc', 'def', '=ghi', ':pw@', '=jkl']: assert secret not in value


def test_unavailable_samples_are_reported_explicitly(monkeypatch):
    monkeypatch.setattr(m, 'csv_query', lambda *a: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(m, 'HOST_PROC', Path('/missing-monitor-proc'))
    monkeypatch.setattr(m, '_cached', None)
    result = m.sample(None, Path('/shared'))
    assert result['cpu'] is None and result['memory'] is None
    assert len(result['warnings']) == 3 and result['gpu_error']
    assert result['allocation_error'] and result['process_error']


def test_monitoring_rejects_auditor_before_reading_nodes():
    from fastapi import HTTPException
    from app.main import monitoring
    with pytest.raises(HTTPException) as error: monitoring(SimpleNamespace(role='auditor'), None)
    assert error.value.status_code == 403

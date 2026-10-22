'use strict';
window.ResourceMonitor = (() => {
  let selected = '', busy = false, last, lastScope;
  const percent = v => Number.isFinite(v) ? `${v.toFixed(1)}%` : '—';
  const bytes = v => {
    if (!Number.isFinite(v)) return '—';
    const unit = v >= 1024 ** 4 ? 'TiB' : 'GiB';
    return `${(v / 1024 ** (unit === 'TiB' ? 4 : 3)).toFixed(1)} ${unit}`;
  };
  const bar = v => `<div class="bar monitor-bar"><i style="width:${Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : 0}%"></i></div>`;
  const mib = v => bytes(v == null ? null : v * 1024 ** 2);
  const states = {queued:'等待资源',starting:'启动中',running:'运行中',stopping:'停止中',stopped:'已停止',failed:'失败',pending_approval:'待审批',rejected:'已拒绝',deleting:'删除中'};

  function processes(rows, ui) {
    const {esc, table, date} = ui;
    return table(['GPU / PID', '平台用户 / 系统用户', '开发环境 / 容器', '启动命令 / 启动时间', '进程显存'], rows.map(p => {
      const w = p.workspace;
      return `<tr><td><strong>${p.gpu_index == null ? '' : 'GPU ' + esc(p.gpu_index) + ' · '}${esc(p.pid)}</strong></td><td>${w ? esc(w.username) : '<span class="monitor-unlinked">未关联平台账号</span>'}<span class="subtext">系统用户 ${esc(p.user || '未知')}${p.user_uid == null ? '' : ' · UID ' + esc(p.user_uid)}</span></td><td>${w ? esc(w.name) : '平台外进程'}<span class="subtext">${esc(p.container || '宿主机或未识别容器')}</span></td><td class="monitor-command"><code>${esc(p.command || p.process_name)}</code><span class="subtext">${p.started_at ? date(p.started_at) : '启动时间不可用'}</span></td><td>${mib(p.memory_mib)}</td></tr>`;
    }));
  }

  function workspaces(metrics, ui) {
    const {esc, table} = ui;
    const rows = metrics.workspaces || [];
    let html = '<div class="monitor-section-title">开发环境 GPU 分配与使用</div><p class="monitor-help">分配数来自运行容器的 GPU 访问权限；停止训练进程不会释放环境的 GPU。整卡利用率与该环境进程显存分别显示。</p>';
    if (!rows.length) return html + `<div class="monitor-empty-workspaces">${ui.scope === 'allocated' ? '当前没有可监控的个人 GPU 环境。' : '当前没有申请 GPU 的开发环境。平台外容器及宿主机进程可在下方逐卡查看。'}</div>`;
    html += table(['开发环境', '所属用户 / 项目', '状态', '申请 / 实际分配', '已分配 GPU 的利用率', '环境 GPU 进程'], rows.map(w => {
      const tasks = w.processes || [];
      const devices = (w.devices || []).map(g => `<span class="monitor-device">GPU ${esc(g.index)} · ${percent(g.utilization_percent)}</span>`).join('');
      const detail = metrics.process_error ? '<span class="monitor-unlinked">进程采集不可用</span>' : tasks.length ? `<details class="monitor-workspace-tasks" data-gpu="${esc('workspace:' + w.id)}" ${ui.opened?.get('workspace:' + w.id) ? 'open' : ''}><summary>${esc(w.process_count)} 个进程 · 显存合计 ${mib(w.process_memory_mib)}</summary>${processes(tasks, ui)}</details>` : '未检测到 GPU 计算进程';
      return `<tr><td><strong>${esc(w.name)}</strong><span class="subtext">${w.gpu_mode === 'shared' ? 'GPU 共享访问' : 'GPU 独占分配'}</span></td><td>${esc(w.username)}<span class="subtext">${esc(w.project_name)}</span></td><td><span class="badge ${esc(w.status)}">${esc(states[w.status] || w.status)}</span></td><td>申请 ${esc(w.gpu)} 张 / ${metrics.allocation_error ? '分配采集不可用' : '已分配 ' + (w.devices || []).length + ' 张'}</td><td>${devices || (metrics.allocation_error ? '—' : '尚未分配 GPU')}</td><td>${detail}</td></tr>`;
    }));
    return html;
  }

  function draw(nodes, ui) {
    const {esc, head, table, date, container} = ui;
    const scoped = ui.scope === 'allocated';
    const opened = new Map([...container.querySelectorAll('details[data-gpu]')].map(e => [e.dataset.gpu, e.open]));
    const options = '<option value="">全部节点</option>' + nodes.map(n => `<option value="${esc(n.id)}" ${n.id === selected ? 'selected' : ''}>${esc(n.name)} · ${esc(n.ip)}</option>`).join('');
    let html = head('实时监控', scoped ? '每 5 秒刷新：查看自己已分配 GPU 上的全部进程，包含共享卡上其他任务的占用。' : '每 5 秒刷新 CPU、内存、存储与 GPU 使用量，关联平台用户、开发环境和实际进程。', '<button class="button secondary" data-action="refresh-monitor">立即刷新</button>');
    html += `<div class="monitor-toolbar"><label>计算节点<select id="monitor-node">${options}</select></label><span>${scoped ? 'CPU、内存和存储为所在节点整体用量；GPU 仅展示自己的已分配卡。' : '本地磁盘按文件系统去重；JuiceFS 显示共享存储总用量。'}</span></div><div id="monitor-error" class="notice warn" hidden></div>`;
    for (const n of nodes.filter(n => !selected || n.id === selected)) {
      const m = n.metrics;
      if (!n.online || !m) {
        html += `<section class="panel"><div class="panel-head"><h2>${esc(n.name)} · ${esc(n.ip)}</h2><span class="badge failed">监控不可用</span></div><div class="panel-body">${esc(n.error || '节点暂时没有监控数据')}</div></section>`;
        continue;
      }
      const gpus = m.gpus || [], values = gpus.map(g => g.utilization_percent).filter(Number.isFinite);
      const gpuUtil = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
      const tasks = new Set(gpus.flatMap(g => (g.processes || []).map(p => p.pid))).size;
      const disks = m.storage || [], shared = disks.find(d => d.shared && d.available), disk = shared || disks.find(d => d.available);
      const stats = [
        ['CPU 使用率', percent(m.cpu?.used_percent), `${m.cpu?.cores ?? '—'} 核 · 1 分钟负载 ${m.cpu?.load_average?.[0] ?? '—'}`],
        ['内存使用率', percent(m.memory?.used_percent), `${bytes(m.memory?.used_bytes)} / ${bytes(m.memory?.total_bytes)}`],
        [scoped ? '已分配 GPU 平均利用率' : 'GPU 平均利用率', percent(gpuUtil), `${gpus.length} 张卡 · ${tasks} 个计算进程 · ${(m.workspaces || []).filter(w => w.devices?.length).length} 个已分配环境`],
        [shared ? 'JuiceFS 使用率' : '磁盘使用率', percent(disk?.used_percent), `${bytes(disk?.used_bytes)} / ${bytes(disk?.total_bytes)}`]
      ];
      html += `<section class="monitor-node"><div class="monitor-node-head"><h2>${esc(n.name)} <small>${esc(n.ip)}</small></h2><span class="monitor-sample ${Date.now() / 1000 - m.collected_at > 20 ? 'error' : ''}">采样 ${date(m.collected_at)}${Date.now() / 1000 - m.collected_at > 20 ? ' · 样本已过期' : ''}</span></div>`;
      html += `<div class="stats">${stats.map(([label, value, sub]) => `<div class="stat"><div class="label">${label}</div><div class="number">${value}</div><div class="sub">${esc(sub)}</div></div>`).join('')}</div>`;
      if (m.warnings?.length) html += `<div class="notice warn">${m.warnings.map(esc).join('；')}</div>`;
      html += `<div class="monitor-section-title">存储使用情况</div>` + table(['挂载目录', '类型', '已用 / 总量', '可用空间', '使用率'], disks.map(d => `<tr><td class="code">${esc(d.mountpoint)}</td><td>${d.shared ? 'JuiceFS · 共享' : '本地磁盘'}</td><td>${d.available ? `${bytes(d.used_bytes)} / ${bytes(d.total_bytes)}` : esc(d.error || '不可用')}</td><td>${d.available ? bytes(d.available_bytes) : '—'}</td><td>${percent(d.used_percent)}${bar(d.used_percent)}</td></tr>`));
      html += workspaces(m, {...ui, opened});
      html += '<div class="monitor-section-title">GPU 与运行任务</div>';
      if (m.gpu_error) html += `<div class="notice warn">${esc(m.gpu_error)}</div>`;
      if (m.process_error) html += `<div class="notice warn">${esc(m.process_error)}，下方进程列表可能不完整。</div>`;
      if (m.allocation_error) html += `<div class="notice warn">${esc(m.allocation_error)}，GPU 分配信息可能不完整。</div>`;
      html += `<p class="monitor-help">${scoped ? '以下为已分配卡上的全部计算进程。共享卡的整卡利用率包含其他任务；自己的环境进程在上方单独统计。' : '平台外进程显示真实系统用户与容器；未关联的平台账号不根据容器名称推断。容器拥有 GPU 访问权限不代表该卡正在计算。'}</p>`;
      html += '<div class="monitor-gpu-grid">';
      for (const gpu of gpus) {
        const key = n.id + ':' + gpu.uuid;
        const memPct = gpu.memory_total_mib > 0 && Number.isFinite(gpu.memory_used_mib) ? gpu.memory_used_mib / gpu.memory_total_mib * 100 : null;
        html += `<article class="monitor-gpu"><div class="monitor-gpu-head"><div><h3>GPU ${esc(gpu.index)}</h3><span>${esc(gpu.name)}</span></div><strong>${percent(gpu.utilization_percent)}</strong></div>`;
        html += `<div class="monitor-gpu-memory">显存 ${bytes(gpu.memory_used_mib == null ? null : gpu.memory_used_mib * 1024 ** 2)} / ${bytes(gpu.memory_total_mib == null ? null : gpu.memory_total_mib * 1024 ** 2)}${bar(memPct)}</div>`;
        html += `<div class="monitor-gpu-meta"><span>温度 ${gpu.temperature_c ?? '—'} °C</span><span>功耗 ${gpu.power_w ?? '—'} / ${gpu.power_limit_w ?? '—'} W</span></div>`;
        const allocations = gpu.allocations || [];
        const assigned = allocations.filter(a => a.workspace), external = allocations.filter(a => !a.workspace);
        html += `<details class="monitor-allocations" data-gpu="${esc(key + ':allocation')}" ${opened.get(key + ':allocation') ? 'open' : ''}><summary>分配 / 访问权限 · ${assigned.length} 个开发环境 · ${external.length} 个平台外容器</summary>`;
        html += allocations.length ? `<ul>${allocations.map(a => `<li>${a.workspace ? `<strong>${esc(a.workspace.username)}</strong> · ${esc(a.workspace.name)}` : '平台外容器'}<span class="subtext">${esc(a.container)}</span></li>`).join('')}</ul>` : `<div class="monitor-no-task">${m.allocation_error ? '容器分配信息暂不可用' : '未检测到容器 GPU 分配；宿主机进程见下方列表。'}</div>`;
        html += '</details>';
        html += `<details data-gpu="${esc(key)}" ${opened.get(key) === false ? '' : 'open'}><summary>运行任务 · ${(gpu.processes || []).length} 个</summary>`;
        if (!gpu.processes?.length) {
          html += `<div class="monitor-no-task">${m.process_error ? '进程数据暂不可用' : '当前未检测到 GPU 计算进程'}</div>`;
        } else {
          html += processes(gpu.processes, ui);
        }
        html += '</details></article>';
      }
      if (!gpus.length && !m.gpu_error) html += `<div class="empty">${scoped ? '当前没有可确认分配给你的 GPU；环境未启动或分配信息暂不可用。' : '该节点没有检测到 NVIDIA GPU。'}</div>`;
      html += '</div></section>';
    }
    if (!nodes.length) html += `<div class="empty">${scoped ? '当前没有正在使用的开发环境节点。环境启动并实际分配 GPU 后，可在这里查看卡上进程。' : '尚未登记计算节点。'}</div>`;
    container.innerHTML = html;
    container.querySelector('#monitor-node').onchange = e => { selected = e.target.value; draw(nodes, ui); };
  }

  async function render(ui) {
    if (busy) return;
    if (lastScope !== ui.scope) { last = undefined; selected = ''; lastScope = ui.scope; }
    busy = true;
    try {
      const result = await ui.api('/monitoring');
      if (!ui.isCurrent()) return;
      last = result.nodes || [];
      draw(last, ui);
    } catch (e) {
      if (!ui.isCurrent()) return;
      if (!last) draw([], ui);
      const error = ui.container.querySelector('#monitor-error');
      if (error) { error.hidden = false; error.textContent = '刷新失败，当前展示上次采样。' + e.message; }
    } finally { busy = false; }
  }
  return {render};
})();

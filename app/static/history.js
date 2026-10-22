'use strict';
window.ResourceHistory = (() => {
  let filters = {owner_id:'', project_id:'', page:1}, range = 7, result;
  const n = v => Number.isFinite(v) ? v.toLocaleString('zh-CN', {maximumFractionDigits:2}) : '—';
  const pct = v => Number.isFinite(v) ? n(v) + '%' : '—';
  const local = t => {const d = new Date(t * 1000); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0,16);};
  function chart(points, esc, date) {
    const width = 1000, height = 220, left = 45, top = 18, bottom = 185;
    let segments = [], current = [];
    points.forEach((p, i) => {
      if (!Number.isFinite(p.gpu_avg_percent)) {if (current.length) segments.push(current); current = []; return;}
      current.push([left + i / Math.max(1, points.length - 1) * (width - left - 20), bottom - p.gpu_avg_percent / 100 * (bottom - top)]);
    });
    if (current.length) segments.push(current);
    return `<svg class="history-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="关联物理GPU平均利用率历史曲线">${[0,25,50,75,100].map(v => {
      const y = bottom - v / 100 * (bottom - top); return `<line x1="${left}" y1="${y}" x2="980" y2="${y}" stroke="#e6ebf2"/><text x="5" y="${y + 4}">${v}%</text>`;
    }).join('')}${segments.map(s => s.length === 1 ? `<circle cx="${s[0][0]}" cy="${s[0][1]}" r="3" fill="#2466df"/>` : `<polyline points="${s.map(p=>p.join(',')).join(' ')}" fill="none" stroke="#2466df" stroke-width="2.5"/>`).join('')}<text x="${left}" y="214">${esc(date(points[0]?.time || 0))}</text><text x="980" y="214" text-anchor="end">${esc(date(points.at(-1)?.time || 0))}</text></svg>`;
  }
  function query() {
    const q = new URLSearchParams();
    for (const [k,v] of Object.entries(filters)) if (v !== '') q.set(k,v);
    return q.toString();
  }
  async function render(ui) {
    if (!filters.start) {filters.end = Date.now()/1000; filters.start = filters.end - range * 86400;}
    const data = await ui.api('/resource-history?' + query());
    if (!ui.isCurrent()) return;
    result = data;
    const {esc, date, table, head, container} = ui;
    const pick = (key, label, values) => `<label>${label}<select name="${key}"><option value="">全部</option>${values.map(v=>`<option value="${esc(v.id)}" ${filters[key]===v.id?'selected':''}>${esc(v.name)}</option>`).join('')}</select></label>`;
    let html = head('资源使用统计', '按用户、项目查看容器分配历史与 GPU 利用率；分配记录长期保留，指标保留 30 天。');
    html += `<form id="history-filter" class="history-filters"><label>开始时间<input type="datetime-local" name="start" value="${local(filters.start)}" required></label><label>结束时间<input type="datetime-local" name="end" value="${local(filters.end)}" required></label>${pick('owner_id','用户',data.choices.users)}${pick('project_id','项目',data.choices.projects)}<button type="submit" class="button primary">查询</button><button type="button" id="history-now" class="button secondary">最近 24 小时</button></form>`;
    html += `<div class="history-exports">${[['allocations','导出分配记录'],['users','导出用户统计'],['projects','导出项目统计'],['gpu','导出 GPU 统计']].map(([kind,title])=>`<a class="button secondary" href="/api/resource-history/export?${esc(query())}&amp;kind=${kind}">${title} CSV</a>`).join('')}</div>`;
    const c = data.collection, s = data.summary;
    html += `<p class="monitor-help">每 ${c.interval_seconds} 秒采集。${c.last_sample?'最近有效采样：'+date(c.last_sample):'所选区间暂无匹配的有效 GPU 样本'}。${c.failed_node_samples?'节点采集失败 '+c.failed_node_samples+' 次；缺失值不按 0 计算。':''} 上线前没有完整采样；已有容器从首次观测时开始记账。</p>`;
    if (c.last_sample && Date.now()/1000-c.last_sample>180 && filters.end>Date.now()/1000-180) html += '<div class="notice warn">最近采样已超过 3 分钟，请检查节点监控连接。</div>';
    html += `<div class="stats">${[['容器运行会话',n(s.sessions),'所选区间内有资源分配的会话'],['独占 GPU 卡时',n(s.exclusive_gpu_hours),'实际分配卡数 × 区间占用小时'],['共享 GPU 访问卡时',n(s.shared_gpu_access_hours),'访问权限时长，单列统计'],['关联整卡平均利用率',pct(s.gpu_avg_percent),n(s.gpu_samples)+' 个有效卡样本，物理卡去重']].map(([label,value,sub])=>`<div class="stat"><div class="label">${label}</div><div class="number">${value}</div><div class="sub">${sub}</div></div>`).join('')}</div>`;
    html += `<div class="notice">${esc(data.note)} 利用率表示采样期间 GPU 的忙碌程度，不等同于有效训练算力。无采样的区间显示空缺。</div>`;
    html += `<section class="panel"><div class="panel-head"><h2>GPU 利用率趋势</h2><span class="subtext">约 240 个时间分组，采样算术均值</span></div><div class="panel-body">${s.gpu_samples?chart(data.timeline,esc,date):'<div class="empty">暂无有效 GPU 指标；采集后将在这里显示历史曲线。</div>'}</div></section>`;
    html += '<div class="monitor-section-title">逐卡使用统计</div>' + table(['节点 / GPU','平均 / 峰值利用率','峰值显存','有效卡样本'],data.gpus.map(g=>`<tr><td>${esc(g.node_name)} · GPU ${esc(g.index)}<span class="subtext">${esc(g.uuid)}</span></td><td>${pct(g.gpu_avg_percent)} / ${pct(g.gpu_max_percent)}</td><td>${n(g.gpu_memory_peak_mib == null ? null : g.gpu_memory_peak_mib/1024)} GiB</td><td>${n(g.gpu_samples)}</td></tr>`));
    for (const [key,title] of [['users','按用户统计'],['projects','按项目统计']]) html += `<div class="monitor-section-title">${title}</div>` + table([key==='users'?'用户':'项目','会话数','CPU 核时','内存 GiB 时','独占 GPU 卡时','共享 GPU 访问卡时','关联整卡平均利用率'],data[key].map(g=>`<tr><td>${esc(g.name)}</td><td>${n(g.sessions)}</td><td>${n(g.cpu_core_hours)}</td><td>${n(g.memory_gib_hours)}</td><td>${n(g.exclusive_gpu_hours)}</td><td>${n(g.shared_gpu_access_hours)}</td><td>${pct(g.gpu_avg_percent)}</td></tr>`));
    html += '<div class="monitor-section-title">容器资源分配历史</div>' + table(['环境 / 用户 / 项目','节点 / 容器 / 镜像','CPU / 内存 / GPU','持久存储分配','分配起点 / 释放时间','区间时长 / 释放原因'],data.allocations.map(a=>{
      const v=a.snapshot;
      return `<tr><td><strong>${esc(v.name)}</strong><span class="subtext">${esc(v.username)} · ${esc(v.project_name)}</span></td><td>${esc(v.node_name)}<span class="subtext">${esc(a.runtime_id.slice(0,12))}</span><span class="subtext history-image">${esc(v.image_ref)}</span></td><td>${n(v.cpu)} 核 / ${n(v.memory_gib)} GiB / ${v.devices.length} GPU<span class="subtext">申请 ${n(v.gpu)} 张 · ${v.gpu_mode==='shared'?'共享访问':'独占分配'}</span><details><summary>GPU UUID</summary>${v.devices.map(esc).join('<br>')||'无'}</details></td><td>${(v.mounts||[]).map(m=>`${esc(m.path)} · ${m.elastic?'弹性不限额':n(m.size_gib)+' GiB'}`).join('<br>')||'无'}</td><td>${date(a.started_at)}<span class="subtext">${a.start_source==='observed'?'上线时开始观测':'启动时记录'}</span><span class="subtext">${a.ended_at?date(a.ended_at):'尚未释放'}</span></td><td>${n(a.period_hours)} 小时<span class="subtext">${esc(a.end_reason)}</span></td></tr>`;
    }));
    html += `<div class="history-pages"><span>共 ${n(data.total)} 条 · 第 ${data.page} / ${Math.max(1,Math.ceil(data.total/data.page_size))} 页</span><button class="button secondary" id="history-prev" ${data.page<=1?'disabled':''}>上一页</button><button class="button secondary" id="history-next" ${data.page*data.page_size>=data.total?'disabled':''}>下一页</button></div>`;
    container.innerHTML = html;
    const reload = async () => {try {await render(ui);} catch(e) {ui.toast(e.message);}};
    container.querySelector('#history-filter').onsubmit = e => {
      e.preventDefault();const f=new FormData(e.target);const start=new Date(f.get('start')).getTime()/1000,end=new Date(f.get('end')).getTime()/1000;
      if (!(end>start) || end-start>31*86400) {ui.toast('请选择不超过 31 天的有效时间区间');return;}
      filters={start,end,owner_id:f.get('owner_id'),project_id:f.get('project_id'),page:1};reload();
    };
    container.querySelector('#history-now').onclick=()=>{const end=Date.now()/1000;filters={...filters,start:end-86400,end,page:1};reload();};
    container.querySelector('#history-prev').onclick=()=>{filters.page--;reload();};
    container.querySelector('#history-next').onclick=()=>{filters.page++;reload();};
  }
  return {render};
})();

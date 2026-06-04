/* ═════════════════════════════════════════════
   AgenticSRE Dashboard — Main SPA Logic
   Blue-White Theme with Chart.js Metrics
   ═════════════════════════════════════════════ */

// ── State ──
const state = {
    currentView: 'overview',
    refreshTimer: null,
    refreshInterval: 10,
    sseConnections: {},
    rcaRunId: null,
    daemonLogSSE: null,
    detectionSSE: null,
    podChart: null,
    metricRange: '1h',
    charts: {},  // chart instances keyed by canvas id
};

// Chart color palette (blue theme)
const CHART_COLORS = [
    '#1e6fd9', '#0ea5e9', '#10b981', '#8b5cf6',
    '#f59e0b', '#ef4444', '#06b6d4', '#ec4899',
    '#14b8a6', '#6366f1',
];

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initRefresh();
    loadOverview();
    healthCheck();
    setInterval(healthCheck, 30000);
});

// ─────────────────────────────────────────
// Navigation
// ─────────────────────────────────────────

function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const view = item.dataset.view;
            switchView(view);
        });
    });

    document.getElementById('toggle-sidebar').addEventListener('click', () => {
        const sidebar = document.getElementById('sidebar');
        sidebar.classList.toggle('collapsed');
        document.body.classList.toggle('sidebar-collapsed');
    });
}

function switchView(viewId) {
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    document.querySelector(`.nav-item[data-view="${viewId}"]`)?.classList.add('active');

    document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
    document.getElementById(`view-${viewId}`)?.classList.add('active');

    const titles = {
        overview: '集群概览', metrics: '指标监控', logs: '日志查询',
        alerts: '告警中心', rca: '根因分析', traces: '链路追踪',
        daemon: '守护进程', knowledge: '故障知识库', evolution: '演化闭环',
        configcenter: '配置中心', healcenter: '自愈中心',
        faultlab: '通算故障实验', llmfaultlab: '智算故障实验', reports: '故障报告',
        events: '事件追踪', hermes: 'Hermes 智能助手'
    };
    const subtitles = {
        overview: 'Cluster health, workload topology, and recent operational signals',
        metrics: 'Prometheus-backed infrastructure, network, container, and business metrics',
        logs: 'Namespace-scoped log search for diagnosis evidence collection',
        alerts: 'Alert grouping, detection rules, and noise reduction controls',
        rca: 'Multi-agent root-cause analysis with evidence, critique, and closure',
        traces: 'Distributed traces and service-level execution paths',
        daemon: 'Background detection daemon and streaming operational signals',
        knowledge: 'Fault knowledge, supervised feedback, and memory governance',
        evolution: 'SoW alignment, HITL queue, and agent evolution telemetry',
        configcenter: 'Runtime configuration for LLM, anomaly detection, and self-healing policy',
        healcenter: 'Self-healing knowledge base, recommendations, dry-run execution, and rollback records',
        faultlab: 'General-compute K8s/AIOpsLab controlled fault experiments',
        llmfaultlab: 'LLM inference and vLLM runtime fault experiments on T4 environments',
        reports: 'Historical RCA reports and exportable diagnostic summaries',
        events: 'Kubernetes event stream for cluster state changes',
        hermes: 'Interactive operations assistant for investigation and command execution'
    };
    document.getElementById('view-title').textContent = titles[viewId] || viewId;
    const subtitle = document.getElementById('view-subtitle');
    if (subtitle) subtitle.textContent = subtitles[viewId] || '';

    state.currentView = viewId;
    refreshCurrentView();
}

function refreshCurrentView() {
    const loaders = {
        overview: loadOverview,
        metrics: loadMetrics,
        logs: () => loadNamespaces('log-ns'),
        alerts: () => { loadAlertList(); loadDetectionConfig(); },
        rca: loadRCAHistory,
        traces: loadTracesView,
        daemon: loadDaemonStatus,
        configcenter: loadConfigCenter,
        healcenter: loadHealCenter,
        knowledge: loadKnowledge,
        evolution: loadEvolution,
        faultlab: loadFaultLab,
        llmfaultlab: loadLLMFaultLab,
        reports: loadReportList,
        events: () => { Promise.all([loadNamespaces('event-ns'), loadEvents()]); },
    };
    (loaders[state.currentView] || (() => {}))();
}

// ── Auto-Refresh ──

function initRefresh() {
    document.getElementById('refresh-interval').addEventListener('change', (e) => {
        state.refreshInterval = parseInt(e.target.value);
        clearInterval(state.refreshTimer);
        if (state.refreshInterval > 0) {
            state.refreshTimer = setInterval(refreshCurrentView, state.refreshInterval * 1000);
        }
    });
    state.refreshTimer = setInterval(refreshCurrentView, 10000);
}

// ─────────────────────────────────────────
// API Helpers
// ─────────────────────────────────────────

async function api(path, options = {}) {
    let timer = null;
    try {
        const timeoutMs = options.timeoutMs || 30000;
        const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        if (controller) timer = setTimeout(() => controller.abort(), timeoutMs);
        const fetchOptions = { ...options };
        delete fetchOptions.timeoutMs;
        if (controller && !fetchOptions.signal) fetchOptions.signal = controller.signal;
        const res = await fetch(path, fetchOptions);
        const text = await res.text();
        let data = null;
        try { data = text ? JSON.parse(text) : {}; } catch { data = { message: text || res.statusText }; }
        if (!res.ok) {
            const msg = typeof data?.detail === 'string'
                ? data.detail
                : data?.detail?.message || data?.message || data?.error || `HTTP ${res.status}`;
            const err = new Error(msg);
            err.status = res.status;
            err.data = data;
            throw err;
        }
        return data;
    } catch (e) {
        console.error(`API error [${path}]:`, e);
        if (options.throwOnError) throw e;
        return null;
    } finally {
        if (timer) clearTimeout(timer);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatTime(ts) {
    if (!ts) return '-';
    try { return new Date(ts).toLocaleString('zh-CN'); } catch { return ts; }
}

function badgeClass(phase) {
    const map = { Running: 'success', Succeeded: 'success', Pending: 'warning', Failed: 'danger', Unknown: 'gray' };
    return map[phase] || 'gray';
}

// ─────────────────────────────────────────
// Overview
// ─────────────────────────────────────────

async function loadOverview() {
    const [data, nodesData, events] = await Promise.all([
        api('/api/cluster/overview'),
        api('/api/cluster/nodes'),
        api('/api/cluster/events?limit=5'),
    ]);

    if (data) {
        animateValue('stat-nodes', data.nodes || 0);
        animateValue('stat-pods', data.pods_total || 0);
        animateValue('stat-ns', data.namespaces || 0);
    }

    if (nodesData?.nodes) {
        document.getElementById('node-grid').innerHTML = nodesData.nodes.map(n => `
            <div class="node-card">
                <div class="node-name">${escapeHtml(n.name)}</div>
                <div class="node-meta">
                    <span class="badge badge-${n.ready === 'True' ? 'success' : 'danger'}">${n.ready === 'True' ? 'Ready' : 'NotReady'}</span>
                    ${n.roles.map(r => `<span class="badge badge-info">${r}</span>`).join(' ')}
                </div>
                <div class="node-meta" style="margin-top:4px">CPU: ${n.cpu} | Mem: ${n.memory} | ${n.version}</div>
            </div>
        `).join('');
    }

    if (events?.events) {
        const warnings = events.events.filter(e => e.type === 'Warning');
        document.getElementById('alert-preview').innerHTML = warnings.length
            ? warnings.map(e => `
                <div class="signal-item">
                    <span><span class="badge badge-warning">${e.reason}</span> ${escapeHtml(e.message?.substring(0, 100) || '')}</span>
                    <span class="text-muted">${e.object}</span>
                </div>
            `).join('')
            : '<p class="text-muted" style="padding:12px">暂无告警</p>';
    }

    // Load topology + pod chart
    loadTopology();
    if (data?.pod_phases) renderPodChart(data.pod_phases);
}

function animateValue(elemId, target) {
    const el = document.getElementById(elemId);
    if (!el) return;
    const current = parseInt(el.textContent) || 0;
    if (current === target) { el.textContent = target; return; }
    const diff = target - current;
    const steps = Math.min(Math.abs(diff), 20);
    const stepVal = diff / steps;
    let i = 0;
    const timer = setInterval(() => {
        i++;
        if (i >= steps) { el.textContent = target; clearInterval(timer); }
        else { el.textContent = Math.round(current + stepVal * i); }
    }, 30);
}

function renderPodChart(phases) {
    const canvas = document.getElementById('pod-chart');
    const ctx = canvas.getContext('2d');

    if (state.podChart) { state.podChart.destroy(); }

    const entries = Object.entries(phases);
    const colors = {
        Running: '#10b981', Succeeded: '#0ea5e9',
        Pending: '#f59e0b', Failed: '#ef4444', Unknown: '#a0aec0'
    };

    state.podChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: entries.map(([k]) => k),
            datasets: [{
                data: entries.map(([, v]) => v),
                backgroundColor: entries.map(([k]) => colors[k] || '#a0aec0'),
                borderWidth: 2,
                borderColor: '#ffffff',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '60%',
            plugins: {
                legend: { position: 'bottom', labels: { padding: 16, font: { size: 12 } } },
            },
            animation: { animateRotate: true, duration: 800 },
        }
    });
}

// ─────────────────────────────────────────
// Operational Topology
// ─────────────────────────────────────────

const _topo = { topoData: null, nsFilter: '' };

const NS_COLORS = ['#60a5fa','#34d399','#a78bfa','#fbbf24','#f472b6','#22d3ee','#fb923c','#818cf8','#4ade80','#e879f9','#38bdf8','#facc15','#c084fc','#2dd4bf','#f87171'];

function _nsColor(ns, nsList) {
    const idx = nsList.indexOf(ns);
    return NS_COLORS[idx % NS_COLORS.length];
}

async function loadTopology() {
    const data = await api('/api/cluster/topology');
    if (!data) return;
    _topo.topoData = data;

    const s = data.summary || {};
    animateValue('stat-nodes', s.total_nodes || 0);
    animateValue('stat-deploys', s.total_deployments || 0);
    animateValue('stat-services', s.total_services || 0);
    animateValue('stat-pods', s.total_pods || 0);
    animateValue('stat-ns', data.namespaces?.length || 0);
    animateValue('stat-faulty', (s.faulty_pods || 0) + (s.faulty_deployments || 0));

    const sel = document.getElementById('topo-ns-filter');
    if (sel && data.namespaces) {
        const cur = sel.value;
        sel.innerHTML = '<option value="">所有命名空间</option>' +
            data.namespaces.map(ns => `<option value="${ns}" ${ns===cur?'selected':''}>${ns}</option>`).join('');
    }

    renderTopology(data);
}

function filterTopologyNs() {
    _topo.nsFilter = document.getElementById('topo-ns-filter')?.value || '';
    if (_topo.topoData) renderTopology(_topo.topoData);
}

function renderTopology(data) {
    const nsFilter = _topo.nsFilter;
    const pods = nsFilter ? data.pods.filter(p => p.namespace === nsFilter) : data.pods;
    const deployments = nsFilter ? data.deployments.filter(d => d.namespace === nsFilter) : data.deployments;
    const services = nsFilter ? data.services.filter(s => s.namespace === nsFilter) : data.services;
    const nodes = data.nodes || [];

    renderTopologyHealth(data, pods, deployments, services);
    renderTopologyNodes(nodes, pods, deployments, services);
    renderTopologyFaults(pods, deployments);
    renderTopologyNamespaces(data.namespaces || [], pods, deployments);
}

function renderTopologyHealth(data, pods, deployments, services) {
    const strip = document.getElementById('topo-health-strip');
    if (!strip) return;
    const faultyPods = pods.filter(p => p.is_faulty).length;
    const faultyDeploys = deployments.filter(d => d.is_faulty).length;
    const readyPods = pods.filter(p => p.phase === 'Running' && !p.is_faulty).length;
    strip.innerHTML = [
        ['Pods', `${readyPods}/${pods.length}`, faultyPods ? 'warning' : 'success'],
        ['Deployments', `${deployments.filter(d => !d.is_faulty).length}/${deployments.length}`, faultyDeploys ? 'warning' : 'success'],
        ['Services', String(services.length), 'info'],
        ['Namespaces', String(nsFilterOrAll(data.namespaces?.length || 0)), 'info'],
    ].map(([label, value, kind]) => `
        <div class="topo-health ${kind}">
            <span>${label}</span>
            <strong>${value}</strong>
        </div>
    `).join('');
}

function nsFilterOrAll(total) {
    return _topo.nsFilter ? '1' : total;
}

function renderTopologyNodes(nodes, pods, deployments, services) {
    const container = document.getElementById('topo-node-map');
    if (!container) return;

    const podsByNode = {};
    pods.forEach(p => {
        const key = p.node || 'unscheduled';
        if (!podsByNode[key]) podsByNode[key] = [];
        podsByNode[key].push(p);
    });

    container.innerHTML = nodes.map(node => {
        const nodePods = podsByNode[node.name] || [];
        const faulty = nodePods.filter(p => p.is_faulty);
        const namespaces = [...new Set(nodePods.map(p => p.namespace))].sort();
        const topNamespaces = namespaces.slice(0, 5).map(ns => `<span>${escapeHtml(ns)}</span>`).join('');
        const podDots = nodePods.slice(0, 80).map(p => `
            <button class="pod-dot ${p.is_faulty ? 'faulty' : p.phase === 'Running' ? 'running' : 'pending'}"
                    title="${escapeHtml(p.namespace + '/' + p.name)} | ${escapeHtml(p.phase)} | Ready ${escapeHtml(p.ready)} | Restarts ${p.restarts}">
            </button>
        `).join('');
        const morePods = nodePods.length > 80 ? `<span class="topo-more">+${nodePods.length - 80}</span>` : '';
        const role = node.roles?.includes('control-plane') ? 'control-plane' : 'worker';

        return `<section class="topo-node-card ${node.ready ? '' : 'faulty'}">
            <div class="topo-node-head">
                <div>
                    <h4>${escapeHtml(node.name)}</h4>
                    <span>${escapeHtml(role)} · CPU ${escapeHtml(node.cpu || '-')} · Mem ${escapeHtml(node.memory || '-')}</span>
                </div>
                <span class="badge badge-${node.ready ? 'success' : 'danger'}">${node.ready ? 'Ready' : 'NotReady'}</span>
            </div>
            <div class="topo-node-metrics">
                <div><strong>${nodePods.length}</strong><span>Pod</span></div>
                <div><strong>${faulty.length}</strong><span>异常</span></div>
                <div><strong>${namespaces.length}</strong><span>NS</span></div>
            </div>
            <div class="topo-namespace-tags">${topNamespaces || '<span>无工作负载</span>'}</div>
            <div class="topo-pod-dots">${podDots}${morePods}</div>
        </section>`;
    }).join('');
}

function renderTopologyFaults(pods, deployments) {
    const el = document.getElementById('topo-fault-list');
    if (!el) return;
    const podFaults = pods.filter(p => p.is_faulty).slice(0, 12).map(p => ({
        type: 'Pod',
        name: p.name,
        namespace: p.namespace,
        detail: p.fault_reason || p.phase,
    }));
    const depFaults = deployments.filter(d => d.is_faulty).slice(0, 8).map(d => ({
        type: 'Deployment',
        name: d.name,
        namespace: d.namespace,
        detail: `${d.available}/${d.replicas} available`,
    }));
    const items = [...podFaults, ...depFaults];
    if (!items.length) {
        el.innerHTML = '<p class="text-muted">当前过滤范围内无异常实体</p>';
        return;
    }
    el.innerHTML = items.map(item => `
        <div class="topo-fault-item">
            <span class="badge badge-danger">${item.type}</span>
            <div>
                <strong>${escapeHtml(item.name)}</strong>
                <p>${escapeHtml(item.namespace)} · ${escapeHtml(item.detail)}</p>
            </div>
        </div>
    `).join('');
}

function renderTopologyNamespaces(namespaces, pods, deployments) {
    const el = document.getElementById('topo-namespace-list');
    if (!el) return;
    const rows = namespaces.map(ns => {
        const nsPods = (_topo.topoData?.pods || []).filter(p => p.namespace === ns);
        const nsDeps = (_topo.topoData?.deployments || []).filter(d => d.namespace === ns);
        const faults = nsPods.filter(p => p.is_faulty).length + nsDeps.filter(d => d.is_faulty).length;
        return { ns, pods: nsPods.length, deps: nsDeps.length, faults };
    }).sort((a, b) => b.faults - a.faults || b.pods - a.pods).slice(0, 14);

    el.innerHTML = rows.map(row => `
        <button class="topo-ns-row ${_topo.nsFilter === row.ns ? 'active' : ''}" onclick="selectTopologyNamespace('${escapeHtml(row.ns)}')">
            <span>${escapeHtml(row.ns)}</span>
            <small>${row.pods} pod · ${row.deps} deploy${row.faults ? ` · ${row.faults} 异常` : ''}</small>
        </button>
    `).join('');
}

function selectTopologyNamespace(ns) {
    const sel = document.getElementById('topo-ns-filter');
    if (sel) sel.value = ns;
    _topo.nsFilter = ns;
    if (_topo.topoData) renderTopology(_topo.topoData);
}

// ─────────────────────────────────────────
// Metrics (Charts + Instant Values)
// ─────────────────────────────────────────

function setMetricRange(range, btn) {
    state.metricRange = range;
    document.querySelectorAll('#metrics-time-range .time-range-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    loadMetricCharts();
}

async function loadMetrics() {
    const ns = document.getElementById('metrics-ns')?.value || '';
    loadNamespaces('metrics-ns');

    // Load both charts and instant values in parallel
    await Promise.all([
        loadMetricCharts(),
        loadInstantMetrics(ns),
    ]);
}

async function loadInstantMetrics(ns) {
    const [metrics, podData] = await Promise.all([
        api(`/api/prometheus/metrics_summary?namespace=${ns || ''}`),
        api(`/api/cluster/pods?namespace=${ns || ''}`),
    ]);

    if (metrics) {
        renderMetricBars('metrics-node-cpu', metrics.node_cpu, '%');
        renderMetricBars('metrics-node-memory', metrics.node_memory, '%');
        renderMetricBars('metrics-node-disk', metrics.node_disk, '%');
        renderContainerTop('metrics-cpu-top', metrics.container_cpu_top, '%');
        renderContainerTop('metrics-mem-top', metrics.container_mem_top, 'MB');
    }

    if (podData?.pods) {
        const tbody = document.querySelector('#pod-table tbody');
        tbody.innerHTML = podData.pods.map(p => `
            <tr>
                <td>${escapeHtml(p.name)}</td>
                <td>${escapeHtml(p.namespace)}</td>
                <td><span class="badge badge-${badgeClass(p.phase)}">${p.phase}</span></td>
                <td>${p.ready}</td>
                <td class="${p.restarts > 5 ? 'text-danger' : ''}">${p.restarts}</td>
                <td>${escapeHtml(p.node || '')}</td>
            </tr>
        `).join('');
    }
}

async function loadMetricCharts() {
    const range = state.metricRange;
    const ns = document.getElementById('metrics-ns')?.value || '';
    const nsParam = ns ? `&namespace=${encodeURIComponent(ns)}` : '';

    // ── Node metrics ──
    const [cpuData, memData, diskData] = await Promise.all([
        api(`/api/prometheus/metric_history?metric_name=node_cpu_usage&duration=${range}`),
        api(`/api/prometheus/metric_history?metric_name=node_memory_usage&duration=${range}`),
        api(`/api/prometheus/metric_history?metric_name=node_disk_usage&duration=${range}`),
    ]);
    renderTimeSeriesChart('chart-node-cpu', cpuData, '%');
    renderTimeSeriesChart('chart-node-memory', memData, '%');
    renderTimeSeriesChart('chart-node-disk', diskData, '%');

    // ── Network metrics ──
    const netQueries = {
        'chart-net-receive': 'sum by(instance)(rate(node_network_receive_bytes_total{device!="lo"}[5m])) / 1024',
        'chart-net-transmit': 'sum by(instance)(rate(node_network_transmit_bytes_total{device!="lo"}[5m])) / 1024',
        'chart-tcp-established': 'node_netstat_Tcp_CurrEstab',
        'chart-tcp-retrans': 'sum by(instance)(rate(node_netstat_Tcp_RetransSegs[5m]))',
        'chart-dns-requests': 'sum by(pod)(rate(coredns_dns_requests_total[5m]))',
    };
    const netUnits = {
        'chart-net-receive': ' KB/s', 'chart-net-transmit': ' KB/s',
        'chart-tcp-established': '', 'chart-tcp-retrans': '/s', 'chart-dns-requests': '/s',
    };
    const netPromises = Object.entries(netQueries).map(([id, q]) =>
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(q)}&duration=${range}`).then(d => [id, d])
    );
    const netResults = await Promise.all(netPromises);
    for (const [id, data] of netResults) {
        renderTimeSeriesChart(id, data, netUnits[id] || '');
    }

    // ── Container metrics — filtered by namespace ──
    const nsFilter = ns ? `namespace="${ns}"` : '';
    const containerCpuQuery = nsFilter
        ? `sum by(pod, namespace)(rate(container_cpu_usage_seconds_total{${nsFilter}}[5m])) * 100`
        : `sum by(pod, namespace)(rate(container_cpu_usage_seconds_total[5m])) * 100`;
    const containerMemQuery = nsFilter
        ? `sum by(pod, namespace)(container_memory_working_set_bytes{${nsFilter}}) / 1024 / 1024`
        : `sum by(pod, namespace)(container_memory_working_set_bytes) / 1024 / 1024`;

    const [ccpuData, cmemData] = await Promise.all([
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(containerCpuQuery)}&duration=${range}&max_series=10`),
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(containerMemQuery)}&duration=${range}&max_series=10`),
    ]);
    renderTimeSeriesChart('chart-container-cpu', ccpuData, '%');
    renderTimeSeriesChart('chart-container-mem', cmemData, 'MB');

    // ── Business metrics (use real cluster metrics) ──
    const nsFilterBiz = ns ? `namespace="${ns}",` : '';
    const bizQueries = {
        'chart-biz-restarts': `sum by(namespace)(rate(kube_pod_container_status_restarts_total{${nsFilterBiz}}[5m])) * 300`,
        'chart-biz-replicas': nsFilter
            ? `sum by(deployment)(kube_deployment_status_replicas_available{${nsFilter}})`
            : `sum by(deployment)(kube_deployment_status_replicas_available)`,
        'chart-biz-apiserver': 'sum by(verb)(rate(apiserver_request_total[5m]))',
    };
    const bizUnits = { 'chart-biz-restarts': '', 'chart-biz-replicas': '', 'chart-biz-apiserver': '/s' };
    const bizPromises = Object.entries(bizQueries).map(([id, q]) =>
        api(`/api/prometheus/metric_history?custom_query=${encodeURIComponent(q)}&duration=${range}&max_series=10`).then(d => [id, d])
    );
    const bizResults = await Promise.all(bizPromises);
    for (const [id, data] of bizResults) {
        renderTimeSeriesChart(id, data, bizUnits[id] || '');
    }
}

function _fmtTs(ts) {
    const d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}

function renderTimeSeriesChart(canvasId, data, unit) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    if (state.charts[canvasId]) {
        state.charts[canvasId].destroy();
        delete state.charts[canvasId];
    }

    // Update anomaly badge on chart header
    const chartCard = canvas.closest('.card');
    if (chartCard) {
        const existing = chartCard.querySelector('.anomaly-badge');
        if (existing) existing.remove();
        if (data?.anomalies?.length > 0) {
            const badge = document.createElement('span');
            badge.className = 'anomaly-badge';
            const methods = [...new Set(data.anomalies.map(a => a.method || 'zscore'))].join('+');
            badge.innerHTML = `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;background:var(--danger-bg);color:var(--danger)">${data.anomalies.length} anomalies (${methods})</span>`;
            const header = chartCard.querySelector('.chart-header');
            if (header) header.appendChild(badge);
        }
        // Show detection config info
        const existingInfo = chartCard.querySelector('.detection-info');
        if (existingInfo) existingInfo.remove();
        if (data?.detection) {
            const info = document.createElement('div');
            info.className = 'detection-info';
            info.style.cssText = 'font-size:10px;color:var(--text-muted);margin-top:-8px;margin-bottom:8px;';
            info.textContent = `detection: ${data.detection.methods?.join(', ') || '-'} | z=${data.detection.z_threshold} | ewma_span=${data.detection.ewma_span}`;
            canvas.parentElement.insertBefore(info, canvas);
        }
    }

    if (!data || !data.series || data.series.length === 0) {
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#a0aec0';
        ctx.font = '13px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('暂无数据', canvas.width / 2, canvas.height / 2);
        return;
    }

    // Use category labels (HH:mm) from the first series timestamps
    const refTimestamps = data.series[0].timestamps;
    const labels = refTimestamps.map(t => _fmtTs(t));

    // Thin out labels to avoid crowding (show ~12 labels max)
    const step = Math.max(1, Math.floor(labels.length / 12));
    const displayLabels = labels.map((l, i) => i % step === 0 ? l : '');

    const datasets = data.series.map((s, i) => ({
        label: s.label,
        data: s.values,
        borderColor: CHART_COLORS[i % CHART_COLORS.length],
        backgroundColor: CHART_COLORS[i % CHART_COLORS.length] + '18',
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        fill: true,
        tension: 0.3,
    }));

    // Build anomaly annotation points (using data index)
    const anomalyAnnotations = {};
    if (data.anomalies && data.anomalies.length > 0) {
        data.anomalies.forEach((a, i) => {
            const idx = a.index != null ? a.index : -1;
            if (idx >= 0 && idx < refTimestamps.length) {
                const color = a.severity === 'critical' ? '#ef4444' : '#f59e0b';
                anomalyAnnotations[`anomaly_${i}`] = {
                    type: 'point',
                    xValue: idx,
                    yValue: a.value,
                    radius: 6,
                    backgroundColor: color,
                    borderColor: '#ffffff',
                    borderWidth: 2,
                    label: {
                        content: `${a.severity} z=${a.zscore}`,
                        display: true,
                        position: 'end',
                        font: { size: 9, weight: 'bold' },
                        color: color,
                        backgroundColor: 'rgba(255,255,255,0.85)',
                        padding: 3,
                    },
                };
            }
        });
    }

    // Threshold lines
    if (data.thresholds) {
        if (data.thresholds.warn != null) {
            anomalyAnnotations['warn_line'] = {
                type: 'line',
                yMin: data.thresholds.warn, yMax: data.thresholds.warn,
                borderColor: '#f59e0b88', borderWidth: 1.5, borderDash: [6, 4],
                label: { content: `Warn ${data.thresholds.warn}`, display: true, position: 'start', font: { size: 10 }, color: '#d97706' },
            };
        }
        if (data.thresholds.crit != null) {
            anomalyAnnotations['crit_line'] = {
                type: 'line',
                yMin: data.thresholds.crit, yMax: data.thresholds.crit,
                borderColor: '#ef444488', borderWidth: 1.5, borderDash: [6, 4],
                label: { content: `Crit ${data.thresholds.crit}`, display: true, position: 'start', font: { size: 10 }, color: '#ef4444' },
            };
        }
    }

    try {
        state.charts[canvasId] = new Chart(canvas, {
            type: 'line',
            data: { labels: displayLabels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: {
                        grid: { color: 'rgba(226,232,240,0.3)' },
                        ticks: { color: '#a0aec0', font: { size: 10 }, maxRotation: 0 },
                    },
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(226,232,240,0.4)' },
                        ticks: { color: '#a0aec0', font: { size: 10 }, callback: (v) => v + unit },
                    },
                },
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { padding: 12, font: { size: 11 }, usePointStyle: true, pointStyle: 'circle' },
                    },
                    tooltip: {
                        backgroundColor: '#1a2b42', titleFont: { size: 12 }, bodyFont: { size: 11 },
                        padding: 10, cornerRadius: 6,
                        callbacks: {
                            title: (items) => items[0] ? labels[items[0].dataIndex] || '' : '',
                            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(2)}${unit}`,
                        },
                    },
                    annotation: { annotations: anomalyAnnotations },
                },
                animation: { duration: 600, easing: 'easeOutQuart' },
            }
        });
    } catch(e) {
        console.error(`Chart render error [${canvasId}]:`, e);
    }
}

function renderBusinessChart(canvasId, data, unit) {
    if (!data) {
        renderTimeSeriesChart(canvasId, null, unit);
        return;
    }

    const series = [];
    const results = data.results || [];
    for (const r of results) {
        const metric = r.metric || {};
        const label = metric.service || metric.destination_service_name || Object.values(metric)[0] || 'unknown';
        const values_raw = r.values || [];
        series.push({
            label,
            timestamps: values_raw.map(v => v[0]),
            values: values_raw.map(v => {
                const f = parseFloat(v[1]);
                return isNaN(f) || !isFinite(f) ? 0 : f;
            }),
        });
    }

    renderTimeSeriesChart(canvasId, { series, anomalies: [], thresholds: {} }, unit);
}

function renderMetricBars(containerId, data, unit) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const results = data?.results || [];
    if (!results.length) {
        el.innerHTML = '<p class="text-muted" style="padding:8px">暂无数据</p>';
        return;
    }

    el.innerHTML = results.map(r => {
        const label = r.metric?.instance || r.metric?.node || Object.values(r.metric || {})[0] || 'unknown';
        const shortLabel = label.replace(/:.*$/, '');
        const val = parseFloat(r.value?.[1] || 0).toFixed(1);
        const pct = Math.min(parseFloat(val), 100);
        const color = pct > 90 ? 'var(--danger)' : pct > 75 ? 'var(--warning)' : 'var(--accent)';
        return `
            <div class="metric-bar-item">
                <div class="metric-bar-label">
                    <span>${escapeHtml(shortLabel)}</span>
                    <strong>${val}${unit}</strong>
                </div>
                <div class="metric-bar-track">
                    <div class="metric-bar-fill" style="width:${pct}%;background:${color}"></div>
                </div>
            </div>
        `;
    }).join('');
}

function renderContainerTop(tableId, data, unit) {
    const tbody = document.querySelector(`#${tableId} tbody`);
    if (!tbody) return;

    const results = data?.results || [];
    if (!results.length) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-muted" style="text-align:center">暂无数据</td></tr>';
        return;
    }

    tbody.innerHTML = results.map(r => {
        const pod = r.metric?.pod || 'unknown';
        const ns = r.metric?.namespace || '-';
        const val = parseFloat(r.value?.[1] || 0).toFixed(2);
        return `<tr><td>${escapeHtml(pod)}</td><td>${escapeHtml(ns)}</td><td>${val} ${unit}</td></tr>`;
    }).join('');
}

async function runPromQL() {
    const query = document.getElementById('promql-input')?.value?.trim();
    const queryType = document.getElementById('promql-type')?.value || 'instant';
    const resultEl = document.getElementById('promql-result');

    if (!query) { resultEl.textContent = '请输入 PromQL 查询'; return; }

    resultEl.textContent = '查询中...';
    const data = await api(`/api/prometheus/query?query=${encodeURIComponent(query)}&query_type=${queryType}`);

    if (!data || data.error) {
        resultEl.textContent = `错误: ${data?.error || '请求失败'}`;
        return;
    }

    resultEl.textContent = JSON.stringify(data.results || data, null, 2);
}

// ─────────────────────────────────────────
// Logs
// ─────────────────────────────────────────

async function loadNamespaces(selectId) {
    const data = await api('/api/cluster/namespaces');
    if (!data?.namespaces) return;

    const sel = document.getElementById(selectId);
    const current = sel.value;
    const firstOpt = sel.querySelector('option')?.textContent || '选择命名空间';
    sel.innerHTML = `<option value="">${firstOpt}</option>` +
        data.namespaces.map(ns => `<option value="${ns}" ${ns === current ? 'selected' : ''}>${ns}</option>`).join('');
}

async function loadPodsByNs() {
    const ns = document.getElementById('log-ns').value;
    if (!ns) return;

    const data = await api(`/api/cluster/pods?namespace=${ns}`);
    if (!data?.pods) return;

    const sel = document.getElementById('log-pod');
    sel.innerHTML = '<option value="">选择Pod</option>' +
        data.pods.map(p => `<option value="${p.name}">${p.name} (${p.phase})</option>`).join('');
}

async function fetchLogs() {
    const ns = document.getElementById('log-ns').value;
    const pod = document.getElementById('log-pod').value;
    const lines = document.getElementById('log-lines').value || 200;

    if (!ns || !pod) { alert('请选择命名空间和Pod'); return; }

    const viewer = document.getElementById('log-content');
    viewer.textContent = '加载中...';
    const data = await api(`/api/logs/${ns}/${pod}?lines=${lines}`);
    viewer.textContent = data?.logs || '无日志内容';
}

// ─────────────────────────────────────────
// Alerts
// ─────────────────────────────────────────

let _alertData = [];
let _filteredAlerts = [];
let _activeAlertHealIndex = null;
let _lastAlertHealResult = null;
let _activeAlertHealHtml = '';
let _currentHealPlan = null;
let _currentHealPlanSourcePayload = null;
let _lastHealResult = null;
let _lastHealRuns = [];
let _activeRCAAlertKey = null;
const _alertDiagnosisCache = new Map();

const SOURCE_CONFIG = {
    k8s_event:   { label: 'K8s事件',    icon: '&#9783;', cls: 'source-k8s' },
    prometheus:  { label: 'Prometheus', icon: '&#9636;', cls: 'source-prom' },
    pod_health:  { label: 'Pod健康',    icon: '&#9673;', cls: 'source-pod' },
    node_health: { label: '节点健康',   icon: '&#9633;', cls: 'source-node' },
    metric_anomaly: { label: '指标异常', icon: '&#9650;', cls: 'source-metric' },
};

async function loadAlertList() {
    const data = await api('/api/alerts/list');
    if (!data) return;
    _alertData = data.alerts || [];
    renderAlertSourceStats(_alertData);
    filterAlerts();
}

function renderAlertSourceStats(alerts) {
    const stats = {};
    alerts.forEach(a => { stats[a.source] = (stats[a.source] || 0) + 1; });

    const container = document.getElementById('alert-source-stats');
    const sourceInfo = {
        k8s_event:   { label: 'K8s事件',    color: 'var(--info)' },
        prometheus:  { label: 'Prometheus', color: 'var(--accent)' },
        pod_health:  { label: 'Pod健康',    color: 'var(--danger)' },
        node_health: { label: '节点健康',   color: 'var(--warning)' },
        metric_anomaly: { label: '指标异常', color: 'var(--success)' },
    };

    container.innerHTML = Object.entries(stats).map(([src, count]) => {
        const info = sourceInfo[src] || { label: src, color: 'var(--text-muted)' };
        return `<div class="stat-card" style="border-left:3px solid ${info.color}">
            <div class="stat-label">${info.label}</div>
            <div class="stat-value">${count}</div>
        </div>`;
    }).join('') + `<div class="stat-card accent">
        <div class="stat-label">总告警</div>
        <div class="stat-value">${alerts.length}</div>
    </div>`;
}

function filterAlerts(source) {
    if (source) {
        document.querySelectorAll('.alert-source-btn').forEach(b => b.classList.remove('active'));
        document.querySelector(`.alert-source-btn[data-source="${source}"]`)?.classList.add('active');
    }

    const activeSource = document.querySelector('.alert-source-btn.active')?.dataset.source || 'all';
    const severityFilter = document.getElementById('alert-severity-filter').value;

    let filtered = _alertData;
    if (activeSource !== 'all') filtered = filtered.filter(a => a.source === activeSource);
    if (severityFilter !== 'all') filtered = filtered.filter(a => a.severity === severityFilter);

    renderAlertTable(filtered);
}

function renderAlertTable(alerts) {
    _filteredAlerts = alerts;
    const tbody = document.getElementById('alert-table-body');
    const empty = document.getElementById('alert-empty');

    if (!alerts.length) { tbody.innerHTML = ''; empty.style.display = 'block'; return; }
    empty.style.display = 'none';

    tbody.innerHTML = alerts.map((a, i) => {
        const src = SOURCE_CONFIG[a.source] || { label: a.source, icon: '?', cls: '' };
        const sevClass = a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info';
        const isActive = _activeAlertHealIndex === i;
        const healContent = isActive ? (_activeAlertHealHtml || renderAlertHealLoading()) : '';
        return `<tr class="${isActive ? 'alert-row-active' : ''}">
            <td><span class="source-badge ${src.cls}">${src.icon} ${src.label}</span></td>
            <td><span class="badge badge-${sevClass}">${a.severity}</span></td>
            <td title="${escapeHtml(a.description || '')}">${escapeHtml(a.title || (a.description || '').substring(0, 80) || '')}</td>
            <td>${escapeHtml(a.service || '')}</td>
            <td>${escapeHtml(a.namespace || '')}</td>
            <td>${formatTime(a.timestamp ? a.timestamp * 1000 : null)}</td>
            <td>
                <div class="table-actions">
                    <button class="btn btn-sm btn-primary" onclick="startRCAFromAlert(${i})">分析</button>
                    <button class="btn btn-sm btn-secondary" id="alert-heal-btn-${i}" onclick="showAlertHealCapability(${i})">自愈</button>
                </div>
            </td>
        </tr>${isActive ? `<tr class="alert-heal-row"><td colspan="7"><div id="alert-heal-panel-${i}">${healContent}</div></td></tr>` : ''}`;
    }).join('');
}

function buildHealPayloadFromAlert(a) {
    const description = a?.description || a?.message || '';
    const raw = a?.raw_data || {};
    const involved = raw?.involvedObject || {};
    return {
        source: 'alert',
        alert: {
            source: a?.source || '',
            severity: a?.severity || '',
            title: a?.title || '',
            description,
            namespace: a?.namespace || '',
            service: a?.service || '',
            labels: a?.labels || {},
            raw_data: {
                reason: raw?.reason || '',
                message: raw?.message || description,
                involvedObject: {
                    kind: involved?.kind || '',
                    name: involved?.name || '',
                    namespace: involved?.namespace || a?.namespace || '',
                },
                metadata: {
                    namespace: raw?.metadata?.namespace || a?.namespace || '',
                },
            },
        },
        namespace: a?.namespace || 'default',
        message: `[${(a?.severity || 'warning').toUpperCase()}] ${a?.title || ''} ${description}`,
        object: a?.service || '',
        pod: a?.source === 'pod_health' || /pod/i.test(a?.title || '') ? a?.service || '' : '',
        node: a?.source === 'node_health' ? a?.service || '' : '',
        evidence: [a?.source || '', a?.severity || '', description],
    };
}

function alertDiagnosisKey(a) {
    if (!a) return '';
    const raw = a.raw_data || {};
    const involved = raw.involvedObject || {};
    return [
        a.source || '',
        a.namespace || '',
        a.service || '',
        a.title || '',
        involved.kind || '',
        involved.name || '',
        raw.reason || '',
        String(a.timestamp || ''),
    ].join('|');
}

function enrichHealPayloadWithDiagnosis(payload, diagnosis, key = '') {
    if (!diagnosis) return { ...payload, diagnosis_mode: 'alert_only' };
    const services = Array.isArray(diagnosis.affected_services) ? diagnosis.affected_services : [];
    const evidence = Object.values(diagnosis.evidence_summary || {}).map(v => String(v || '')).filter(Boolean);
    return {
        ...payload,
        source: 'alert-rca',
        diagnosis_mode: 'rca_result',
        diagnosis_key: key,
        fault_type: diagnosis.fault_type || payload.fault_type || '',
        root_cause: diagnosis.root_cause || payload.root_cause || '',
        root_cause_component: diagnosis.root_cause_component || payload.root_cause_component || services[0] || payload.object || '',
        remediation_hint: diagnosis.remediation_suggestion || payload.remediation_hint || '',
        diagnosis,
        evidence: [...(payload.evidence || []), ...evidence],
        object: payload.object || services[0] || '',
        deployment: payload.deployment || (/deployment|deploy/i.test(services[0] || '') ? services[0] : ''),
        message: [
            payload.message || '',
            diagnosis.fault_type || '',
            diagnosis.root_cause || '',
            diagnosis.remediation_suggestion || '',
            evidence.join(' '),
        ].join(' '),
    };
}

function buildHealPayloadFromAlertWithDiagnosis(a) {
    const base = buildHealPayloadFromAlert(a);
    const key = alertDiagnosisKey(a);
    const diagnosis = key ? _alertDiagnosisCache.get(key) : null;
    return enrichHealPayloadWithDiagnosis(base, diagnosis, key);
}

async function showAlertHealCapability(index) {
    const a = _filteredAlerts[index];
    if (!a) return;
    _activeAlertHealIndex = index;
    _lastAlertHealResult = null;
    _activeAlertHealHtml = renderAlertHealLoading('知识库检索中...');
    renderAlertTable(_filteredAlerts);
    const btn = document.getElementById(`alert-heal-btn-${index}`);
    if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
    const panel = document.getElementById(`alert-heal-panel-${index}`);
    if (panel) panel.innerHTML = _activeAlertHealHtml;
    const result = await api('/api/heal/capability', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildHealPayloadFromAlertWithDiagnosis(a)),
        timeoutMs: 8000,
    });
    if (!result) {
        _activeAlertHealHtml = renderAlertHealError('自愈能力生成超时或失败。已避免无限等待，请稍后重试，或先做 RCA 补齐上下文。');
        if (panel) panel.innerHTML = _activeAlertHealHtml;
        if (btn) { btn.disabled = false; btn.textContent = '自愈'; }
        return;
    }
    _lastAlertHealResult = result;
    renderAlertHealPanel(result, index);
    if (btn) { btn.disabled = false; btn.textContent = '自愈'; }
}

function renderAlertHealLoading(text = '生成自愈能力...') {
    return `<div class="alert-heal-panel">
        <div class="heal-process">
            ${renderHealProcessSteps('detect')}
        </div>
        <div class="loading">${escapeHtml(text)}</div>
    </div>`;
}

function renderAlertHealError(message) {
    return `<div class="alert-heal-panel">
        ${renderHealProcessSteps('failed')}
        <div class="text-danger" style="padding:10px 0">${escapeHtml(message)}</div>
    </div>`;
}

function renderHealProcessSteps(active) {
    const steps = [
        ['detect', '告警识别', '提取类型和对象'],
        ['kb', '知识库检索', '匹配自愈策略'],
        ['plan', '方案生成', '命令和回滚动作'],
        ['approve', '审批确认', '风险门禁'],
        ['execute', '执行/验证', 'Dry-run 或真实执行'],
    ];
    const order = steps.map(s => s[0]);
    const activeIdx = order.indexOf(active);
    return `<div class="heal-process-steps">${steps.map((s, i) => {
        let cls = 'pending';
        if (active === 'failed') cls = i <= 1 ? 'failed' : 'pending';
        else if (i < activeIdx) cls = 'done';
        else if (i === activeIdx) cls = 'active';
        return `<div class="heal-step ${cls}">
            <span>${i + 1}</span>
            <strong>${escapeHtml(s[1])}</strong>
            <small>${escapeHtml(s[2])}</small>
        </div>`;
    }).join('')}</div>`;
}

function renderAlertHealPanel(result, index) {
    const container = document.getElementById(`alert-heal-panel-${index}`) || document.getElementById('alert-scan-result');
    _activeAlertHealHtml = renderAlertHealPanelHtml(result, index);
    container.innerHTML = _activeAlertHealHtml;
    container.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderAlertHealPanelHtml(result, index) {
    const summary = result.summary || {};
    const target = result.target || {};
    const capability = result.capability || {};
    const suggestions = result.suggestions || [];
    const blocked = result.blocked_templates || [];
    const available = suggestions.length > 0;
    const a = _filteredAlerts[index];
    const diagnosis = a ? _alertDiagnosisCache.get(alertDiagnosisKey(a)) : null;
    const diagnosisMode = diagnosis ? '已使用分析诊断结果' : '未关联诊断结果';
    return `
        <div class="alert-heal-panel">
            ${renderHealProcessSteps(available ? 'approve' : 'plan')}
            <div class="alert-heal-header">
                <div>
                    <h4>告警自愈能力</h4>
                    <p class="text-muted">知识库${result.recipe ? '命中' : '未命中'} · ${diagnosisMode} · 故障类型 ${escapeHtml(summary.fault_type || result.fault_type || 'unknown')} · namespace ${escapeHtml(summary.namespace || result.namespace || 'default')}</p>
                </div>
                <div class="table-actions">
                    ${available ? `<button class="btn btn-sm btn-primary" onclick="openHealPlanFromAlert(${index})">查看/编辑方案</button>` : ''}
                    ${available ? `<button class="btn btn-sm" onclick="dryRunHealFromAlert(${index})">快速 Dry-run</button>` : ''}
                    <button class="btn btn-sm" onclick="startRCAFromAlert(${index})">先做 RCA</button>
                    <button class="btn btn-sm" onclick="switchView('healcenter')">自愈中心</button>
                </div>
            </div>
            <div class="alert-heal-meta">
                <span class="badge badge-${available ? 'success' : 'warning'}">${available ? '可生成自愈方案' : '暂无可执行方案'}</span>
                <span class="badge badge-${diagnosis ? 'success' : 'warning'}">${diagnosis ? '诊断驱动' : '告警文本降级'}</span>
                <span class="badge badge-info">目标 pod=${escapeHtml(target.pod || '-')}</span>
                <span class="badge badge-info">deployment=${escapeHtml(target.deployment || '-')}</span>
                <span class="badge badge-info">node=${escapeHtml(target.node || '-')}</span>
                ${capability.supports_rollback ? '<span class="badge badge-success">支持回滚</span>' : ''}
                ${capability.requires_approval ? '<span class="badge badge-warning">需要审批</span>' : ''}
            </div>
            ${diagnosis ? `<div class="rca-remediation"><strong>诊断依据：</strong>${escapeHtml(diagnosis.root_cause || diagnosis.fault_type || '')}</div>` : '<div class="rca-remediation"><strong>诊断依据：</strong>当前未找到该告警的分析结果，方案基于告警文本和对象生成。建议先点击“分析”。</div>'}
            ${result.recipe?.description ? `<div class="rca-remediation">${escapeHtml(result.recipe.description)}</div>` : ''}
            <div class="heal-stage-groups">
                <div class="heal-stage-card">
                    <strong>1. 只读确认</strong>
                    <span>优先执行 describe/top/logs 等观测动作，降低误修复概率。</span>
                </div>
                <div class="heal-stage-card">
                    <strong>2. 恢复动作</strong>
                    <span>对 rollout restart/undo/cordon 等动作应用审批和高风险门禁。</span>
                </div>
                <div class="heal-stage-card">
                    <strong>3. 验证回滚</strong>
                    <span>记录回滚命令，运行结果可在自愈中心查看。</span>
                </div>
            </div>
            <div class="rem-actions-list">
                ${suggestions.map((s, i) => renderHealSuggestionItem(s, i)).join('')}
                ${blocked.map((s, i) => renderHealBlockedItem(s, i)).join('')}
            </div>
            <pre class="log-viewer" id="alert-heal-output-${index}" style="min-height:90px">${available ? '等待用户确认：点击 Dry-run 预演查看将执行的自愈动作。' : '当前告警缺少可落地目标对象或知识库策略。建议先执行根因分析补齐上下文。'}</pre>
        </div>`;
}

function renderHealSuggestionItem(s, i) {
    const risk = (s.risk || 'low').toLowerCase();
    const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
    return `<div class="rem-action-item">
        <div class="rem-action-header"><span class="rem-action-num">${i + 1}</span><span class="badge badge-${riskClass}">${escapeHtml(s.risk || 'low')}</span><span class="badge badge-info">${escapeHtml(s.source || 'dynamic')}</span><span class="rem-action-desc">${escapeHtml(s.step || '')}</span></div>
        <div class="rem-action-cmd"><code>${escapeHtml(s.command || '')}</code></div>
        ${s.selection_reason ? `<div class="rem-action-rollback">score=${escapeHtml(s.selection_score ?? '-')} · ${escapeHtml(s.selection_reason)}</div>` : ''}
        ${s.rollback_command ? `<div class="rem-action-rollback">rollback: ${escapeHtml(s.rollback_command)}</div>` : ''}
    </div>`;
}

function renderHealBlockedItem(s, i) {
    return `<div class="rem-action-item blocked">
        <div class="rem-action-header"><span class="rem-action-num">!</span><span class="badge badge-warning">blocked</span><span class="rem-action-desc">${escapeHtml(s.step || `模板 ${i + 1}`)}</span></div>
        <div class="rem-action-cmd"><code>${escapeHtml(s.command || '')}</code></div>
        <div class="rem-action-rollback">${escapeHtml(s.blocked_reason || 'blocked')}</div>
    </div>`;
}

async function dryRunHealFromAlert(index) {
    const a = _filteredAlerts[index];
    if (!a) return;
    const out = document.getElementById(`alert-heal-output-${index}`) || document.getElementById('alert-heal-output');
    if (out) out.textContent = 'Dry-run 执行中...';
    const panel = document.getElementById(`alert-heal-panel-${index}`);
    if (panel && _lastAlertHealResult) {
        const cloned = panel.querySelector('.heal-process-steps');
        if (cloned) cloned.outerHTML = renderHealProcessSteps('execute');
    }
    const result = await api('/api/heal/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...buildHealPayloadFromAlertWithDiagnosis(a), dry_run: true, source: 'alert-center' }),
    });
    if (!result) {
        if (out) out.textContent = 'Dry-run 失败，请检查后端日志。';
        showToast('告警自愈 dry-run 失败', 'error');
        return;
    }
    if (out) out.textContent = JSON.stringify(result, null, 2);
    if (_activeAlertHealIndex === index) {
        const panel = document.getElementById(`alert-heal-panel-${index}`);
        if (panel) _activeAlertHealHtml = panel.innerHTML;
    }
    showToast('告警自愈 dry-run 已完成');
}

function openHealPlanFromAlert(index) {
    const a = _filteredAlerts[index];
    if (!a || !_lastAlertHealResult) return;
    openHealPlanModal(_lastAlertHealResult, buildHealPayloadFromAlertWithDiagnosis(a), { source: 'alert-center' });
}

async function runAlertScan() {
    const container = document.getElementById('alert-scan-result');
    container.innerHTML = '<div class="loading">扫描中</div>';

    const data = await api('/api/alerts/scan');
    if (!data) { container.innerHTML = '<p class="text-danger">扫描失败</p>'; return; }

    let html = `<div style="margin-bottom:12px">
        <span class="badge badge-info">总告警: ${data.total_alerts || 0}</span>
        <span class="badge badge-success">分组数: ${data.compressed_groups || data.num_groups || 0}</span>
        <span class="badge badge-warning">压缩率: ${((data.compression_ratio || 0) * 100).toFixed(0)}%</span>
    </div>`;

    (data.groups || []).forEach((g, gi) => {
        const severity = (g.severity || '').toLowerCase();
        const rawAlerts = data.raw_alerts || [];
        const groupAlerts = (g.alert_indices || []).map(i => rawAlerts[i]).filter(Boolean);

        html += `
            <div class="alert-group ${severity === 'critical' ? 'critical' : ''}">
                <div class="group-title">${escapeHtml(g.group_label || g.representative || g.common_pattern || '告警组 ' + (gi+1))}</div>
                <div class="group-meta">${g.alert_count || 0} 条告警 | ${escapeHtml(g.severity || 'unknown')}</div>
                ${g.root_cause || g.root_cause_recommendation ? `<div class="group-rca">${escapeHtml(g.root_cause || g.root_cause_recommendation)}</div>` : ''}
                ${groupAlerts.length ? `<details style="margin-top:8px;font-size:12px">
                    <summary style="cursor:pointer;color:var(--text-muted)">查看组内告警详情</summary>
                    <div style="margin-top:6px">
                    ${groupAlerts.map(a => {
                        const src = SOURCE_CONFIG[a.source] || { label: a.source, icon: '?', cls: '' };
                        return `<div class="signal-item"><span><span class="source-badge ${src.cls}">${src.icon} ${src.label}</span> ${escapeHtml(a.name || '')} — ${escapeHtml((a.message || '').substring(0, 120))}</span></div>`;
                    }).join('')}
                    </div>
                </details>` : ''}
            </div>
        `;
    });

    container.innerHTML = html;
}

function toggleDetectionSSE() {
    if (state.detectionSSE) { state.detectionSSE.close(); state.detectionSSE = null; return; }

    const feed = document.getElementById('detection-feed');
    state.detectionSSE = new EventSource('/api/detection/stream');
    state.detectionSSE.onmessage = (e) => {
        try {
            const signal = JSON.parse(e.data);
            const item = document.createElement('div');
            item.className = 'signal-item';
            item.innerHTML = `
                <span><span class="badge badge-${signal.severity === 'critical' ? 'danger' : 'warning'}">${signal.severity || 'info'}</span> ${escapeHtml(signal.description || signal.msg || JSON.stringify(signal).substring(0, 100))}</span>
                <span class="text-muted">${formatTime(signal.timestamp)}</span>
            `;
            feed.prepend(item);
        } catch {}
    };
}

async function clearSignals() {
    await api('/api/detection/signals', { method: 'DELETE' });
    document.getElementById('detection-feed').innerHTML = '';
}

// ─────────────────────────────────────────
// Detection Config Management
// ─────────────────────────────────────────

let _detectionConfig = null;

async function loadDetectionConfig() {
    const data = await api('/api/detection/config');
    if (!data) return;
    _detectionConfig = data;
    renderSourceToggles(data);
    renderCategoryToggles(data);
    renderServiceTags('business-services-tags', data.business_services || [], 'business_services');
    renderServiceTags('db-services-tags', data.db_services || [], 'db_services');
    renderMetricChecksTable(data.metric_checks || []);
    renderCriticalReasons('critical-event-reasons', data.critical_event_reasons || []);
    renderCriticalReasons('critical-pod-reasons', data.critical_pod_reasons || []);
    updateSourceFilterButtons(data.sources_enabled || {});
    const lbEl = document.getElementById('cfg-lookback-m');
    const ztEl = document.getElementById('cfg-z-threshold');
    const esEl = document.getElementById('cfg-ewma-span');
    if (lbEl) lbEl.value = data.default_lookback_m || 30;
    if (ztEl) ztEl.value = data.default_z_threshold || 3.0;
    if (esEl) esEl.value = data.default_ewma_span || 10;
}

function renderSourceToggles(config) {
    const container = document.getElementById('source-toggles');
    const sources = config.sources_enabled || {};
    const labels = { prometheus: 'Prometheus', k8s_event: 'K8s事件', pod_health: 'Pod健康', node_health: '节点健康', metric_anomaly: '指标异常' };
    container.innerHTML = Object.entries(sources).map(([key, enabled]) => `
        <label style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:13px;cursor:pointer">
            <input type="checkbox" data-source="${escapeHtml(key)}" ${enabled ? 'checked' : ''}
                   onchange="toggleSource('${escapeHtml(key)}', this.checked)">
            ${labels[key] || key}
        </label>
    `).join('');
}

function toggleSource(key, enabled) {
    if (_detectionConfig?.sources_enabled) _detectionConfig.sources_enabled[key] = enabled;
}

function renderCategoryToggles(config) {
    const container = document.getElementById('category-toggles');
    if (!container) return;
    const cats = config.categories_enabled || {};
    const labels = { infrastructure: '基础设施', application: '应用', business: '业务工作负载', database: '数据库', k8s_workload: 'K8s工作负载健康' };
    container.innerHTML = Object.entries(cats).map(([key, enabled]) => `
        <label style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:13px;cursor:pointer">
            <input type="checkbox" data-category="${escapeHtml(key)}" ${enabled ? 'checked' : ''}
                   onchange="toggleCategory('${escapeHtml(key)}', this.checked)">
            ${labels[key] || key}
        </label>
    `).join('');
}

function toggleCategory(key, enabled) {
    if (_detectionConfig?.categories_enabled) _detectionConfig.categories_enabled[key] = enabled;
}

function renderServiceTags(containerId, services, configKey) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = services.map((s, i) => `
        <span class="badge badge-info" style="margin:2px;font-size:12px">
            ${escapeHtml(s)}
            <span style="cursor:pointer;margin-left:4px" onclick="removeServiceTag('${containerId}', '${configKey}', ${i})">&times;</span>
        </span>
    `).join('') + `<button class="btn btn-sm" onclick="addServiceTag('${containerId}', '${configKey}')" style="font-size:11px">+ 添加</button>`;
}

function addServiceTag(containerId, configKey) {
    const name = prompt('输入服务名称:');
    if (!name) return;
    if (_detectionConfig) {
        _detectionConfig[configKey] = _detectionConfig[configKey] || [];
        _detectionConfig[configKey].push(name.trim());
        renderServiceTags(containerId, _detectionConfig[configKey], configKey);
    }
}

function removeServiceTag(containerId, configKey, index) {
    if (_detectionConfig?.[configKey]) {
        _detectionConfig[configKey].splice(index, 1);
        renderServiceTags(containerId, _detectionConfig[configKey], configKey);
    }
}

function renderMetricChecksTable(checks) {
    const tbody = document.getElementById('metric-checks-body');
    if (!checks.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center">暂无指标配置</td></tr>';
        return;
    }
    const allMethods = ['threshold', 'zscore', 'ewma', 'spectral_residual', 'pearson_onset', 'rate_change'];
    const defaultMethods = (_detectionConfig?.default_detect_methods) || ['threshold', 'zscore'];
    tbody.innerHTML = checks.map((c, i) => {
        const methods = c.detect_methods || defaultMethods;
        const methodCheckboxes = allMethods.map(m => {
            const checked = methods.includes(m) ? 'checked' : '';
            return `<label style="display:inline-flex;align-items:center;gap:2px;font-size:11px;white-space:nowrap"><input type="checkbox" class="mc-method" data-idx="${i}" data-method="${m}" ${checked}> ${m}</label>`;
        }).join(' ');
        return `<tr>
            <td><input class="input-sm mc-name" value="${escapeHtml(c.name || '')}" style="width:120px" data-idx="${i}"></td>
            <td><select class="select-sm mc-level" data-idx="${i}"><option value="node" ${c.level==='node'?'selected':''}>node</option><option value="container" ${c.level==='container'?'selected':''}>container</option></select></td>
            <td><input type="number" class="input-sm mc-warn" value="${c.warn}" style="width:60px" data-idx="${i}"></td>
            <td><input type="number" class="input-sm mc-crit" value="${c.crit}" style="width:60px" data-idx="${i}"></td>
            <td>${escapeHtml(c.unit || '%')}</td>
            <td style="max-width:280px">${methodCheckboxes}</td>
            <td><button class="btn btn-sm" onclick="editMetricCheck(${i})" title="编辑PromQL">Edit</button> <button class="btn btn-sm btn-danger" onclick="removeMetricCheck(${i})">Del</button></td>
        </tr>`;
    }).join('');
}

function renderCriticalReasons(containerId, reasons) {
    const container = document.getElementById(containerId);
    container.innerHTML = reasons.map((r, i) => `
        <span class="badge badge-danger" style="margin:2px;font-size:12px">${escapeHtml(r)}
            <span style="cursor:pointer;margin-left:4px" onclick="removeCriticalReason('${containerId}', ${i})">&times;</span>
        </span>
    `).join('') + `<button class="btn btn-sm" onclick="addCriticalReason('${containerId}')" style="font-size:11px">+ 添加</button>`;
}

function addCriticalReason(containerId) {
    const reason = prompt('输入原因名称:');
    if (!reason) return;
    const key = containerId === 'critical-event-reasons' ? 'critical_event_reasons' : 'critical_pod_reasons';
    if (_detectionConfig) {
        _detectionConfig[key] = _detectionConfig[key] || [];
        _detectionConfig[key].push(reason.trim());
        renderCriticalReasons(containerId, _detectionConfig[key]);
    }
}

function removeCriticalReason(containerId, index) {
    const key = containerId === 'critical-event-reasons' ? 'critical_event_reasons' : 'critical_pod_reasons';
    if (_detectionConfig?.[key]) { _detectionConfig[key].splice(index, 1); renderCriticalReasons(containerId, _detectionConfig[key]); }
}

function addMetricCheck() {
    const defaultMethods = (_detectionConfig?.default_detect_methods) || ['threshold', 'zscore'];
    const newCheck = { name: 'new_metric', query: '', unit: '%', label_key: 'instance', ns_key: '', level: 'node', warn: 85, crit: 95, detect_methods: [...defaultMethods] };
    if (_detectionConfig) {
        _detectionConfig.metric_checks = _detectionConfig.metric_checks || [];
        _detectionConfig.metric_checks.push(newCheck);
        renderMetricChecksTable(_detectionConfig.metric_checks);
        editMetricCheck(_detectionConfig.metric_checks.length - 1);
    }
}

function editMetricCheck(index) {
    if (!_detectionConfig?.metric_checks) return;
    const check = _detectionConfig.metric_checks[index];
    if (!check) return;
    const query = prompt('PromQL 查询表达式:', check.query || '');
    if (query === null) return;
    check.query = query;
    const labelKey = prompt('标签键 (label_key):', check.label_key || 'instance');
    if (labelKey !== null) check.label_key = labelKey;
    const nsKey = prompt('命名空间键 (ns_key, 可留空):', check.ns_key || '');
    if (nsKey !== null) check.ns_key = nsKey;
    renderMetricChecksTable(_detectionConfig.metric_checks);
}

function removeMetricCheck(index) {
    if (!_detectionConfig?.metric_checks) return;
    _detectionConfig.metric_checks.splice(index, 1);
    renderMetricChecksTable(_detectionConfig.metric_checks);
}

function _collectMetricChecksFromUI() {
    if (!_detectionConfig?.metric_checks) return;
    const checks = _detectionConfig.metric_checks;
    document.querySelectorAll('.mc-name').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].name = el.value; });
    document.querySelectorAll('.mc-level').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].level = el.value; });
    document.querySelectorAll('.mc-warn').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].warn = parseFloat(el.value) || 0; });
    document.querySelectorAll('.mc-crit').forEach(el => { const idx = parseInt(el.dataset.idx); if (checks[idx]) checks[idx].crit = parseFloat(el.value) || 0; });
    const methodsByIdx = {};
    document.querySelectorAll('.mc-method').forEach(el => { const idx = parseInt(el.dataset.idx); if (!methodsByIdx[idx]) methodsByIdx[idx] = []; if (el.checked) methodsByIdx[idx].push(el.dataset.method); });
    for (const [idx, methods] of Object.entries(methodsByIdx)) { const i = parseInt(idx); if (checks[i]) checks[i].detect_methods = methods; }
}

async function saveDetectionConfig() {
    if (!_detectionConfig) return;
    _collectMetricChecksFromUI();
    const lbEl = document.getElementById('cfg-lookback-m');
    const ztEl = document.getElementById('cfg-z-threshold');
    const esEl = document.getElementById('cfg-ewma-span');
    const payload = {
        sources_enabled: _detectionConfig.sources_enabled,
        metric_checks: _detectionConfig.metric_checks,
        critical_event_reasons: _detectionConfig.critical_event_reasons,
        critical_pod_reasons: _detectionConfig.critical_pod_reasons,
        default_lookback_m: lbEl ? parseInt(lbEl.value) || 30 : 30,
        default_z_threshold: ztEl ? parseFloat(ztEl.value) || 3.0 : 3.0,
        default_ewma_span: esEl ? parseInt(esEl.value) || 10 : 10,
        categories_enabled: _detectionConfig.categories_enabled || {},
        business_services: _detectionConfig.business_services || [],
        db_services: _detectionConfig.db_services || [],
    };
    const statusEl = document.getElementById('detection-save-status');
    statusEl.textContent = '保存中...';
    statusEl.style.color = 'var(--accent)';
    const result = await api('/api/detection/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (result?.status === 'ok') {
        statusEl.innerHTML = '<span style="color:var(--success);font-weight:600">&#10003; 配置已保存并生效</span>';
        showToast('检测配置已保存，指标图表将使用新的异常检测参数');
        updateSourceFilterButtons(_detectionConfig.sources_enabled);
        loadAlertList();
    } else {
        statusEl.innerHTML = '<span style="color:var(--danger);font-weight:600">&#10007; 保存失败</span>';
        showToast('保存失败，请检查参数', 'error');
    }
    setTimeout(() => { statusEl.textContent = ''; }, 5000);
}

function showToast(msg, type) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position:fixed;top:70px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    const bg = type === 'error' ? 'var(--danger)' : 'var(--accent)';
    toast.style.cssText = `padding:12px 20px;border-radius:8px;background:${bg};color:#fff;font-size:13px;font-weight:500;box-shadow:0 4px 16px rgba(0,0,0,0.15);opacity:0;transform:translateX(20px);transition:all 0.3s ease;max-width:360px;`;
    toast.textContent = msg;
    container.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; toast.style.transform = 'translateX(0)'; });
    setTimeout(() => {
        toast.style.opacity = '0'; toast.style.transform = 'translateX(20px)';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

function updateSourceFilterButtons(sourcesEnabled) {
    const filterContainer = document.getElementById('alert-source-filters');
    if (!filterContainer) return;
    filterContainer.querySelectorAll('.alert-source-btn[data-source]').forEach(btn => {
        const src = btn.dataset.source;
        if (src === 'all') return;
        btn.style.display = (sourcesEnabled[src] !== false) ? '' : 'none';
    });
}

function startRCAFromAlert(index) {
    const a = _filteredAlerts[index];
    if (!a) return;
    const query = `[${(a.severity || 'warning').toUpperCase()}] ${a.title || ''} — ${a.description || ''} (service=${a.service || ''}, namespace=${a.namespace || ''})`;
    _activeRCAAlertKey = alertDiagnosisKey(a);
    switchView('rca');
    document.getElementById('rca-query').value = query;
    document.getElementById('rca-ns').value = a.namespace || '';
    startRCA();
}

// ─────────────────────────────────────────
// RCA
// ─────────────────────────────────────────

async function startRCA() {
    const query = document.getElementById('rca-query').value.trim();
    const ns = document.getElementById('rca-ns').value.trim();
    if (!query) { alert('请描述故障现象'); return; }

    const data = await api('/api/rca/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, namespace: ns }),
    });
    if (!data?.run_id) { alert('启动失败'); return; }

    state.rcaRunId = data.run_id;
    const progress = document.getElementById('rca-progress');
    progress.style.display = 'block';
    document.getElementById('rca-result-card').style.display = 'none';
    state._pendingCollaboration = null;
    state._pendingCritique = null;
    state._pendingClosure = null;

    const logEl = document.getElementById('rca-log');
    logEl.textContent = '';
    document.getElementById('rca-phases').innerHTML = '';
    document.getElementById('rca-hyp-list').innerHTML = '';
    document.getElementById('rca-hypotheses').style.display = 'none';
    document.getElementById('rca-evidence-grid').innerHTML = '';
    document.getElementById('rca-evidence').style.display = 'none';
    document.getElementById('rca-iteration').style.display = 'none';
    document.getElementById('rca-result-content').innerHTML = '';

    const sse = new EventSource(`/api/rca/${data.run_id}/stream`);
    sse.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            if (msg.type === 'log') { logEl.textContent += msg.msg + '\n'; logEl.scrollTop = logEl.scrollHeight; }
            else if (msg.type === 'event') { handleRCAEvent(msg.data); }
            else if (msg.type === 'done') { sse.close(); renderRCAFinalResult(msg.result); loadRCAHistory(); }
        } catch {}
    };
    sse.onerror = () => { sse.close(); };
}

function handleRCAEvent(evt) {
    switch (evt.event) {
        case 'phase_start': updatePhase(evt.phase, evt.name, 'active'); break;
        case 'phase_complete': updatePhase(evt.phase, evt.name, 'done'); break;
        case 'hypotheses': renderHypotheses(evt.items); break;
        case 'evidence': addEvidenceCard(evt.agent, evt.summary, evt.success); break;
        case 'iteration': updateIteration(evt.current, evt.total); break;
        case 'collaboration_policy': renderCollaborationPolicy(evt.data); break;
        case 'collaboration_critique': renderCollaborationCritique(evt.data); break;
        case 'diagnosis_closure': renderDiagnosisClosure(evt.data); break;
        case 'judge': renderJudge(evt.data); break;
        case 'remediation': renderRemediation(evt.data); break;
        case 'remediation_executed': renderRemediationResult(evt.data); break;
    }
}

const PHASE_NAMES = { 0:'告警压缩', 1:'上下文检索', 2:'假设生成', 3:'证据调查', 4:'交叉关联', 5:'图分析', 6:'报告生成', 7:'质量评估', 8:'自动学习', 9:'自愈修复', 10:'协作反思', 11:'诊断闭环' };

function updatePhase(num, name, status) {
    const container = document.getElementById('rca-phases');
    let badge = document.getElementById(`rca-phase-${num}`);
    if (!badge) { badge = document.createElement('span'); badge.id = `rca-phase-${num}`; badge.className = 'phase-badge'; badge.textContent = PHASE_NAMES[num] || name; container.appendChild(badge); }
    badge.className = `phase-badge ${status}`;
}

function updateIteration(current, total) {
    const el = document.getElementById('rca-iteration');
    el.style.display = 'block';
    el.innerHTML = `<span class="iteration-badge">迭代 ${current} / ${total}</span>`;
}

function renderHypotheses(items) {
    document.getElementById('rca-hypotheses').style.display = 'block';
    document.getElementById('rca-hyp-list').innerHTML = items.map((h, i) => {
        const pct = Math.round(h.confidence * 100);
        return `<div class="hyp-item"><span class="hyp-rank">#${i+1}</span><div class="hyp-bar-wrap"><div class="hyp-bar" style="width:${pct}%"></div></div><span class="hyp-conf">${pct}%</span><span class="hyp-desc" title="${escapeHtml(h.description)}">${escapeHtml(h.description)}</span></div>`;
    }).join('');
}

function addEvidenceCard(agent, summary, success) {
    document.getElementById('rca-evidence').style.display = 'block';
    const grid = document.getElementById('rca-evidence-grid');
    const agentLabels = { metric_agent: 'Metric Agent', log_agent: 'Log Agent', trace_agent: 'Trace Agent', event_agent: 'Event Agent' };
    const card = document.createElement('div');
    card.className = `evidence-card ${success ? 'success' : 'error'}`;
    card.innerHTML = `<div class="ev-agent">${success ? '&#10003;' : '&#9888;'} ${agentLabels[agent] || agent}</div><div class="ev-summary">${escapeHtml(summary || (success ? '分析完成' : '分析失败'))}</div>`;
    grid.appendChild(card);
}

function renderJudge(data) {
    if (!data) return;
    const level = (data.judge_level || '').toLowerCase();
    const cls = level === 'gold' ? 'gold' : level === 'silver' ? 'silver' : 'bronze';
    const label = level === 'gold' ? 'Gold' : level === 'silver' ? 'Silver' : 'Bronze';
    const judgeEl = document.createElement('div');
    judgeEl.id = 'rca-judge-info';
    judgeEl.style.marginTop = '12px';
    judgeEl.innerHTML = `<span class="judge-badge ${cls}">${label} — 评分 ${(data.combined_score || data.score || 0).toFixed(3)}</span>${data.needs_review ? '<span class="badge badge-warning" style="margin-left:8px">需要人工复核</span>' : ''}`;
    state._pendingJudge = judgeEl;
}

function renderCollaborationPolicy(data) {
    if (!data) return;
    const el = document.createElement('div');
    el.className = 'rca-collab-info';
    const focus = (data.evidence_focus || []).slice(0, 5).map(x => `<span>${escapeHtml(x)}</span>`).join('');
    el.innerHTML = `
        <div class="collab-title">协作策略：${escapeHtml(data.strategy || 'plan_and_execute')}</div>
        <div class="collab-rationale">${escapeHtml(data.rationale || '')}</div>
        ${focus ? `<div class="collab-focus">${focus}</div>` : ''}
    `;
    state._pendingCollaboration = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingCollaboration = null; }
}

function renderCollaborationCritique(data) {
    if (!data) return;
    const el = document.createElement('div');
    el.className = 'rca-collab-info critique';
    const score = Math.round((data.quality_score || 0) * 100);
    const weaknesses = (data.weaknesses || []).slice(0, 3).map(x => `<li>${escapeHtml(x)}</li>`).join('');
    el.innerHTML = `
        <div class="collab-title">反思质量门：${score}% ${data.needs_human_review ? '<span class="badge badge-warning">建议人工复核</span>' : ''}</div>
        ${weaknesses ? `<ul>${weaknesses}</ul>` : '<div class="collab-rationale">未发现明显协作质量问题</div>'}
    `;
    state._pendingCritique = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingCritique = null; }
}

function renderDiagnosisClosure(data) {
    if (!data) return;
    const plan = data.plan || {};
    const el = document.createElement('div');
    el.className = 'rca-collab-info closure';
    const modes = (plan.failure_modes || []).map(x => `<span>${escapeHtml(x)}</span>`).join('');
    const agents = (plan.target_agents || data.evidence_agents || []).map(x => `<span>${escapeHtml(x)}</span>`).join('');
    const score = data.judge_after?.combined_score;
    el.innerHTML = `
        <div class="collab-title">诊断闭环：${escapeHtml(data.status || (plan.should_iterate ? 'started' : 'skipped'))}${score != null ? ` · 复评 ${Number(score).toFixed(3)}` : ''}</div>
        <div class="collab-rationale">${escapeHtml(plan.reason || data.reason || '')}</div>
        ${modes ? `<div class="collab-focus">${modes}</div>` : ''}
        ${agents ? `<div class="collab-focus">${agents}</div>` : ''}
    `;
    state._pendingClosure = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingClosure = null; }
}

function renderRCAFinalResult(result) {
    const card = document.getElementById('rca-result-card');
    const content = document.getElementById('rca-result-content');
    card.style.display = 'block';

    if (!result) { content.innerHTML = '<p class="text-danger">未获取到结果</p>'; return; }

    const status = result.status || 'unknown';
    const inner = result.result || result;
    const rca = (inner.result && typeof inner.result === 'object' && !Array.isArray(inner.result)) ? inner.result : inner;
    const rootCause = rca.root_cause || rca.error || 'N/A';
    const conf = rca.confidence || 0;
    const confPct = Math.round(conf * 100);
    const confClass = conf >= 0.7 ? 'high' : conf >= 0.4 ? 'medium' : 'low';

    let html = `<div class="rca-result-structured">`;
    html += `<div class="result-banner ${status === 'completed' ? 'success' : 'failed'}"><h4>${status === 'completed' ? '&#10003; 根因分析完成' : '&#10007; 分析失败'}</h4></div>`;
    html += `<div class="rca-root-cause">${escapeHtml(rootCause)}</div>`;
    html += `<div class="rca-conf-row"><span style="font-size:12px;color:var(--text-muted)">置信度</span><div class="rca-conf-bar"><div class="rca-conf-fill ${confClass}" style="width:${confPct}%"></div></div><span class="rca-conf-label">${confPct}%</span></div>`;

    html += `<div class="rca-meta-grid">`;
    if (rca.fault_type) html += `<div class="rca-meta-item"><div class="meta-label">故障类型</div><div class="meta-value">${escapeHtml(rca.fault_type)}</div></div>`;
    if (rca.affected_services?.length) html += `<div class="rca-meta-item"><div class="meta-label">受影响服务</div><div class="meta-value">${rca.affected_services.map(s => escapeHtml(s)).join(', ')}</div></div>`;
    if (rca.evidence_summary) { for (const [key, val] of Object.entries(rca.evidence_summary)) { if (val) html += `<div class="rca-meta-item"><div class="meta-label">${escapeHtml(key)}</div><div class="meta-value">${escapeHtml(String(val).substring(0, 200))}</div></div>`; } }
    html += `</div>`;

    if (rca.timeline?.length) {
        html += `<div><h4 style="font-size:13px;color:var(--text-secondary);margin-bottom:8px">事件时间线</h4><div class="rca-timeline">`;
        rca.timeline.forEach(t => { html += `<div class="rca-timeline-item"><div class="tl-time">${escapeHtml(t.time || '')}</div><div class="tl-event">${escapeHtml(t.event || '')}</div></div>`; });
        html += `</div></div>`;
    }

    if (rca.remediation_suggestion) html += `<div class="rca-remediation"><strong>修复建议：</strong>${escapeHtml(rca.remediation_suggestion)}</div>`;
    if (rca.prevention) html += `<div class="rca-remediation" style="margin-top:8px"><strong>预防措施：</strong>${escapeHtml(rca.prevention)}</div>`;

    html += `<div class="rca-heal-toolbar">
        <button class="btn btn-sm btn-primary" onclick="suggestHealFromRCA()">生成自愈能力</button>
        <span class="text-muted">基于当前诊断结论生成可执行建议、风险门禁和 dry-run 预演。</span>
    </div>
    <div id="rca-heal-capability"></div>`;

    html += `</div>`;
    content.innerHTML = html;
    state._lastRCAResult = rca;
    if (_activeRCAAlertKey) {
        _alertDiagnosisCache.set(_activeRCAAlertKey, rca);
        showToast('诊断结果已关联到该告警，自愈将优先使用诊断结论');
    }

    if (state._pendingJudge) { content.appendChild(state._pendingJudge); state._pendingJudge = null; }
    if (state._pendingCollaboration) { content.appendChild(state._pendingCollaboration); state._pendingCollaboration = null; }
    if (state._pendingCritique) { content.appendChild(state._pendingCritique); state._pendingCritique = null; }
    if (state._pendingClosure) { content.appendChild(state._pendingClosure); state._pendingClosure = null; }
    if (state._pendingRemediation) { content.appendChild(state._pendingRemediation); state._pendingRemediation = null; }
}

function buildHealPayloadFromRCA(rca) {
    const services = rca?.affected_services || [];
    const primary = Array.isArray(services) && services.length ? services[0] : '';
    return {
        source: 'rca',
        namespace: document.getElementById('rca-ns')?.value || 'default',
        fault_type: rca?.fault_type || '',
        root_cause: rca?.root_cause || '',
        root_cause_component: rca?.root_cause_component || primary,
        message: `${rca?.fault_type || ''} ${rca?.root_cause || ''} ${rca?.remediation_suggestion || ''}`,
        object: primary,
        pod: /pod/i.test(primary) ? primary : '',
        deployment: /deployment|deploy/i.test(primary) ? primary : '',
        evidence: Object.values(rca?.evidence_summary || {}).map(v => String(v || '')),
        diagnosis: rca,
    };
}

async function suggestHealFromRCA() {
    const rca = state._lastRCAResult;
    if (!rca) return;
    const sourcePayload = buildHealPayloadFromRCA(rca);
    const panel = document.getElementById('rca-heal-capability');
    if (panel) panel.innerHTML = '<div class="loading">生成自愈能力</div>';
    const result = await api('/api/heal/capability', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sourcePayload),
    });
    if (!result || !panel) return;
    state._lastRCAHealResult = result;
    state._lastRCAHealPayload = sourcePayload;
    const suggestions = result.suggestions || [];
    const blocked = result.blocked_templates || [];
    const capability = result.capability || {};
    panel.innerHTML = `<div class="alert-heal-panel rca-heal-panel">
        <div class="alert-heal-header">
            <div>
                <h4>诊断自愈能力</h4>
                <p class="text-muted">${escapeHtml(result.fault_type || 'unknown')} · ${escapeHtml(result.namespace || 'default')}</p>
            </div>
            <div class="table-actions">
                ${suggestions.length ? '<button class="btn btn-sm btn-primary" onclick="openHealPlanFromRCA()">查看/编辑方案</button>' : ''}
                ${suggestions.length ? '<button class="btn btn-sm" onclick="dryRunHealFromRCA()">快速 Dry-run</button>' : ''}
                <button class="btn btn-sm" onclick="switchView('healcenter')">自愈中心</button>
            </div>
        </div>
        <div class="alert-heal-meta">
            <span class="badge badge-${suggestions.length ? 'success' : 'warning'}">${suggestions.length ? '可自愈' : '仅建议'}</span>
            ${capability.supports_rollback ? '<span class="badge badge-success">支持回滚</span>' : ''}
            ${capability.requires_approval ? '<span class="badge badge-warning">需要审批</span>' : ''}
        </div>
        ${result.recipe?.description ? `<div class="rca-remediation">${escapeHtml(result.recipe.description)}</div>` : ''}
        <div class="rem-actions-list">
            ${suggestions.map((s, i) => renderHealSuggestionItem(s, i)).join('')}
            ${blocked.map((s, i) => renderHealBlockedItem(s, i)).join('')}
        </div>
        <pre class="log-viewer" id="rca-heal-output" style="min-height:90px">${suggestions.length ? '点击 Dry-run 预演查看将执行的自愈动作。' : '诊断结果未匹配到可执行知识库策略。'}</pre>
    </div>`;
    if (suggestions.length) openHealPlanModal(result, sourcePayload, { source: 'rca-result' });
}

async function dryRunHealFromRCA() {
    const rca = state._lastRCAResult;
    if (!rca) return;
    const out = document.getElementById('rca-heal-output');
    if (out) out.textContent = 'Dry-run 执行中...';
    const result = await api('/api/heal/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...buildHealPayloadFromRCA(rca), dry_run: true, source: 'rca-result' }),
    });
    if (out) out.textContent = JSON.stringify(result, null, 2);
    showToast('诊断自愈 dry-run 已完成');
}

function openHealPlanFromRCA() {
    if (!state._lastRCAHealResult) return;
    openHealPlanModal(state._lastRCAHealResult, state._lastRCAHealPayload || buildHealPayloadFromRCA(state._lastRCAResult), { source: 'rca-result' });
}

function renderRemediation(data) {
    if (!data) return;
    const status = data.status || 'unknown';
    const el = document.createElement('div');
    el.id = 'rca-remediation-section';
    el.className = 'rca-remediation-section';

    if (status === 'pending_approval') {
        const plan = data.plan || {};
        const actions = plan.actions || [];
        let actionsHtml = actions.map((a, i) => {
            const risk = (a.risk_level || 'low').toLowerCase();
            const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
            return `<div class="rem-action-item"><div class="rem-action-header"><span class="rem-action-num">${i+1}</span><span class="badge badge-${riskClass}">${a.risk_level || 'low'}</span><span class="rem-action-desc">${escapeHtml(a.description || '')}</span></div><div class="rem-action-cmd"><code>${escapeHtml(a.command || '')}</code></div>${a.rollback_command ? `<div class="rem-action-rollback">${escapeHtml(a.rollback_command)}</div>` : ''}</div>`;
        }).join('');
        el.innerHTML = `<h4>自愈修复方案</h4><div class="rem-status-badge pending">等待审批</div>${plan.estimated_recovery_time ? `<div class="rem-meta">预计恢复时间: ${escapeHtml(plan.estimated_recovery_time)}</div>` : ''}<div class="rem-actions-list">${actionsHtml}</div><div class="rem-buttons"><button class="btn btn-primary" onclick="approveRemediation()">批准执行</button><button class="btn btn-secondary" onclick="dismissRemediation()">拒绝</button></div>`;
    } else if (status === 'disabled') { el.innerHTML = `<div class="rem-status-badge disabled">自愈已禁用</div>`; }
    else if (status === 'skipped') { el.innerHTML = `<div class="rem-status-badge skipped">置信度不足，跳过自愈</div>`; }
    else if (status === 'executed') { renderRemediationResult(data); return; }

    state._pendingRemediation = el;
    const content = document.getElementById('rca-result-content');
    if (content?.innerHTML) { content.appendChild(el); state._pendingRemediation = null; }
}

function renderRemediationResult(data) {
    const section = document.getElementById('rca-remediation-section');
    const target = section || document.getElementById('rca-result-content');
    if (!target) return;
    const actions = data.actions || [];
    let html = `<div class="rca-remediation-section"><h4>自愈执行结果</h4><div class="rem-status-badge executed">已执行</div>`;
    actions.forEach((a, i) => {
        const ok = a.status === 'executed';
        html += `<div class="rem-result-item ${ok ? 'success' : 'failed'}"><span>${ok ? '&#10003;' : '&#10007;'} ${escapeHtml(a.description || `Action ${i+1}`)}</span><span class="rem-result-detail">${escapeHtml((a.result || '').substring(0, 100))}</span></div>`;
    });
    if (data.rollback_available) html += `<div class="rem-buttons"><button class="btn btn-warning" onclick="rollbackRemediation()">回滚</button></div>`;
    html += `</div>`;
    if (section) section.outerHTML = html; else target.insertAdjacentHTML('beforeend', html);
}

async function approveRemediation() {
    if (!state.rcaRunId) return;
    const btn = event.target; btn.disabled = true; btn.textContent = '执行中...';
    try { const result = await api(`/api/rca/${state.rcaRunId}/remediation/approve`, { method: 'POST' }); if (result) renderRemediationResult(result); }
    catch (e) { alert('执行失败: ' + e.message); }
    btn.disabled = false;
}

async function rollbackRemediation() {
    if (!state.rcaRunId) return;
    if (!confirm('确认回滚所有修复操作？')) return;
    try { const result = await api(`/api/rca/${state.rcaRunId}/remediation/rollback`, { method: 'POST' }); if (result) alert(`回滚完成: ${(result.actions || []).length} 个操作已撤销`); }
    catch (e) { alert('回滚失败: ' + e.message); }
}

function dismissRemediation() { const section = document.getElementById('rca-remediation-section'); if (section) section.remove(); }

async function loadRCAHistory() {
    const data = await api('/api/rca/history');
    if (!data?.runs) return;
    const container = document.getElementById('rca-history');
    if (data.runs.length === 0) { container.innerHTML = '<p class="text-muted">暂无历史记录</p>'; return; }
    container.innerHTML = data.runs.map(r => `
        <div class="signal-item">
            <span><span class="badge badge-${r.status === 'completed' ? 'success' : r.status === 'running' ? 'warning' : 'danger'}">${r.status}</span> ${escapeHtml(r.query?.substring(0, 80) || '')}</span>
            <span class="text-muted">${formatTime(r.started_at ? r.started_at * 1000 : null)}</span>
        </div>
    `).join('');
}

// ─────────────────────────────────────────
// Daemon
// ─────────────────────────────────────────

async function loadDaemonStatus() {
    const data = await api('/api/daemon/status');
    if (!data) return;
    document.getElementById('daemon-status').innerHTML = data.running ? '<span class="text-success">运行中</span>' : '<span class="text-danger">已停止</span>';
    document.getElementById('daemon-uptime').textContent = data.uptime_s ? `${Math.floor(data.uptime_s / 60)}m ${Math.floor(data.uptime_s % 60)}s` : '-';
    document.getElementById('daemon-cycles').textContent = data.cycles ?? '-';
    document.getElementById('daemon-pipelines').textContent = data.active_pipelines ?? '-';
}

async function startDaemon() {
    await api('/api/daemon/start', { method: 'POST' });
    loadDaemonStatus();
    if (state.daemonLogSSE) state.daemonLogSSE.close();
    const logEl = document.getElementById('daemon-log');
    logEl.textContent = '';
    state.daemonLogSSE = new EventSource('/api/daemon/logs/stream');
    state.daemonLogSSE.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            if (msg.type === 'log') { logEl.textContent += msg.msg + '\n'; logEl.scrollTop = logEl.scrollHeight; }
            else if (msg.type === 'status') { document.getElementById('daemon-cycles').textContent = msg.data?.cycles ?? '-'; document.getElementById('daemon-pipelines').textContent = msg.data?.active_pipelines ?? '-'; }
        } catch {}
    };
}

async function stopDaemon() {
    await api('/api/daemon/stop', { method: 'POST' });
    if (state.daemonLogSSE) { state.daemonLogSSE.close(); state.daemonLogSSE = null; }
    setTimeout(loadDaemonStatus, 1000);
}

// ─────────────────────────────────────────
// Traces (Jaeger)
// ─────────────────────────────────────────

async function loadTracesView() {
    const data = await api('/api/jaeger/services');
    const sel = document.getElementById('trace-service');
    const source = document.getElementById('trace-source-badge');
    if (source) {
        source.textContent = data?.selected_url
            ? `Jaeger ${data.services?.length || 0} services · ${data.selected_url}`
            : 'Jaeger 未连接';
        source.className = `badge badge-${data?.error ? 'warning' : 'info'}`;
    }
    if (data?.services?.length) {
        const current = sel.value;
        sel.innerHTML = '<option value="">选择服务</option>' + data.services.filter(s => s).sort().map(s => `<option value="${s}" ${s === current ? 'selected' : ''}>${s}</option>`).join('');
    } else if (data?.error) {
        sel.innerHTML = `<option value="">Jaeger 连接失败</option>`;
        document.getElementById('trace-empty').style.display = 'block';
        document.getElementById('trace-empty').textContent = `Jaeger 连接失败: ${data.error}`;
    }
}

async function loadTraceOperations() {
    const service = document.getElementById('trace-service')?.value;
    const sel = document.getElementById('trace-operation');
    sel.innerHTML = '<option value="">所有操作</option>';
    if (!service) return;
    const data = await api(`/api/jaeger/operations?service=${encodeURIComponent(service)}`);
    if (data?.operations?.length) sel.innerHTML += data.operations.map(op => `<option value="${op}">${op}</option>`).join('');
}

async function searchTraces() {
    const service = document.getElementById('trace-service')?.value;
    if (!service) { alert('请先选择服务'); return; }
    const operation = document.getElementById('trace-operation')?.value || '';
    const minDuration = document.getElementById('trace-min-duration')?.value || '';
    const maxDuration = document.getElementById('trace-max-duration')?.value || '';
    const lookback = document.getElementById('trace-lookback')?.value || '1h';
    const limit = document.getElementById('trace-limit')?.value || 20;
    let url = `/api/jaeger/traces?service=${encodeURIComponent(service)}&lookback=${lookback}&limit=${limit}`;
    if (operation) url += `&operation=${encodeURIComponent(operation)}`;
    if (minDuration) url += `&min_duration=${encodeURIComponent(minDuration)}`;
    if (maxDuration) url += `&max_duration=${encodeURIComponent(maxDuration)}`;
    renderTraceTable(await api(url));
}

function renderTraceTable(data) {
    const tbody = document.getElementById('trace-table-body');
    const emptyEl = document.getElementById('trace-empty');
    const countEl = document.getElementById('trace-count');
    if (!data?.traces?.length) {
        tbody.innerHTML = ''; emptyEl.style.display = 'block';
        emptyEl.textContent = data?.error ? `错误: ${data.error}` : '未找到 Trace';
        countEl.textContent = ''; return;
    }
    emptyEl.style.display = 'none';
    countEl.textContent = `共 ${data.traces.length} 条${data.selected_url ? ` · ${data.selected_url}` : ''}`;
    tbody.innerHTML = data.traces.map(t => {
        const durationMs = (t.total_duration_us / 1000).toFixed(1);
        const startTime = t.start_time ? new Date(t.start_time / 1000).toLocaleString('zh-CN') : '-';
        const shortId = t.traceID?.substring(0, 16) || '';
        const services = (t.services || []).slice(0, 3).join(', ');
        const moreServices = t.services?.length > 3 ? ` +${t.services.length - 3}` : '';
        return `<tr>
            <td><code style="font-size:11px">${escapeHtml(shortId)}</code></td>
            <td>${escapeHtml(t.root_service || '-')}</td><td>${escapeHtml(t.root_operation || '-')}</td>
            <td>${t.span_count}</td><td style="font-size:11px">${escapeHtml(services)}${moreServices}</td>
            <td>${durationMs} ms</td><td style="font-size:11px">${startTime}</td>
            <td><button class="btn btn-sm" onclick="viewTraceDetail('${t.traceID}')">详情</button></td>
        </tr>`;
    }).join('');
}

async function lookupTraceById() {
    const traceId = document.getElementById('trace-id-input')?.value?.trim();
    if (!traceId) { alert('请输入 Trace ID'); return; }
    await viewTraceDetail(traceId);
}

async function viewTraceDetail(traceId) {
    const card = document.getElementById('trace-detail-card');
    const content = document.getElementById('trace-detail-content');
    card.style.display = 'block';
    content.innerHTML = '<p class="text-muted">加载中...</p>';
    const data = await api(`/api/jaeger/trace/${traceId}`);
    if (!data || data.error) { content.innerHTML = `<p class="text-danger">加载失败: ${data?.error || '未知错误'}</p>`; return; }
    const spans = data.spans || [];
    if (!spans.length) { content.innerHTML = '<p class="text-muted">无 Span 数据</p>'; return; }
    const minStart = Math.min(...spans.map(s => s.startTime || Infinity));
    const maxEnd = Math.max(...spans.map(s => (s.startTime || 0) + (s.duration_us || 0)));
    const totalRange = maxEnd - minStart || 1;
    content.innerHTML = `
        <div style="margin-bottom:12px"><strong>Trace ID:</strong> <code>${escapeHtml(traceId)}</code> &nbsp; <strong>Span数:</strong> ${spans.length} &nbsp; <strong>总耗时:</strong> ${((maxEnd - minStart) / 1000).toFixed(1)} ms</div>
        <div class="trace-timeline">
            ${spans.map(s => {
                const left = ((s.startTime - minStart) / totalRange * 100).toFixed(2);
                const width = Math.max((s.duration_us / totalRange * 100), 0.5).toFixed(2);
                const dMs = (s.duration_us / 1000).toFixed(1);
                const hasError = s.tags?.['error'] === true || s.tags?.['otel.status_code'] === 'ERROR';
                const barColor = hasError ? 'var(--danger)' : 'var(--accent)';
                return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:2px;font-size:11px">
                    <span style="min-width:120px;text-align:right;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.serviceName)}">${escapeHtml(s.serviceName)}</span>
                    <span style="min-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.operationName)}">${escapeHtml(s.operationName)}</span>
                    <div style="flex:1;position:relative;height:14px;background:var(--bg-primary);border-radius:3px">
                        <div style="position:absolute;left:${left}%;width:${width}%;height:100%;background:${barColor};border-radius:3px;min-width:2px" title="${dMs} ms"></div>
                    </div>
                    <span style="min-width:60px;text-align:right">${dMs} ms</span>
                </div>`;
            }).join('')}
        </div>
    `;
}

// ─────────────────────────────────────────
// Events
// ─────────────────────────────────────────

async function loadEvents() {
    const ns = document.getElementById('event-ns')?.value || '';
    const data = await api(`/api/cluster/events?namespace=${ns}&limit=100`);
    if (!data?.events) return;
    const tbody = document.querySelector('#event-table tbody');
    tbody.innerHTML = data.events.map(e => `
        <tr>
            <td><span class="badge badge-${e.type === 'Warning' ? 'warning' : 'info'}">${e.type}</span></td>
            <td>${escapeHtml(e.reason)}</td><td>${escapeHtml(e.object)}</td>
            <td>${escapeHtml(e.message?.substring(0, 120) || '')}</td>
            <td>${e.count}</td><td>${formatTime(e.last_seen)}</td>
        </tr>
    `).join('');
}

// ─────────────────────────────────────────
// 配置中心 / 自愈中心
// ─────────────────────────────────────────

let _lastHealSuggestions = [];

async function loadConfigCenter() {
    const data = await api('/api/platform/config');
    const eff = data?.effective || {};
    renderConfigCenterDocs(data?.schema || {});
    const llm = eff.llm || {};
    const det = eff.detection || {};
    const heal = eff.remediation || {};
    const setVal = (id, value) => { const el = document.getElementById(id); if (el) el.value = value ?? ''; };
    const setCheck = (id, value) => { const el = document.getElementById(id); if (el) el.checked = !!value; };
    setVal('cfg-llm-model', llm.model);
    setVal('cfg-llm-base', llm.base_url);
    setVal('cfg-llm-key', '');
    setVal('cfg-llm-temp', llm.temperature);
    setVal('cfg-llm-maxtokens', llm.max_tokens);
    setVal('cfg-llm-timeout', llm.timeout);
    setVal('cfg-det-algo', det.default_algorithm || 'zscore');
    setVal('cfg-det-z', det.default_z_threshold);
    setVal('cfg-det-lookback', det.default_lookback_m);
    setVal('cfg-det-minsamples', det.min_samples);
    setVal('cfg-det-confirm', det.confirmation_points);
    setCheck('cfg-heal-enabled', heal.enabled);
    setCheck('cfg-heal-recommendonly', heal.recommend_only);
    setCheck('cfg-heal-dryrun', heal.dry_run);
    setCheck('cfg-heal-approval', heal.require_approval);
    setVal('cfg-heal-confidence', heal.confidence_threshold);
    setVal('cfg-heal-maxsteps', heal.max_steps);
    setVal('cfg-heal-risk', heal.max_auto_risk_level || 'medium');
    const out = document.getElementById('configcenter-output');
    if (out) out.textContent = JSON.stringify(eff, null, 2);
}

function renderConfigCenterDocs(schema) {
    const el = document.getElementById('configcenter-docs');
    if (!el) return;
    const sections = Object.entries(schema || {});
    if (!sections.length) {
        el.innerHTML = '<div class="empty-state">暂无参数说明</div>';
        return;
    }
    el.innerHTML = sections.map(([sectionKey, section]) => {
        const fields = Object.entries(section.fields || {}).map(([fieldKey, field]) => `
            <tr>
                <td><code>${escapeHtml(sectionKey)}.${escapeHtml(fieldKey)}</code></td>
                <td>${escapeHtml(field.label || fieldKey)}</td>
                <td>${escapeHtml(formatConfigMeta(field))}</td>
                <td>${escapeHtml(field.description || '')}</td>
            </tr>
        `).join('');
        return `
            <div class="config-doc-card">
                <div class="config-doc-title">
                    <span>${escapeHtml(section.label || sectionKey)}</span>
                    <small>${escapeHtml(section.description || '')}</small>
                </div>
                <table class="config-doc-table">
                    <thead><tr><th>参数</th><th>名称</th><th>默认/范围</th><th>说明</th></tr></thead>
                    <tbody>${fields}</tbody>
                </table>
            </div>
        `;
    }).join('');
}

function formatConfigMeta(field) {
    const parts = [];
    if (field.default !== undefined) parts.push(`默认 ${field.default === '' ? '空' : field.default}`);
    if (field.options) parts.push(`可选 ${field.options.join('/')}`);
    const range = [];
    if (field.min !== undefined) range.push(`>=${field.min}`);
    if (field.max !== undefined) range.push(`<=${field.max}`);
    if (range.length) parts.push(range.join(' '));
    if (field.unit) parts.push(field.unit);
    if (field.risk) parts.push(`风险 ${field.risk}`);
    return parts.join('；');
}

async function saveConfigCenter() {
    const payload = {
        llm: {
            model: document.getElementById('cfg-llm-model')?.value || '',
            base_url: document.getElementById('cfg-llm-base')?.value || '',
            temperature: parseFloat(document.getElementById('cfg-llm-temp')?.value || '0.1'),
            max_tokens: parseInt(document.getElementById('cfg-llm-maxtokens')?.value || '65536', 10),
            timeout: parseInt(document.getElementById('cfg-llm-timeout')?.value || '300', 10),
        },
        detection: {
            default_algorithm: document.getElementById('cfg-det-algo')?.value || 'zscore',
            default_z_threshold: parseFloat(document.getElementById('cfg-det-z')?.value || '3'),
            default_lookback_m: parseInt(document.getElementById('cfg-det-lookback')?.value || '30', 10),
            min_samples: parseInt(document.getElementById('cfg-det-minsamples')?.value || '12', 10),
            confirmation_points: parseInt(document.getElementById('cfg-det-confirm')?.value || '1', 10),
        },
        remediation: {
            enabled: document.getElementById('cfg-heal-enabled')?.checked === true,
            recommend_only: document.getElementById('cfg-heal-recommendonly')?.checked === true,
            dry_run: document.getElementById('cfg-heal-dryrun')?.checked !== false,
            require_approval: document.getElementById('cfg-heal-approval')?.checked !== false,
            confidence_threshold: parseFloat(document.getElementById('cfg-heal-confidence')?.value || '0.85'),
            max_steps: parseInt(document.getElementById('cfg-heal-maxsteps')?.value || '5', 10),
            max_auto_risk_level: document.getElementById('cfg-heal-risk')?.value || 'medium',
        },
    };
    const apiKey = document.getElementById('cfg-llm-key')?.value || '';
    if (apiKey) payload.llm.api_key = apiKey;
    const res = await api('/api/platform/config', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });
    const out = document.getElementById('configcenter-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

function healRequestBody() {
    return {
        namespace: document.getElementById('heal-namespace')?.value || 'default',
        message: document.getElementById('heal-message')?.value || '',
        pod: document.getElementById('heal-pod')?.value || '',
        node: document.getElementById('heal-node')?.value || '',
        dry_run: document.getElementById('heal-dryrun')?.checked !== false,
    };
}

async function loadHealCenter() {
    const [recipes, runs] = await Promise.all([
        api('/api/heal/recipes'),
        api('/api/heal/runs?limit=10'),
    ]);
    renderHealRecipesMeta(recipes || {});
    renderHealRecipes(recipes?.recipes || [], recipes?.grouped || null);
    _lastHealRuns = runs?.runs || [];
    renderHealRunsSummary(runs?.summary || {});
    renderHealRuns(runs?.runs || []);
}

function renderHealRecipesMeta(data) {
    const el = document.getElementById('heal-recipes-meta');
    if (!el) return;
    const validation = data.validation || {};
    const ok = validation.ok !== false;
    const issues = validation.issues || [];
    el.innerHTML = `
        <div class="heal-recipes-meta-row">
            <span class="badge badge-info">version ${escapeHtml(data.version || '-')}</span>
            <span class="badge badge-info">${escapeHtml(data.total || 0)} 条策略</span>
            <span class="badge badge-${ok ? 'success' : 'danger'}">${ok ? '校验通过' : '校验失败'}</span>
            ${validation.warning_count ? `<span class="badge badge-warning">${validation.warning_count} warnings</span>` : ''}
            ${validation.error_count ? `<span class="badge badge-danger">${validation.error_count} errors</span>` : ''}
        </div>
        <div class="heal-recipes-source">${escapeHtml(data.source || '')}</div>
        ${issues.length ? `<details class="heal-recipe-validation"><summary>查看校验问题</summary>${issues.map(renderHealRecipeIssue).join('')}</details>` : ''}
    `;
}

function renderHealRecipeIssue(issue) {
    return `<div class="heal-recipe-issue">
        <span class="badge badge-${issue.severity === 'error' ? 'danger' : 'warning'}">${escapeHtml(issue.severity || 'warning')}</span>
        <strong>${escapeHtml(issue.recipe || '-')}</strong>
        <span>${escapeHtml(issue.field || '')}</span>
        <small>${escapeHtml(issue.message || '')}</small>
    </div>`;
}

function renderHealRecipes(recipes, grouped = null) {
    const el = document.getElementById('heal-recipes');
    if (!el) return;
    if (!recipes.length) {
        el.innerHTML = '<p class="text-muted">暂无自愈知识库规则</p>';
        return;
    }
    const groups = grouped && Object.keys(grouped).length
        ? grouped
        : recipes.reduce((acc, r) => {
            const key = r.category || 'unknown';
            acc[key] = acc[key] || [];
            acc[key].push(r);
            return acc;
        }, {});
    el.innerHTML = Object.entries(groups).sort(([a], [b]) => a.localeCompare(b, 'zh-CN')).map(([category, items]) => `
        <details class="heal-recipe-group" ${category === '应用' || category === '资源' ? 'open' : ''}>
            <summary>
                <strong>${escapeHtml(category)}</strong>
                <span class="badge badge-info">${items.length} 条策略</span>
            </summary>
            <div class="heal-recipe-group-body">
                ${items.map(r => `<div class="fault-card heal-recipe-card">
                    <div><span class="badge badge-info">${escapeHtml(r.category || '')}</span>
                    <span class="badge badge-${r.risk === 'high' ? 'danger' : r.risk === 'medium' ? 'warning' : 'success'}">${escapeHtml(r.risk || '')}</span>
                    <span class="badge badge-info">${escapeHtml(r.tier || 'kubectl')}</span>
                    <span class="badge badge-success">${escapeHtml(r.coverage || 'tested')}</span>
                    ${r.requires_approval ? '<span class="badge badge-warning">审批</span>' : '<span class="badge badge-success">可预演</span>'}</div>
                    <strong>${escapeHtml(r.fault_type || '')}</strong>
                    <p>${escapeHtml(r.description || '')}</p>
                    ${r.how_fixed ? `<small class="text-muted">${escapeHtml(r.how_fixed)}</small>` : ''}
                    <div class="heal-recipe-signals">${(r.signals || []).slice(0, 4).map(s => `<span>${escapeHtml(s)}</span>`).join('')}</div>
                    <details class="heal-recipe-actions-fold">
                        <summary>动作模板 ${(r.actions || []).length} 条</summary>
                        <div class="heal-recipe-actions">
                            ${(r.actions || []).map((a, i) => renderHealRecipeAction(a, i)).join('') || '<p class="text-muted">暂无动作模板</p>'}
                        </div>
                    </details>
                </div>`).join('')}
            </div>
        </details>
    `).join('');
}

function renderHealRecipeAction(action, index) {
    const risk = (action.risk || 'low').toLowerCase();
    const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
    return `<div class="heal-recipe-action">
        <div class="heal-recipe-action-head">
            <span class="rem-action-num">${index + 1}</span>
            <span class="badge badge-${riskClass}">${escapeHtml(action.risk || 'low')}</span>
            <strong>${escapeHtml(action.step || '')}</strong>
        </div>
        <div class="rem-action-cmd"><code>${escapeHtml(action.command || '')}</code></div>
        ${action.rollback_command ? `<div class="rem-action-rollback">rollback: ${escapeHtml(action.rollback_command)}</div>` : ''}
    </div>`;
}

function healVerificationBadge(verification) {
    if (!verification) return '';
    if (verification.status === 'skipped') {
        return '<span class="badge badge-info">验证跳过</span>';
    }
    if (verification.recovered === true) {
        return '<span class="badge badge-success">已验证恢复</span>';
    }
    if (verification.recovered === false) {
        return '<span class="badge badge-danger">验证未恢复</span>';
    }
    return '<span class="badge badge-warning">验证未知</span>';
}

function renderHealRunsSummary(summary = {}) {
    const el = document.getElementById('heal-runs-summary');
    if (!el) return;
    el.innerHTML = `
        <span class="badge badge-info">total ${escapeHtml(summary.total ?? 0)}</span>
        <span class="badge badge-info">real ${escapeHtml(summary.real ?? 0)}</span>
        <span class="badge badge-info">dry-run ${escapeHtml(summary.dry_run ?? 0)}</span>
        <span class="badge badge-success">recovered ${escapeHtml(summary.verified_recovered ?? 0)}</span>
        <span class="badge badge-danger">unrecovered ${escapeHtml(summary.verified_unrecovered ?? 0)}</span>
        <span class="badge badge-warning">failed ${escapeHtml(summary.failed ?? 0)}</span>
    `;
}

function renderHealRuns(runs) {
    const el = document.getElementById('heal-runs');
    if (!el) return;
    const filter = document.getElementById('heal-run-filter')?.value || 'all';
    const filtered = (runs || []).filter(r => {
        if (filter === 'dry_run') return r.status === 'dry_run' || r.dry_run === true;
        if (filter === 'real') return r.dry_run === false;
        if (filter === 'failed') return r.success === false || r.status === 'failed';
        if (filter === 'recovered') return r.verification?.recovered === true;
        if (filter === 'unrecovered') return r.verification?.recovered === false;
        return true;
    });
    el.innerHTML = filtered.length ? filtered.map(r => `<div class="fault-exp-run">
        <div>
            <span class="badge badge-${r.success ? 'success' : r.status === 'dry_run' ? 'info' : 'danger'}">${escapeHtml(r.status || '')}</span>
            <span class="badge badge-${r.diagnosis_mode === 'rca_result' ? 'success' : 'warning'}">${r.diagnosis_mode === 'rca_result' ? '诊断驱动' : '告警驱动'}</span>
            ${healVerificationBadge(r.verification)}
            <strong>${escapeHtml(r.fault_type || r.id)}</strong>
            <span class="text-muted">${escapeHtml(r.namespace || '')} · actions=${(r.commands || []).length} · ${formatTime((r.started_at || 0) * 1000)}</span>
        </div>
        <div class="table-actions">
            <button class="btn btn-sm" onclick="showHealRun('${escapeHtml(r.id)}')">查看</button>
            ${(r.rollback_commands || []).length ? `<button class="btn btn-sm btn-warning" onclick="rollbackHealRun('${escapeHtml(r.id)}')">回滚预演</button>` : ''}
        </div>
    </div>`).join('') : '<p class="text-muted">暂无匹配的自愈运行记录</p>';
}

async function suggestHeal() {
    const res = await api('/api/heal/suggest', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(healRequestBody()),
    });
    _lastHealResult = res || null;
    _lastHealSuggestions = res?.suggestions || [];
    renderHealSuggestions(_lastHealSuggestions, res?.blocked_templates || []);
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
    if (res) openHealPlanModal(res, healRequestBody(), { source: 'heal-center' });
}

function renderHealSuggestions(suggestions, blocked) {
    const el = document.getElementById('heal-suggestions');
    if (!el) return;
    const cards = suggestions.map(s => `<div class="heal-card">
        <span class="badge badge-${s.risk === 'high' ? 'danger' : s.risk === 'medium' ? 'warning' : 'success'}">${escapeHtml(s.risk || '')}</span>
        <strong>${escapeHtml(s.step || '')}</strong>
        <code>${escapeHtml(s.command || '')}</code>
    </div>`);
    const blockedCards = blocked.map(s => `<div class="heal-card">
        <span class="badge badge-gray">blocked</span>
        <strong>${escapeHtml(s.step || '')}</strong>
        <code>${escapeHtml(s.command || '')}</code>
        <p class="text-muted">${escapeHtml(s.blocked_reason || '')}</p>
    </div>`);
    el.innerHTML = cards.concat(blockedCards).join('') || '<p class="text-muted">未生成可执行建议</p>';
}

async function executeHealDryRun() {
    const body = healRequestBody();
    if (_lastHealSuggestions.length) {
        body.commands = _lastHealSuggestions.map(s => s.command).filter(Boolean);
        body.rollback_commands = _lastHealSuggestions.map(s => s.rollback_command).filter(Boolean);
    }
    body.approved = true;
    const res = await api('/api/heal/execute', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    });
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
    await loadHealCenter();
}

async function openHealPlanFromCenter() {
    if (_lastHealResult) {
        openHealPlanModal(_lastHealResult, healRequestBody(), { source: 'heal-center' });
        return;
    }
    await suggestHeal();
}

function openHealPlanModal(result, sourcePayload = {}, options = {}) {
    _currentHealPlan = result || {};
    _currentHealPlanSourcePayload = { ...(sourcePayload || {}), source: options.source || sourcePayload.source || 'heal-plan-modal' };
    const modal = document.getElementById('heal-plan-modal');
    if (!modal) return;
    const target = _currentHealPlan.target || {};
    const capability = _currentHealPlan.capability || {};
    const policy = _currentHealPlan.policy || {};
    const suggestions = (_currentHealPlan.suggestions || []).map((s, i) => ({ ...s, _idx: i }));
    const blocked = _currentHealPlan.blocked_templates || [];
    const diagnosis = _currentHealPlanSourcePayload?.diagnosis || _currentHealPlan?.diagnosis || null;
    const diagnosisGate = _currentHealPlan.diagnosis_gate || {};
    const diagnosisMode = diagnosis ? '诊断结果驱动' : '告警文本降级';
    const verificationPlan = _currentHealPlan.verification_plan || {};
    document.getElementById('heal-plan-subtitle').textContent =
        `${_currentHealPlan.fault_type || sourcePayload.fault_type || 'unknown'} · namespace ${_currentHealPlan.namespace || sourcePayload.namespace || 'default'}`;
    document.getElementById('heal-plan-summary').innerHTML = `
        <div>
            <span class="badge badge-${suggestions.length ? 'success' : 'warning'}">${suggestions.length ? '可执行方案' : '无可执行动作'}</span>
            <span class="badge badge-${diagnosis ? 'success' : 'warning'}">${diagnosisMode}</span>
            <span class="badge badge-info">pod=${escapeHtml(target.pod || sourcePayload.pod || '-')}</span>
            <span class="badge badge-info">deployment=${escapeHtml(target.deployment || sourcePayload.deployment || '-')}</span>
            <span class="badge badge-info">node=${escapeHtml(target.node || sourcePayload.node || '-')}</span>
            ${capability.execution_enabled ? '<span class="badge badge-success">真实执行已启用</span>' : '<span class="badge badge-warning">真实执行未启用</span>'}
            <span class="badge badge-${diagnosisGate.ready ? 'success' : 'warning'}">${diagnosisGate.ready ? '诊断门禁通过' : '诊断门禁未通过'}</span>
            <span class="badge badge-info">conf=${escapeHtml(formatConfidence(diagnosisGate.confidence))}/${escapeHtml(formatConfidence(diagnosisGate.confidence_threshold))}</span>
            ${policy.recommend_only ? '<span class="badge badge-warning">仅建议模式</span>' : ''}
            ${capability.requires_approval ? '<span class="badge badge-warning">需要审批</span>' : ''}
            ${capability.requires_risk_ack ? '<span class="badge badge-danger">高风险确认</span>' : ''}
        </div>
        <p>${escapeHtml(diagnosis ? `诊断根因: ${diagnosis.root_cause || diagnosis.fault_type || '-'}` : '当前未关联分析诊断结果，方案基于告警文本生成；建议先完成分析再执行真实自愈。')}</p>
        ${renderDiagnosisGate(diagnosisGate)}
        <p>${escapeHtml(_currentHealPlan.diagnosis_summary || _currentHealPlan.recipe?.description || '可修改命令后执行；默认 Dry-run 不会改变集群。')}</p>
        ${renderHealVerificationPlan(verificationPlan)}
    `;
    document.getElementById('heal-plan-dryrun').checked = true;
    document.getElementById('heal-plan-approved').checked = false;
    document.getElementById('heal-plan-riskack').checked = false;
    document.getElementById('heal-plan-select-all').checked = true;
    document.getElementById('heal-plan-actions').innerHTML = suggestions.length
        ? suggestions.map((s, i) => renderHealPlanActionEditor(s, i)).join('')
        : '<p class="text-muted">未生成可执行动作。请补齐 namespace/pod/deployment/node 后重新生成。</p>';
    document.getElementById('heal-plan-blocked').innerHTML = blocked.length
        ? `<details class="heal-blocked-fold"><summary>被阻断模板 ${blocked.length} 条</summary>${blocked.map(renderHealBlockedPlanItem).join('')}</details>`
        : '';
    const out = document.getElementById('heal-plan-output');
    if (out) out.textContent = suggestions.length
        ? healPlanGateMessage(capability, policy)
        : '当前没有可执行命令。';
    modal.style.display = 'flex';
}

function formatConfidence(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return '0%';
    return `${Math.round(n * 100)}%`;
}

function renderDiagnosisGate(gate = {}) {
    const blockers = gate.blockers || [];
    if (gate.ready) {
        return `<div class="heal-diagnosis-gate success">
            <span class="badge badge-success">真实执行准入</span>
            <span>RCA 置信度、根因和故障类型满足配置中心门禁。</span>
        </div>`;
    }
    return `<div class="heal-diagnosis-gate warning">
        <span class="badge badge-warning">真实执行受限</span>
        <span>${escapeHtml(blockers.length ? blockers.join(' / ') : '缺少可执行诊断结论')}</span>
    </div>`;
}

function renderHealVerificationPlan(plan = {}) {
    const checks = plan.checks || [];
    if (!checks.length) return '';
    return `<div class="heal-verification-plan">
        <div class="heal-verification-title">
            <span class="badge badge-info">执行后验证</span>
            <strong>${escapeHtml(plan.mode || 'namespace')}</strong>
        </div>
        ${checks.map(c => `<code>${escapeHtml(c.command || '')}</code>`).join('')}
    </div>`;
}

function healPlanGateMessage(capability = {}, policy = {}) {
    const blockers = [];
    if (policy.recommend_only || capability.policy === 'recommend_only') blockers.push('配置中心处于仅建议模式');
    if (!capability.execution_enabled) blockers.push('配置中心未启用真实自愈');
    if (capability.diagnosis_ready === false) blockers.push('诊断门禁未通过');
    if (capability.requires_approval) blockers.push('真实执行需要勾选“已审批”');
    if (capability.requires_risk_ack) blockers.push('包含高风险动作，需要勾选“高风险确认”');
    const prefix = '可编辑命令和回滚命令。Dry-run 只记录计划。';
    return blockers.length ? `${prefix}\n真实执行门禁：${blockers.join('；')}。` : `${prefix}\n真实执行门禁已满足配置要求，仍建议先 Dry-run。`;
}

function renderHealPlanActionEditor(action, index) {
    const risk = (action.risk || 'low').toLowerCase();
    const riskClass = risk === 'high' ? 'danger' : risk === 'medium' ? 'warning' : 'success';
    return `<div class="heal-plan-action" data-index="${index}">
        <div class="heal-plan-action-head">
            <label class="inline-check"><input class="heal-plan-action-enabled" type="checkbox" checked> 执行</label>
            <span class="rem-action-num">${index + 1}</span>
            <span class="badge badge-${riskClass}">${escapeHtml(action.risk || 'low')}</span>
            <span class="badge badge-info">${escapeHtml(action.source || 'dynamic')}</span>
            <input class="input-sm heal-plan-step" value="${escapeHtml(action.step || '')}" placeholder="动作说明">
        </div>
        <label class="config-field">
            <span>命令</span>
            <textarea class="heal-plan-command" rows="2">${escapeHtml(action.command || '')}</textarea>
        </label>
        <label class="config-field">
            <span>回滚命令</span>
            <textarea class="heal-plan-rollback" rows="1">${escapeHtml(action.rollback_command || '')}</textarea>
        </label>
        ${action.selection_reason ? `<small class="text-muted">score=${escapeHtml(action.selection_score ?? '-')} · ${escapeHtml(action.selection_reason)}</small>` : ''}
        ${action.impact ? `<small class="text-muted">${escapeHtml(action.impact)}</small>` : ''}
    </div>`;
}

function renderHealBlockedPlanItem(item) {
    return `<div class="heal-plan-blocked-item">
        <strong>${escapeHtml(item.step || 'blocked')}</strong>
        <code>${escapeHtml(item.command || '')}</code>
        <small>${escapeHtml(item.blocked_reason || '')}</small>
    </div>`;
}

function closeHealPlanModal() {
    const modal = document.getElementById('heal-plan-modal');
    if (modal) modal.style.display = 'none';
}

function toggleHealPlanAll(checked) {
    document.querySelectorAll('#heal-plan-actions .heal-plan-action-enabled').forEach(el => { el.checked = checked; });
}

function collectHealPlanActions() {
    return Array.from(document.querySelectorAll('#heal-plan-actions .heal-plan-action')).map((el, index) => {
        const enabled = el.querySelector('.heal-plan-action-enabled')?.checked === true;
        return {
            enabled,
            step: el.querySelector('.heal-plan-step')?.value || `动作 ${index + 1}`,
            command: (el.querySelector('.heal-plan-command')?.value || '').trim(),
            rollback_command: (el.querySelector('.heal-plan-rollback')?.value || '').trim(),
        };
    }).filter(a => a.enabled && a.command);
}

async function executeHealPlan(forceDryRun = null) {
    const dryRunEl = document.getElementById('heal-plan-dryrun');
    const dryRun = forceDryRun === null ? dryRunEl?.checked !== false : !!forceDryRun;
    if (dryRunEl) dryRunEl.checked = dryRun;
    const actions = collectHealPlanActions();
    const out = document.getElementById('heal-plan-output');
    if (!actions.length) {
        if (out) out.textContent = '没有选中任何可执行动作。';
        return;
    }
    const capability = _currentHealPlan?.capability || {};
    const policy = _currentHealPlan?.policy || {};
    const diagnosisGate = _currentHealPlan?.diagnosis_gate || {};
    if (!dryRun) {
        const localBlockers = [];
        if (policy.recommend_only || capability.policy === 'recommend_only') localBlockers.push('配置中心处于仅建议模式');
        if (!capability.execution_enabled) localBlockers.push('配置中心未启用真实自愈');
        if (diagnosisGate.ready === false || capability.diagnosis_ready === false) localBlockers.push('诊断门禁未通过');
        if (capability.requires_approval && document.getElementById('heal-plan-approved')?.checked !== true) localBlockers.push('需要勾选“已审批”');
        if (capability.requires_risk_ack && document.getElementById('heal-plan-riskack')?.checked !== true) localBlockers.push('需要勾选“高风险确认”');
        if (localBlockers.length) {
            if (out) out.textContent = `真实执行未提交：${localBlockers.join('；')}。`;
            showToast('真实执行门禁未满足', 'error');
            return;
        }
    }
    const payload = {
        ...(_currentHealPlanSourcePayload || {}),
        namespace: _currentHealPlan?.namespace || _currentHealPlanSourcePayload?.namespace || 'default',
        fault_type: _currentHealPlan?.fault_type || _currentHealPlanSourcePayload?.fault_type || '',
        dry_run: dryRun,
        approved: document.getElementById('heal-plan-approved')?.checked === true,
        risk_ack: document.getElementById('heal-plan-riskack')?.checked === true,
        commands: actions.map(a => a.command),
        rollback_commands: actions.map(a => a.rollback_command).filter(Boolean),
        edited_actions: actions,
    };
    if (out) out.textContent = dryRun ? 'Dry-run 预演中...' : '真实执行中...';
    try {
        const res = await api('/api/heal/execute', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
            throwOnError: true,
        });
        if (out) out.textContent = JSON.stringify(res, null, 2);
        showToast(dryRun ? '自愈 dry-run 已完成' : '自愈执行已提交');
        await loadHealCenter();
    } catch (e) {
        const detail = e?.data?.detail || e?.data || e?.message || '执行失败';
        if (out) out.textContent = typeof detail === 'string' ? detail : JSON.stringify(detail, null, 2);
        showToast('自愈执行被拦截或失败', 'error');
    }
}

async function submitHealPlanFeedback() {
    const actions = collectHealPlanActions();
    const rating = document.getElementById('heal-plan-feedback-rating')?.value || 'helpful';
    const note = document.getElementById('heal-plan-feedback-note')?.value || '';
    const payload = {
        incident_id: `heal-plan-${Date.now()}`,
        expert_diagnosis: [
            `自愈方案反馈: ${rating}`,
            `故障类型: ${_currentHealPlan?.fault_type || '-'}`,
            `目标: ${JSON.stringify(_currentHealPlan?.target || {})}`,
            `动作: ${actions.map(a => `${a.step}: ${a.command}`).join(' | ')}`,
        ].join('\n'),
        comment: note || `source=${_currentHealPlanSourcePayload?.source || 'heal-plan-modal'}`,
    };
    const res = await api('/api/knowledge/feedback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });
    const out = document.getElementById('heal-plan-output');
    if (out) out.textContent = JSON.stringify(res || payload, null, 2);
    showToast(res ? '自愈反馈已记录' : '反馈提交失败', res ? 'success' : 'error');
}

async function showHealRun(runId) {
    const res = await api(`/api/heal/runs/${runId}`);
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

async function rollbackHealRun(runId) {
    const res = await api(`/api/heal/runs/${runId}/rollback`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ dry_run: true, approved: true }),
    });
    const out = document.getElementById('heal-output');
    if (out) out.textContent = JSON.stringify(res, null, 2);
    if (res?.success) {
        showToast('自愈回滚 dry-run 已完成');
        await loadHealCenter();
    } else {
        showToast('自愈回滚 dry-run 失败', 'error');
    }
}

// ─────────────────────────────────────────
// Knowledge Base (故障知识库)
// ─────────────────────────────────────────

async function loadKnowledge() {
    const [stats, rulesData, faultsData, fbData] = await Promise.all([
        api('/api/knowledge/stats'),
        api('/api/knowledge/rules'),
        api('/api/knowledge/faults'),
        api('/api/knowledge/feedback'),
    ]);

    if (stats) {
        document.getElementById('kb-rules-count').textContent = stats.rules_count || 0;
        document.getElementById('kb-faults-count').textContent = stats.faults_count || 0;
        document.getElementById('kb-feedback-count').textContent = stats.feedback_count || 0;
        document.getElementById('kb-generated-count').textContent = stats.total_rules_generated || 0;
    }

    if (rulesData) {
        _allRules = rulesData.rules || [];
        renderRulesTable(_allRules);
    }
    if (faultsData) renderFaultsTable(faultsData.faults || []);
    if (fbData) renderFeedbackList(fbData.feedback || []);
    loadFeedbackIncidents();
}

// ─────────────────────────────────────────
// Evolution / HITL / SOW Alignment
// ─────────────────────────────────────────

async function loadEvolution() {
    const [sow, evo, hitl, governance] = await Promise.all([
        api('/api/sow/alignment'),
        api('/api/evolution/report'),
        api('/api/hitl/reviews?limit=30'),
        api('/api/knowledge/governance'),
    ]);

    if (governance) {
        document.getElementById('evo-memory-health').textContent = Math.round((governance.health_score || 0) * 100) + '%';
        document.getElementById('evo-low-quality').textContent = governance.low_quality_rules || 0;
        renderMemoryGovernance(governance);
    }
    if (hitl?.stats) {
        document.getElementById('evo-hitl-pending').textContent = hitl.stats.pending || 0;
        renderHitlReviews(hitl.reviews || []);
    }
    if (evo) {
        document.getElementById('evo-snapshots').textContent = evo.total_snapshots || 0;
        const recs = evo.recommendations || [];
        document.getElementById('evolution-recommendations').innerHTML = recs.length
            ? recs.map(r => `<div class="signal-item"><span>${escapeHtml(r)}</span></div>`).join('')
            : '<p class="text-muted">暂无建议</p>';
    }
    if (sow) renderSowAlignment(sow.items || [], sow.signals || {});
}

function renderSowAlignment(items, signals) {
    const el = document.getElementById('sow-alignment-list');
    el.innerHTML = items.map(item => {
        const cls = item.status === 'implemented' ? 'success' : item.status === 'verifying' ? 'warning' : 'gray';
        return `<div class="sow-item">
            <div><strong>${escapeHtml(item.requirement)}</strong><p>${escapeHtml(item.implementation)}</p></div>
            <span class="badge badge-${cls}">${escapeHtml(item.status)}</span>
        </div>`;
    }).join('');

    if (signals.fault_scenarios) {
        el.innerHTML += `<div class="sow-footnote">故障场景 ${signals.fault_scenarios.count || 0} 个；类型：${escapeHtml((signals.fault_scenarios.fault_types || []).join(', '))}</div>`;
    }
}

function renderMemoryGovernance(g) {
    const conflicts = g.conflicts || [];
    document.getElementById('memory-governance').innerHTML = `
        <div class="grid-3col" style="margin-bottom:12px">
            <div class="mini-metric"><span>平均质量</span><strong>${Math.round((g.avg_quality_score || 0) * 100)}%</strong></div>
            <div class="mini-metric"><span>陈旧规则</span><strong>${g.stale_rules || 0}</strong></div>
            <div class="mini-metric"><span>冲突组</span><strong>${conflicts.length}</strong></div>
        </div>
        ${conflicts.length ? conflicts.map(c => `<div class="signal-item"><span><span class="badge badge-warning">冲突</span> ${escapeHtml(c.condition || '')}</span><span class="text-muted">${(c.rule_ids || []).join(', ')}</span></div>`).join('') : '<p class="text-muted">未发现明显规则冲突</p>'}
    `;
}

function renderHitlReviews(reviews) {
    const el = document.getElementById('hitl-review-list');
    if (!reviews.length) {
        el.innerHTML = '<p class="text-muted">暂无待处理审核项</p>';
        return;
    }
    el.innerHTML = reviews.map(r => {
        const root = r.rca_result?.root_cause || '';
        const score = r.judge?.combined_score ?? '-';
        const disabled = r.status !== 'pending' ? 'disabled' : '';
        return `<div class="hitl-item">
            <div class="hitl-main">
                <div><span class="badge badge-${r.priority === 'high' ? 'danger' : 'warning'}">${escapeHtml(r.priority || 'medium')}</span> <strong>${escapeHtml(r.review_id)}</strong> <span class="text-muted">${formatTime(r.created_at ? r.created_at * 1000 : null)}</span></div>
                <p>${escapeHtml(r.reason || '')}</p>
                <p><strong>根因:</strong> ${escapeHtml(root.substring(0, 180))}</p>
                <p><strong>Judge:</strong> ${score} / ${escapeHtml(String(r.judge?.judge_level ?? '-'))}</p>
                <textarea class="textarea" rows="2" id="hitl-diag-${r.review_id}" placeholder="专家诊断结论，可用于监督学习">${escapeHtml(r.expert_diagnosis || '')}</textarea>
            </div>
            <div class="hitl-actions">
                <button class="btn btn-sm btn-primary" ${disabled} onclick="decideHitl('${r.review_id}','approve')">通过</button>
                <button class="btn btn-sm btn-danger" ${disabled} onclick="decideHitl('${r.review_id}','reject')">驳回</button>
                <button class="btn btn-sm" ${disabled} onclick="decideHitl('${r.review_id}','needs_more_evidence')">补证据</button>
            </div>
        </div>`;
    }).join('');
}

async function decideHitl(reviewId, decision) {
    const diagnosis = document.getElementById(`hitl-diag-${reviewId}`)?.value?.trim() || '';
    const result = await api(`/api/hitl/reviews/${reviewId}/decision`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, reviewer: 'web', expert_diagnosis: diagnosis, comment: 'submitted from dashboard' }),
    });
    if (result?.status === 'ok') {
        showToast('审核已提交');
        loadEvolution();
        loadKnowledge();
    } else {
        showToast('审核提交失败', 'error');
    }
}

// ─────────────────────────────────────────
// 通算故障实验
// ─────────────────────────────────────────

let _faultScenarios = [];
let _k8sFaultExperimentSteps = [];
let _faultTargets = { k8s_clusters: [], llm_hosts: [] };

async function loadFaultTargets() {
    const data = await api('/api/fault-targets');
    if (data) {
        _faultTargets = {
            k8s_clusters: data.k8s_clusters || [],
            llm_hosts: data.llm_hosts || [],
        };
    }
    populateClusterDropdown('faultlab-cluster');
    populateClusterDropdown('llmfaultlab-cluster');
    populateHostDropdown('llmfaultlab-host');
}

function populateClusterDropdown(elemId) {
    const sel = document.getElementById(elemId);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = _faultTargets.k8s_clusters.length
        ? _faultTargets.k8s_clusters.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)}${c.context ? ' · ' + escapeHtml(c.context) : ''}</option>`).join('')
        : '<option value="">未配置集群</option>';
    if (prev && _faultTargets.k8s_clusters.some(c => c.id === prev)) sel.value = prev;
}

function populateHostDropdown(elemId) {
    const sel = document.getElementById(elemId);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = _faultTargets.llm_hosts.length
        ? _faultTargets.llm_hosts.map(h => `<option value="${escapeHtml(h.id)}">${escapeHtml(h.name || h.id)} · ${escapeHtml(h.host || '')}</option>`).join('')
        : '<option value="">未配置 GPU 主机</option>';
    if (prev && _faultTargets.llm_hosts.some(h => h.id === prev)) sel.value = prev;
}

async function loadFaultLab() {
    await loadFaultTargets();
    const data = await api('/api/faultlab/scenarios');
    _faultScenarios = data?.scenarios || [];
    const sel = document.getElementById('faultlab-scenario');
    if (!sel) return;
    sel.innerHTML = _faultScenarios.map(s => `<option value="${s.id}">${s.id} — ${escapeHtml(s.name || '')}</option>`).join('');
    sel.onchange = renderFaultScenarioDetail;
    renderFaultScenarioDetail();
    renderFaultScenarioGrid();
    renderK8sFaultExperimentSteps();
    loadK8sFaultExperiments();
}

function selectedFaultScenario() {
    const id = document.getElementById('faultlab-scenario')?.value;
    return _faultScenarios.find(s => s.id === id);
}

function renderFaultScenarioDetail() {
    const s = selectedFaultScenario();
    const el = document.getElementById('faultlab-scenario-detail');
    if (!s || !el) return;
    const cmds = s.inject?.commands || [];
    el.innerHTML = `<div class="fault-detail">
        <div><span class="badge badge-info">${escapeHtml(s.category || '')}</span> <span class="badge badge-warning">${escapeHtml(s.fault_type || '')}</span> <strong>${escapeHtml(s.target_service || '')}</strong></div>
        <p>${escapeHtml(s.description || '')}</p>
        <pre>${escapeHtml(cmds.join('\n'))}</pre>
    </div>`;
}

function renderFaultScenarioGrid() {
    const el = document.getElementById('faultlab-grid');
    if (!el) return;
    el.innerHTML = _faultScenarios.map(s => `<div class="fault-card">
        <div><span class="badge badge-info">${escapeHtml(s.category || '')}</span></div>
        <strong>${escapeHtml(s.name || s.id)}</strong>
        <p>${escapeHtml((s.description || '').substring(0, 120))}</p>
    </div>`).join('');
}

function faultLabBaseBody() {
    const body = {
        cluster_id: document.getElementById('faultlab-cluster')?.value || 'default',
    };
    const nsOverride = document.getElementById('faultlab-ns-override')?.value?.trim();
    if (nsOverride) body.namespace = nsOverride;
    return body;
}

async function injectFaultScenario() {
    const s = selectedFaultScenario();
    if (!s) return;
    const dryRun = document.getElementById('faultlab-dryrun')?.checked;
    const backgroundLoad = document.getElementById('faultlab-load')?.checked;
    const out = document.getElementById('faultlab-output');
    out.textContent = '执行中...';
    const res = await api(`/api/faultlab/inject/${s.id}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...faultLabBaseBody(), dry_run: dryRun, background_load: backgroundLoad }),
    });
    out.textContent = JSON.stringify(res, null, 2);
}

async function cleanupFaultScenario() {
    const s = selectedFaultScenario();
    if (!s) return;
    const dryRun = document.getElementById('faultlab-dryrun')?.checked;
    const out = document.getElementById('faultlab-output');
    out.textContent = '清理中...';
    const res = await api(`/api/faultlab/cleanup/${s.id}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...faultLabBaseBody(), dry_run: dryRun }),
    });
    out.textContent = JSON.stringify(res, null, 2);
}

function addK8sFaultExperimentStep() {
    const s = selectedFaultScenario();
    if (!s) return;
    _k8sFaultExperimentSteps.push({
        scenario_id: s.id,
        name: s.name || s.id,
        fault_type: s.fault_type || '',
        category: s.category || '',
        action: 'inject',
        hold_s: 30,
        wait_after_s: parseInt(document.getElementById('k8s-fault-exp-wait')?.value || '5', 10) || 0,
    });
    renderK8sFaultExperimentSteps();
}

function removeK8sFaultExperimentStep(index) {
    _k8sFaultExperimentSteps.splice(index, 1);
    renderK8sFaultExperimentSteps();
}

function updateK8sFaultExperimentStep(index, field, value) {
    if (!_k8sFaultExperimentSteps[index]) return;
    if (field === 'hold_s' || field === 'wait_after_s') {
        _k8sFaultExperimentSteps[index][field] = Math.max(0, parseInt(value || '0', 10) || 0);
    } else {
        _k8sFaultExperimentSteps[index][field] = value;
    }
}

function renderK8sFaultExperimentSteps() {
    const el = document.getElementById('k8s-fault-exp-steps');
    if (!el) return;
    if (!_k8sFaultExperimentSteps.length) {
        el.innerHTML = '<p class="text-muted" style="font-size:13px">尚未添加连续实验步骤。选择一个通算故障场景后点击“添加当前场景”。</p>';
        return;
    }
    el.innerHTML = _k8sFaultExperimentSteps.map((step, i) => `
        <div class="fault-exp-step">
            <div class="fault-exp-step-main">
                <span class="badge badge-info">#${i + 1}</span>
                <strong>${escapeHtml(step.scenario_id)}</strong>
                <span class="text-muted">${escapeHtml(step.name || '')}</span>
                <span class="badge badge-warning">${escapeHtml(step.fault_type || '')}</span>
            </div>
            <select class="select-sm" onchange="updateK8sFaultExperimentStep(${i}, 'action', this.value)">
                <option value="inject" ${step.action === 'inject' ? 'selected' : ''}>inject/hold/cleanup</option>
                <option value="experiment" ${step.action === 'experiment' ? 'selected' : ''}>inject/hold/cleanup</option>
            </select>
            <input class="input-sm" type="number" min="0" value="${step.hold_s}" title="hold seconds"
                onchange="updateK8sFaultExperimentStep(${i}, 'hold_s', this.value)">
            <input class="input-sm" type="number" min="0" value="${step.wait_after_s}" title="wait after seconds"
                onchange="updateK8sFaultExperimentStep(${i}, 'wait_after_s', this.value)">
            <button class="btn btn-sm btn-danger" onclick="removeK8sFaultExperimentStep(${i})">移除</button>
        </div>
    `).join('');
}

function k8sFaultExperimentRequestBody() {
    return {
        ...faultLabBaseBody(),
        benchmark_type: 'k8s',
        dry_run: document.getElementById('faultlab-dryrun')?.checked !== false,
        background_load: document.getElementById('faultlab-load')?.checked !== false,
        name: document.getElementById('k8s-fault-exp-name')?.value || '',
        business_system: document.getElementById('k8s-fault-exp-system')?.value || 'social-network',
        total_duration_s: parseInt(document.getElementById('k8s-fault-exp-total')?.value || '0', 10) || 0,
        default_wait_after_s: parseInt(document.getElementById('k8s-fault-exp-wait')?.value || '5', 10) || 0,
        steps: _k8sFaultExperimentSteps.map(step => ({
            scenario_id: step.scenario_id,
            action: step.action,
            hold_s: step.hold_s,
            wait_after_s: step.wait_after_s,
        })),
    };
}

async function startK8sFaultExperiment() {
    const out = document.getElementById('k8s-fault-exp-output');
    if (!_k8sFaultExperimentSteps.length) {
        if (out) out.textContent = '请先添加至少一个通算实验步骤。';
        return;
    }
    if (out) out.textContent = '通算连续实验启动中...';
    const res = await api('/api/fault-experiments/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(k8sFaultExperimentRequestBody()),
    });
    if (out) out.textContent = JSON.stringify(res, null, 2);
    await loadK8sFaultExperiments();
}

async function loadK8sFaultExperiments() {
    const data = await api('/api/fault-experiments?limit=20');
    const runs = (data?.experiments || []).filter(run => run.spec?.benchmark_type === 'k8s').slice(0, 10);
    renderFaultExperimentRuns('k8s-fault-exp-runs', 'k8s-fault-exp-output', runs);
}

// ─────────────────────────────────────────
// 智算故障实验
// ─────────────────────────────────────────

let _llmFaultScenarios = [];
let _llmFaultTool = {};
let _faultExperimentSteps = [];

async function loadLLMFaultLab() {
    await loadFaultTargets();
    const data = await api('/api/llm-faultlab/scenarios');
    _llmFaultScenarios = data?.scenarios || [];
    _llmFaultTool = data?.tool || {};
    renderLLMFaultSummary();
    renderLLMFaultEnvironment();
    onLLMFaultModeChange();

    const sel = document.getElementById('llmfaultlab-scenario');
    if (!sel) return;
    sel.innerHTML = _llmFaultScenarios.length
        ? _llmFaultScenarios.map(s => `<option value="${s.id}">${s.id} — ${escapeHtml(s.name || '')}</option>`).join('')
        : '<option value="">未发现 vLLM 故障场景</option>';
    sel.onchange = renderLLMFaultScenarioDetail;
    renderLLMFaultScenarioDetail();
    renderLLMFaultScenarioGrid();
    renderFaultExperimentSteps();
    loadFaultExperiments();
}

function getLLMFaultMode() {
    const r = document.querySelector('input[name="llmfaultlab-mode"]:checked');
    return r ? r.value : 'k8s';
}

function onLLMFaultModeChange() {
    const mode = getLLMFaultMode();
    const k8sRow = document.getElementById('llmfaultlab-k8s-row');
    const hostRow = document.getElementById('llmfaultlab-host-row');
    if (k8sRow) k8sRow.style.display = mode === 'k8s' ? '' : 'none';
    if (hostRow) hostRow.style.display = mode === 'host' ? '' : 'none';
}

async function probeLLMHost() {
    const hostId = document.getElementById('llmfaultlab-host')?.value;
    const hint = document.getElementById('llmfaultlab-host-hint');
    if (!hostId) { if (hint) hint.textContent = '请先选择主机'; return; }
    if (hint) hint.textContent = '探测中...';
    const res = await api(`/api/fault-targets/llm-host/${hostId}/probe`, { method: 'POST' });
    if (!hint) return;
    if (res?.ok) {
        const lines = (res.stdout || '').split('\n').filter(Boolean);
        const summary = lines[0] ? lines[0].substring(0, 80) : '已连接';
        hint.textContent = `✓ ${summary}${lines.length > 1 ? ` (+${lines.length - 1})` : ''}`;
        hint.style.color = '#16a34a';
    } else {
        hint.textContent = `✗ ${(res?.stderr || res?.error || '不可达').substring(0, 100)}`;
        hint.style.color = '#dc2626';
    }
}

function renderLLMFaultSummary() {
    const el = document.getElementById('llmfaultlab-summary');
    if (!el) return;
    const layerText = (_llmFaultTool.layers || []).join(' / ') || '-';
    const statusBadge = _llmFaultTool.available
        ? '<span class="badge badge-success">工具可用</span>'
        : '<span class="badge badge-warning">待部署工具</span>';
    el.innerHTML = `
        <div class="mini-metric"><span>状态</span><strong>${statusBadge}</strong></div>
        <div class="mini-metric"><span>场景数</span><strong>${_llmFaultTool.scenario_count || _llmFaultScenarios.length || 0}</strong></div>
        <div class="mini-metric"><span>覆盖层级</span><strong>${escapeHtml(layerText)}</strong></div>
        <div class="mini-metric"><span>目标</span><strong>vLLM / LLM</strong></div>
    `;
}

function renderLLMFaultEnvironment() {
    const el = document.getElementById('llmfaultlab-env');
    if (!el) return;
    const env = _llmFaultTool.environment || {};
    const jump = env.jump_host || {};
    const targets = env.targets || [];
    el.innerHTML = `
        <div class="fault-env-head">
            <span class="badge badge-info">智算测试环境</span>
            <span class="text-muted">凭据仅运行时使用，不在系统中持久化</span>
        </div>
        <div class="fault-env-grid">
            <div class="fault-env-node">
                <strong>跳板机</strong>
                <span>${escapeHtml(jump.user || '-')}@${escapeHtml(jump.host || '-')}</span>
                <small>${escapeHtml(jump.network || '')}</small>
            </div>
            ${targets.map(t => `<div class="fault-env-node">
                <strong>${escapeHtml(t.name || 'T4')}</strong>
                <span>${escapeHtml(t.user || '-')}@${escapeHtml(t.host || '-')}</span>
                <small>${escapeHtml(t.gpu || '')} · ${escapeHtml(t.role || '')}</small>
            </div>`).join('')}
        </div>
    `;
}

function selectedLLMFaultScenario() {
    const id = document.getElementById('llmfaultlab-scenario')?.value;
    return _llmFaultScenarios.find(s => s.id === id);
}

function llmFaultRequestBody() {
    const mode = getLLMFaultMode();
    const body = {
        dry_run: document.getElementById('llmfaultlab-dryrun')?.checked !== false,
        target_mode: mode,
    };
    if (mode === 'k8s') {
        body.cluster_id = document.getElementById('llmfaultlab-cluster')?.value || 'default';
        body.namespace = document.getElementById('llmfaultlab-namespace')?.value || 'default';
        body.deployment = document.getElementById('llmfaultlab-deployment')?.value || 'vllm-server';
    } else {
        body.host_id = document.getElementById('llmfaultlab-host')?.value || '';
    }
    return body;
}

function renderLLMFaultScenarioDetail() {
    const s = selectedLLMFaultScenario();
    const el = document.getElementById('llmfaultlab-scenario-detail');
    if (!el) return;
    if (!s) {
        el.innerHTML = `<div class="fault-detail">
            <div><span class="badge badge-warning">未加载</span></div>
            <p>未找到 vLLM 故障工具场景文件。请在集群节点设置 VLLM_FAULT_INJECTOR_DIR，或部署 vllm_fault_injector 工具包。</p>
            <pre>${escapeHtml(_llmFaultTool.scenarios_file || '')}</pre>
        </div>`;
        return;
    }
    const symptoms = s.expected_symptoms || [];
    el.innerHTML = `<div class="fault-detail">
        <div>
            <span class="badge badge-info">${escapeHtml(s.layer || '')}</span>
            <span class="badge badge-warning">${escapeHtml(s.fault_type || '')}</span>
            <span class="badge badge-${s.severity === 'critical' ? 'danger' : 'info'}">${escapeHtml(s.severity || '')}</span>
            <strong>${escapeHtml(s.name || s.id)}</strong>
        </div>
        <p>${escapeHtml(s.description || '')}</p>
        <div class="fault-symptoms">
            ${symptoms.map(x => `<span>${escapeHtml(x)}</span>`).join('')}
        </div>
        <pre>${escapeHtml([
            `layer: ${s.layer || '-'}`,
            `fault_type: ${s.fault_type || '-'}`,
            `expected_detection_time_s: ${s.expected_detection_time_s || '-'}`,
            `reference: ${s.reference || '智算推理场景运行时测试工具设计报告'}`
        ].join('\n'))}</pre>
    </div>`;
}

function renderLLMFaultScenarioGrid() {
    const el = document.getElementById('llmfaultlab-grid');
    if (!el) return;
    if (!_llmFaultScenarios.length) {
        el.innerHTML = '<p class="text-muted" style="padding:12px">暂无智算故障场景</p>';
        return;
    }
    el.innerHTML = _llmFaultScenarios.map(s => `<div class="fault-card">
        <div>
            <span class="badge badge-info">${escapeHtml(s.layer || '')}</span>
            <span class="badge badge-${s.severity === 'critical' ? 'danger' : 'warning'}">${escapeHtml(s.severity || '')}</span>
        </div>
        <strong>${escapeHtml(s.name || s.id)}</strong>
        <p>${escapeHtml((s.description || '').substring(0, 140))}</p>
        <p class="text-muted">${escapeHtml(s.fault_type || '')}</p>
    </div>`).join('');
}

async function injectLLMFaultScenario() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const out = document.getElementById('llmfaultlab-output');
    out.textContent = '执行中...';
    const res = await api(`/api/llm-faultlab/inject/${s.id}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(llmFaultRequestBody()),
    });
    out.textContent = JSON.stringify(res, null, 2);
}

async function runLLMFaultExperiment() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const out = document.getElementById('llmfaultlab-output');
    out.textContent = '实验执行中...';
    const res = await api(`/api/llm-faultlab/experiment/${s.id}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            ...llmFaultRequestBody(),
            baseline_samples: 2,
            fault_samples: 3,
            sample_interval: 3,
        }),
    });
    out.textContent = JSON.stringify(res, null, 2);
}

async function cleanupLLMFaultScenario() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    const out = document.getElementById('llmfaultlab-output');
    out.textContent = '恢复中...';
    const res = await api(`/api/llm-faultlab/cleanup/${s.id}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(llmFaultRequestBody()),
    });
    out.textContent = JSON.stringify(res, null, 2);
}

function addFaultExperimentStep() {
    const s = selectedLLMFaultScenario();
    if (!s) return;
    _faultExperimentSteps.push({
        scenario_id: s.id,
        name: s.name || s.id,
        fault_type: s.fault_type || '',
        layer: s.layer || '',
        action: 'experiment',
        hold_s: 30,
        wait_after_s: parseInt(document.getElementById('fault-exp-wait')?.value || '5', 10) || 0,
    });
    renderFaultExperimentSteps();
}

function removeFaultExperimentStep(index) {
    _faultExperimentSteps.splice(index, 1);
    renderFaultExperimentSteps();
}

function updateFaultExperimentStep(index, field, value) {
    if (!_faultExperimentSteps[index]) return;
    if (field === 'hold_s' || field === 'wait_after_s') {
        _faultExperimentSteps[index][field] = Math.max(0, parseInt(value || '0', 10) || 0);
    } else {
        _faultExperimentSteps[index][field] = value;
    }
}

function renderFaultExperimentSteps() {
    const el = document.getElementById('fault-exp-steps');
    if (!el) return;
    if (!_faultExperimentSteps.length) {
        el.innerHTML = '<p class="text-muted" style="font-size:13px">尚未添加连续实验步骤。选择一个智算故障场景后点击“添加当前场景”。</p>';
        return;
    }
    el.innerHTML = _faultExperimentSteps.map((step, i) => `
        <div class="fault-exp-step">
            <div class="fault-exp-step-main">
                <span class="badge badge-info">#${i + 1}</span>
                <strong>${escapeHtml(step.scenario_id)}</strong>
                <span class="text-muted">${escapeHtml(step.name || '')}</span>
                <span class="badge badge-warning">${escapeHtml(step.fault_type || '')}</span>
            </div>
            <select class="select-sm" onchange="updateFaultExperimentStep(${i}, 'action', this.value)">
                <option value="experiment" ${step.action === 'experiment' ? 'selected' : ''}>baseline/inject/measure/recover</option>
                <option value="inject" ${step.action === 'inject' ? 'selected' : ''}>inject/hold/recover</option>
            </select>
            <input class="input-sm" type="number" min="0" value="${step.hold_s}" title="hold seconds"
                onchange="updateFaultExperimentStep(${i}, 'hold_s', this.value)">
            <input class="input-sm" type="number" min="0" value="${step.wait_after_s}" title="wait after seconds"
                onchange="updateFaultExperimentStep(${i}, 'wait_after_s', this.value)">
            <button class="btn btn-sm btn-danger" onclick="removeFaultExperimentStep(${i})">移除</button>
        </div>
    `).join('');
}

function faultExperimentRequestBody() {
    return {
        ...llmFaultRequestBody(),
        benchmark_type: 'llm',
        name: document.getElementById('fault-exp-name')?.value || '',
        business_system: document.getElementById('fault-exp-system')?.value || 'llm-inference',
        total_duration_s: parseInt(document.getElementById('fault-exp-total')?.value || '0', 10) || 0,
        default_wait_after_s: parseInt(document.getElementById('fault-exp-wait')?.value || '5', 10) || 0,
        steps: _faultExperimentSteps.map(step => ({
            scenario_id: step.scenario_id,
            action: step.action,
            hold_s: step.hold_s,
            wait_after_s: step.wait_after_s,
        })),
    };
}

async function startFaultExperiment() {
    const out = document.getElementById('fault-exp-output');
    if (!_faultExperimentSteps.length) {
        if (out) out.textContent = '请先添加至少一个实验步骤。';
        return;
    }
    if (out) out.textContent = '连续实验启动中...';
    const res = await api('/api/fault-experiments/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(faultExperimentRequestBody()),
    });
    if (out) out.textContent = JSON.stringify(res, null, 2);
    await loadFaultExperiments();
}

async function cancelFaultExperiment(runId, outputId = 'fault-exp-output') {
    const out = document.getElementById(outputId);
    const res = await api(`/api/fault-experiments/${runId}/cancel`, { method: 'POST' });
    if (out) out.textContent = JSON.stringify(res, null, 2);
    await loadFaultExperiments();
    await loadK8sFaultExperiments();
}

async function showFaultExperiment(runId, outputId = 'fault-exp-output') {
    const out = document.getElementById(outputId);
    const res = await api(`/api/fault-experiments/${runId}`);
    if (out) out.textContent = JSON.stringify(res, null, 2);
}

async function loadFaultExperiments() {
    const data = await api('/api/fault-experiments?limit=10');
    const runs = (data?.experiments || []).filter(run => (run.spec?.benchmark_type || 'llm') === 'llm');
    renderFaultExperimentRuns('fault-exp-runs', 'fault-exp-output', runs);
}

function renderFaultExperimentRuns(containerId, outputId, runs) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!runs.length) {
        el.innerHTML = '<p class="text-muted" style="font-size:13px;margin-top:10px">暂无连续实验记录</p>';
        return;
    }
    el.innerHTML = runs.map(run => {
        const status = run.status || 'unknown';
        const cls = status === 'completed' ? 'success' : status === 'failed' ? 'danger' : status === 'running' ? 'warning' : 'info';
        const steps = run.spec?.steps?.length || 0;
        return `<div class="fault-exp-run">
            <div>
                <span class="badge badge-${cls}">${escapeHtml(status)}</span>
                <strong>${escapeHtml(run.name || run.id)}</strong>
                <span class="text-muted">${steps} steps · ${formatTime((run.started_at || run.created_at) * 1000)}</span>
            </div>
            <div>
                <button class="btn btn-sm" onclick="showFaultExperiment('${escapeHtml(run.id)}', '${escapeHtml(outputId)}')">查看</button>
                ${status === 'running' || status === 'queued' ? `<button class="btn btn-sm btn-danger" onclick="cancelFaultExperiment('${escapeHtml(run.id)}', '${escapeHtml(outputId)}')">取消</button>` : ''}
            </div>
        </div>`;
    }).join('');
}

// ─────────────────────────────────────────
// 故障目标管理 Modal
// ─────────────────────────────────────────

function openFaultTargetManager() {
    const modal = document.getElementById('fault-target-modal');
    if (!modal) return;
    modal.style.display = 'flex';
    renderFaultTargetTables();
    switchTargetTab('k8s');
}

function closeFaultTargetManager() {
    const modal = document.getElementById('fault-target-modal');
    if (modal) modal.style.display = 'none';
}

function switchTargetTab(tab) {
    const k8sPane = document.getElementById('tgt-pane-k8s');
    const hostPane = document.getElementById('tgt-pane-host');
    const k8sBtn = document.getElementById('tgt-tab-k8s');
    const hostBtn = document.getElementById('tgt-tab-host');
    if (tab === 'k8s') {
        if (k8sPane) k8sPane.style.display = '';
        if (hostPane) hostPane.style.display = 'none';
        if (k8sBtn) k8sBtn.classList.add('btn-primary');
        if (hostBtn) hostBtn.classList.remove('btn-primary');
    } else {
        if (k8sPane) k8sPane.style.display = 'none';
        if (hostPane) hostPane.style.display = '';
        if (k8sBtn) k8sBtn.classList.remove('btn-primary');
        if (hostBtn) hostBtn.classList.add('btn-primary');
    }
}

function renderFaultTargetTables() {
    const k8sBody = document.getElementById('tgt-k8s-body');
    if (k8sBody) {
        k8sBody.innerHTML = _faultTargets.k8s_clusters.map((c, i) => clusterRowHtml(c, i)).join('') ||
            '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:16px">尚无集群，点击下方"新增"</td></tr>';
    }
    const hostBody = document.getElementById('tgt-host-body');
    if (hostBody) {
        hostBody.innerHTML = _faultTargets.llm_hosts.map((h, i) => hostRowHtml(h, i)).join('') ||
            '<tr><td colspan="9" class="text-muted" style="text-align:center;padding:16px">尚无主机，点击下方"新增"</td></tr>';
    }
}

function clusterRowHtml(c, i) {
    return `<tr data-idx="${i}">
        <td><input class="input-sm tgt-k8s-id" value="${escapeHtml(c.id || '')}" style="width:100px"></td>
        <td><input class="input-sm tgt-k8s-name" value="${escapeHtml(c.name || '')}" style="width:140px"></td>
        <td><input class="input-sm tgt-k8s-kubeconfig" value="${escapeHtml(c.kubeconfig || '')}" placeholder="留空=容器默认" style="width:160px"></td>
        <td><input class="input-sm tgt-k8s-context" value="${escapeHtml(c.context || '')}" style="width:100px"></td>
        <td><input class="input-sm tgt-k8s-ns" value="${escapeHtml(c.default_namespace || 'default')}" style="width:100px"></td>
        <td><input class="input-sm tgt-k8s-desc" value="${escapeHtml(c.description || '')}" style="width:160px"></td>
        <td><button class="btn btn-sm btn-danger" onclick="removeClusterRow(${i})">删除</button></td>
    </tr>`;
}

function hostRowHtml(h, i) {
    return `<tr data-idx="${i}">
        <td><input class="input-sm tgt-h-id" value="${escapeHtml(h.id || '')}" style="width:90px"></td>
        <td><input class="input-sm tgt-h-name" value="${escapeHtml(h.name || '')}" style="width:100px"></td>
        <td><input class="input-sm tgt-h-host" value="${escapeHtml(h.host || '')}" style="width:120px"></td>
        <td><input class="input-sm tgt-h-user" value="${escapeHtml(h.ssh_user || 'root')}" style="width:80px"></td>
        <td><input class="input-sm tgt-h-key" value="${escapeHtml(h.ssh_key_path || '')}" style="width:140px"></td>
        <td><input class="input-sm tgt-h-jump" value="${escapeHtml(h.jump_host || '')}" style="width:140px"></td>
        <td><input class="input-sm tgt-h-gpu" value="${escapeHtml(h.gpu || '')}" style="width:110px"></td>
        <td><input class="input-sm tgt-h-role" value="${escapeHtml(h.role || '')}" style="width:120px"></td>
        <td><button class="btn btn-sm btn-danger" onclick="removeHostRow(${i})">删除</button></td>
    </tr>`;
}

function collectFromTables() {
    const k8s = [];
    document.querySelectorAll('#tgt-k8s-body tr[data-idx]').forEach(tr => {
        k8s.push({
            id: tr.querySelector('.tgt-k8s-id')?.value?.trim() || '',
            name: tr.querySelector('.tgt-k8s-name')?.value?.trim() || '',
            kubeconfig: tr.querySelector('.tgt-k8s-kubeconfig')?.value?.trim() || '',
            context: tr.querySelector('.tgt-k8s-context')?.value?.trim() || '',
            default_namespace: tr.querySelector('.tgt-k8s-ns')?.value?.trim() || 'default',
            description: tr.querySelector('.tgt-k8s-desc')?.value?.trim() || '',
        });
    });
    const hosts = [];
    document.querySelectorAll('#tgt-host-body tr[data-idx]').forEach(tr => {
        hosts.push({
            id: tr.querySelector('.tgt-h-id')?.value?.trim() || '',
            name: tr.querySelector('.tgt-h-name')?.value?.trim() || '',
            host: tr.querySelector('.tgt-h-host')?.value?.trim() || '',
            ssh_user: tr.querySelector('.tgt-h-user')?.value?.trim() || 'root',
            ssh_key_path: tr.querySelector('.tgt-h-key')?.value?.trim() || '',
            jump_host: tr.querySelector('.tgt-h-jump')?.value?.trim() || '',
            gpu: tr.querySelector('.tgt-h-gpu')?.value?.trim() || '',
            role: tr.querySelector('.tgt-h-role')?.value?.trim() || '',
        });
    });
    return { k8s_clusters: k8s, llm_hosts: hosts };
}

function addClusterRow() {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.k8s_clusters.push({ id: '', name: '', kubeconfig: '', context: '', default_namespace: 'default', description: '' });
    renderFaultTargetTables();
}

function removeClusterRow(idx) {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.k8s_clusters.splice(idx, 1);
    renderFaultTargetTables();
}

function addHostRow() {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.llm_hosts.push({ id: '', name: '', host: '', ssh_user: 'root', ssh_key_path: '', jump_host: '', gpu: '', role: '' });
    renderFaultTargetTables();
    switchTargetTab('host');
}

function removeHostRow(idx) {
    _faultTargets = { ..._faultTargets, ...collectFromTables() };
    _faultTargets.llm_hosts.splice(idx, 1);
    renderFaultTargetTables();
    switchTargetTab('host');
}

async function saveFaultTargets() {
    const payload = collectFromTables();
    // Basic validation
    for (const c of payload.k8s_clusters) {
        if (!c.id || !c.name) { showToast('集群 id/名称 不能为空', 'error'); return; }
    }
    for (const h of payload.llm_hosts) {
        if (!h.id || !h.name || !h.host) { showToast('主机 id/名称/host 不能为空', 'error'); return; }
    }
    const res = await api('/api/fault-targets', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (res?.status === 'ok' || res?.ok || (res && !res.error && !res.detail)) {
        _faultTargets = payload;
        populateClusterDropdown('faultlab-cluster');
        populateClusterDropdown('llmfaultlab-cluster');
        populateHostDropdown('llmfaultlab-host');
        showToast('故障目标已保存');
        closeFaultTargetManager();
    } else {
        showToast('保存失败：' + (res?.error || '未知错误'), 'error');
    }
}

function renderRulesTable(rules) {
    const tbody = document.getElementById('kb-rules-body');
    if (!rules.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:20px">暂无诊断规则，点击"添加规则"创建</td></tr>';
        return;
    }
    tbody.innerHTML = rules.map(r => {
        const srcBadge = r.source === 'auto_learn' ? '<span class="badge badge-info">自动学习</span>'
            : r.source === 'supervised' ? '<span class="badge badge-success">监督学习</span>'
            : '<span class="badge badge-gray">手动</span>';
        const name = r.name || r.condition?.substring(0, 20) || r.rule_id || '';
        return `<tr>
            <td style="font-weight:600;min-width:100px">${escapeHtml(name)}</td>
            <td style="max-width:250px">${escapeHtml(r.condition || '')}</td>
            <td style="max-width:250px">${escapeHtml(r.conclusion || '')}</td>
            <td><span class="badge badge-info">${escapeHtml(r.fault_type || '-')}</span></td>
            <td>${r.confidence || '-'}</td>
            <td>${srcBadge}</td>
            <td style="white-space:nowrap">
                <button class="btn btn-sm" onclick="editRule('${escapeHtml(r.rule_id||'')}')">编辑</button>
                <button class="btn btn-sm btn-danger" onclick="deleteRule('${escapeHtml(r.rule_id||'')}')">删除</button>
            </td>
        </tr>`;
    }).join('');
}

// ── Rule form helpers ──

let _allRules = [];

function showAddRuleForm() {
    document.getElementById('rule-form').style.display = 'block';
    document.getElementById('rule-form-title').textContent = '添加诊断规则';
    document.getElementById('rule-edit-id').value = '';
    document.getElementById('rule-f-name').value = '';
    document.getElementById('rule-f-condition').value = '';
    document.getElementById('rule-f-conclusion').value = '';
    document.getElementById('rule-f-type').value = '资源不足';
    document.getElementById('rule-f-namespace').value = 'general';
    document.getElementById('rule-f-confidence').value = '0.8';
}

function hideRuleForm() {
    document.getElementById('rule-form').style.display = 'none';
}

function editRule(ruleId) {
    const rule = _allRules.find(r => r.rule_id === ruleId);
    if (!rule) return;
    document.getElementById('rule-form').style.display = 'block';
    document.getElementById('rule-form-title').textContent = '编辑诊断规则';
    document.getElementById('rule-edit-id').value = ruleId;
    document.getElementById('rule-f-name').value = rule.name || '';
    document.getElementById('rule-f-condition').value = rule.condition || '';
    document.getElementById('rule-f-conclusion').value = rule.conclusion || '';
    document.getElementById('rule-f-type').value = rule.fault_type || '其他';
    document.getElementById('rule-f-namespace').value = rule.namespace || 'general';
    document.getElementById('rule-f-confidence').value = rule.confidence || 0.8;
    document.getElementById('rule-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function saveRule() {
    const editId = document.getElementById('rule-edit-id').value;
    const payload = {
        name: document.getElementById('rule-f-name').value.trim(),
        condition: document.getElementById('rule-f-condition').value.trim(),
        conclusion: document.getElementById('rule-f-conclusion').value.trim(),
        fault_type: document.getElementById('rule-f-type').value,
        namespace: document.getElementById('rule-f-namespace').value.trim() || 'general',
        confidence: parseFloat(document.getElementById('rule-f-confidence').value) || 0.8,
    };
    if (!payload.condition || !payload.conclusion) { alert('请填写条件和结论'); return; }
    if (!payload.name) payload.name = payload.condition.substring(0, 30);

    // If editing, delete old then create new
    if (editId) {
        await api(`/api/knowledge/rules/${editId}`, { method: 'DELETE' });
    }
    const result = await api('/api/knowledge/rules', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (result?.status === 'ok') {
        showToast(editId ? '规则已更新' : '规则添加成功');
        hideRuleForm();
        loadKnowledge();
    } else {
        showToast('保存失败', 'error');
    }
}

function renderFaultsTable(faults) {
    const tbody = document.getElementById('kb-faults-body');
    if (!faults.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-muted" style="text-align:center;padding:20px">暂无故障记录</td></tr>';
        return;
    }
    tbody.innerHTML = faults.map(f => {
        const ts = f.timestamp ? formatTime(f.timestamp * 1000) : '-';
        return `<tr>
            <td style="font-size:11px;font-family:var(--font-mono)">${escapeHtml((f.fault_id||'').substring(0,12))}</td>
            <td>${escapeHtml((f.description||'').substring(0,80))}</td>
            <td>${escapeHtml((f.root_cause||'').substring(0,80))}</td>
            <td>${escapeHtml(f.fault_type || '-')}</td>
            <td style="font-size:12px">${ts}</td>
            <td><button class="btn btn-sm btn-danger" onclick="deleteFault('${escapeHtml(f.fault_id||'')}')">删除</button></td>
        </tr>`;
    }).join('');
}

function renderFeedbackList(feedback) {
    const container = document.getElementById('kb-feedback-list');
    if (!feedback.length) { container.innerHTML = '<p class="text-muted">暂无反馈记录</p>'; return; }
    container.innerHTML = feedback.map(f => {
        const statusBadge = f.learning_status === 'success'
            ? `<span class="badge badge-success">学习成功 (${f.rules_generated}条规则)</span>`
            : f.learning_status === 'no_learner'
            ? '<span class="badge badge-gray">未触发学习</span>'
            : `<span class="badge badge-warning">${escapeHtml(f.learning_status||'')}</span>`;
        return `<div class="signal-item">
            <span><span class="badge badge-info">${escapeHtml(f.incident_id||'')}</span> ${escapeHtml((f.expert_diagnosis||'').substring(0,100))} ${statusBadge}</span>
            <span class="text-muted">${formatTime(f.timestamp ? f.timestamp * 1000 : null)}</span>
        </div>`;
    }).join('');
}

async function deleteRule(ruleId) {
    if (!confirm('确认删除此规则？')) return;
    await api(`/api/knowledge/rules/${ruleId}`, { method: 'DELETE' });
    showToast('规则已删除');
    loadKnowledge();
}

async function addFault() {
    const description = prompt('故障描述:');
    if (!description) return;
    const rootCause = prompt('根因:');
    const faultType = prompt('故障类型:', '');
    const resolution = prompt('解决方案:', '');

    const result = await api('/api/knowledge/faults', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description, root_cause: rootCause || '', fault_type: faultType || '', resolution: resolution || '' }),
    });
    if (result?.status === 'ok') { showToast('故障记录添加成功'); loadKnowledge(); }
    else showToast('添加失败', 'error');
}

async function deleteFault(faultId) {
    if (!confirm('确认删除此故障记录？')) return;
    await api(`/api/knowledge/faults/${faultId}`, { method: 'DELETE' });
    showToast('故障记录已删除');
    loadKnowledge();
}

async function searchKnowledge() {
    const q = document.getElementById('kb-search-input')?.value?.trim();
    const container = document.getElementById('kb-search-results');
    if (!q) { container.innerHTML = ''; return; }

    container.innerHTML = '<div class="loading">搜索中</div>';
    const data = await api(`/api/knowledge/search?q=${encodeURIComponent(q)}`);
    if (!data) { container.innerHTML = '<p class="text-danger">搜索失败</p>'; return; }

    let html = '';
    if (data.rules?.length) {
        html += '<h3 style="margin:12px 0 8px">匹配的诊断规则</h3>';
        data.rules.forEach(r => {
            html += `<div class="signal-item"><span><span class="badge badge-info">规则</span> <strong>${escapeHtml(r.condition||r.text||'')}</strong> → ${escapeHtml(r.conclusion||'')}</span></div>`;
        });
    }
    if (data.faults?.length) {
        html += '<h3 style="margin:12px 0 8px">匹配的故障记录</h3>';
        data.faults.forEach(f => {
            html += `<div class="signal-item"><span><span class="badge badge-warning">故障</span> ${escapeHtml(f.description||f.text||'')} — <strong>${escapeHtml(f.root_cause||'')}</strong></span></div>`;
        });
    }
    if (!data.rules?.length && !data.faults?.length) {
        html = '<p class="text-muted" style="padding:12px">未找到匹配结果</p>';
    }
    container.innerHTML = html;
}

async function loadFeedbackIncidents() {
    const sel = document.getElementById('fb-incident-select');
    if (!sel) return;
    const data = await api('/api/rca/history?limit=20');
    sel.innerHTML = '<option value="">— 选择最近 RCA 分析 —</option>';
    (data?.runs || []).forEach(r => {
        const sid = r.session_id || r.id;
        const ts = r.started_at ? new Date(r.started_at * 1000).toLocaleString() : '';
        const q = (r.query || '').slice(0, 50);
        const opt = document.createElement('option');
        opt.value = sid;
        opt.textContent = `${sid} · ${ts} · ${q}`;
        sel.appendChild(opt);
    });
}

function onIncidentSelectChange() {
    const sel = document.getElementById('fb-incident-select');
    const input = document.getElementById('fb-incident-id');
    if (sel && input && sel.value) input.value = sel.value;
}

async function submitFeedback() {
    const incidentId = document.getElementById('fb-incident-id')?.value?.trim();
    const diagnosis = document.getElementById('fb-diagnosis')?.value?.trim();
    const comment = document.getElementById('fb-comment')?.value?.trim();
    const resultEl = document.getElementById('fb-result');

    if (!incidentId || !diagnosis) { alert('请选择或填写分析ID，并填写专家诊断结论'); return; }

    resultEl.innerHTML = '<div class="loading">提交中，正在触发监督学习</div>';
    const result = await api('/api/knowledge/feedback', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ incident_id: incidentId, expert_diagnosis: diagnosis, comment: comment || '' }),
    });

    if (result) {
        const parts = [];
        if (result.learning_status === 'success') {
            parts.push(`学习成功，生成了 ${result.rules_generated} 条新规则`);
            if (typeof result.rules_voted === 'number' && result.rules_voted > 0) {
                const verdict = result.correct ? '正向' : '负向';
                parts.push(`对 ${result.rules_voted} 条召回规则投了 ${verdict} 票`);
            } else if (result.rules_voted === 0) {
                parts.push('本次 RCA 没有召回历史规则，故未触发规则投票');
            }
        } else if (result.learning_status === 'no_learner') {
            parts.push('反馈已记录（LLM 未配置，未触发学习）');
        } else {
            parts.push(`学习状态: ${result.learning_status}`);
        }
        resultEl.innerHTML = `<div class="result-banner success" style="margin-top:12px"><h4>反馈已提交</h4><p>${parts.join('；')}</p></div>`;
        showToast('专家反馈已提交');
        loadKnowledge();
    } else {
        resultEl.innerHTML = '<p class="text-danger">提交失败</p>';
    }
}

// ─────────────────────────────────────────
// Reports (故障报告)
// ─────────────────────────────────────────

let _selectedReportRunId = null;

async function loadReportList() {
    const data = await api('/api/rca/history?limit=50');
    const container = document.getElementById('report-list');
    if (!data?.runs?.length) {
        container.innerHTML = '<p class="text-muted" style="padding:12px">暂无历史分析记录。请先在"根因分析"页面运行一次分析。</p>';
        return;
    }

    container.innerHTML = data.runs.map(r => {
        const statusBadge = r.status === 'completed' ? 'badge-success' : r.status === 'running' ? 'badge-warning' : 'badge-danger';
        return `<div class="signal-item" style="cursor:pointer" onclick="generateReport('${r.id}')">
            <span>
                <span class="badge ${statusBadge}">${r.status}</span>
                <strong>${escapeHtml(r.id)}</strong> — ${escapeHtml((r.query||'').substring(0,80))}
            </span>
            <span class="text-muted">${formatTime(r.started_at ? r.started_at * 1000 : null)}</span>
        </div>`;
    }).join('');
}

async function generateReport(runId) {
    _selectedReportRunId = runId;
    const card = document.getElementById('report-preview-card');
    const preview = document.getElementById('report-preview');
    card.style.display = 'block';
    preview.innerHTML = '<div class="loading">生成报告中</div>';

    const report = await api(`/api/report/${runId}`);
    if (!report) { preview.innerHTML = '<p class="text-danger">报告生成失败</p>'; return; }

    const bi = report.basic_info || {};
    const diag = report.diagnosis || {};
    const rem = report.remediation || {};
    const qual = report.quality || {};
    const sugg = report.suggestions || {};

    let html = `<div style="border-bottom:2px solid var(--accent);padding-bottom:12px;margin-bottom:16px">
        <h3 style="color:var(--accent);font-size:18px;margin-bottom:4px">故障诊断报告</h3>
        <span class="text-muted">${escapeHtml(bi['报告编号']||'')} | ${escapeHtml(bi['开始时间']||'')}</span>
    </div>`;

    // 基本信息
    html += '<h3 style="margin-bottom:8px">一、基本信息</h3><div class="rca-meta-grid">';
    for (const [k, v] of Object.entries(bi)) {
        html += `<div class="rca-meta-item"><div class="meta-label">${escapeHtml(k)}</div><div class="meta-value">${escapeHtml(String(v))}</div></div>`;
    }
    html += '</div>';

    // 假设列表
    html += '<h3 style="margin:16px 0 8px">二、诊断假设</h3>';
    if (diag['假设列表']?.length) {
        html += diag['假设列表'].map((h,i) => `<div class="hyp-item"><span class="hyp-rank">#${h['排名']}</span><div class="hyp-bar-wrap"><div class="hyp-bar" style="width:${parseInt(h['置信度'])}%"></div></div><span class="hyp-conf">${h['置信度']}</span><span class="hyp-desc">${escapeHtml(h['描述'])}</span></div>`).join('');
    } else { html += '<p class="text-muted">无假设数据</p>'; }

    // 证据收集
    html += '<h3 style="margin:16px 0 8px">三、证据收集</h3>';
    if (diag['证据收集']?.length) {
        html += '<div class="evidence-grid">';
        diag['证据收集'].forEach(e => {
            html += `<div class="evidence-card ${e['成功'] ? 'success' : 'error'}"><div class="ev-agent">${e['成功'] ? '&#10003;' : '&#9888;'} ${escapeHtml(e['智能体'])}</div><div class="ev-summary">${escapeHtml(e['结果'])}</div></div>`;
        });
        html += '</div>';
    } else { html += '<p class="text-muted">无证据数据</p>'; }

    // 根因结论
    html += '<h3 style="margin:16px 0 8px">四、根因结论</h3>';
    html += `<div class="root-cause" style="background:var(--accent-bg);border-left:4px solid var(--accent);padding:14px;border-radius:0 6px 6px 0;font-size:15px;font-weight:600">${escapeHtml(diag['根因结论']||'N/A')}</div>`;
    html += `<p style="margin:8px 0;font-size:13px"><strong>置信度:</strong> ${diag['置信度']||'-'} &nbsp; <strong>故障类型:</strong> ${escapeHtml(diag['故障类型']||'-')} &nbsp; <strong>受影响服务:</strong> ${escapeHtml((diag['受影响服务']||[]).join(', ')||'-')}</p>`;

    // 自愈
    html += '<h3 style="margin:16px 0 8px">五、自愈修复</h3>';
    html += `<p><strong>状态:</strong> ${escapeHtml(rem['状态']||'未触发')}</p>`;

    // 质量评估
    if (qual['评级']) {
        html += '<h3 style="margin:16px 0 8px">六、质量评估</h3>';
        html += `<p><span class="badge badge-info">${escapeHtml(qual['评级'])}</span> 评分: ${typeof qual['评分'] === 'number' ? qual['评分'].toFixed(3) : '-'}</p>`;
    }

    // 建议
    if (sugg['修复建议'] || sugg['预防措施']) {
        html += '<h3 style="margin:16px 0 8px">七、建议</h3>';
        if (sugg['修复建议']) html += `<div class="rca-remediation"><strong>修复建议:</strong> ${escapeHtml(sugg['修复建议'])}</div>`;
        if (sugg['预防措施']) html += `<div class="rca-remediation" style="margin-top:8px"><strong>预防措施:</strong> ${escapeHtml(sugg['预防措施'])}</div>`;
    }

    preview.innerHTML = html;
}

function exportReport() {
    if (!_selectedReportRunId) { alert('请先选择一次分析记录'); return; }
    window.open(`/api/report/${_selectedReportRunId}/export`, '_blank');
}

function exportReportWord() {
    if (!_selectedReportRunId) { alert('请先选择一次分析记录'); return; }
    window.open(`/api/report/${_selectedReportRunId}/word`, '_blank');
}

// ─────────────────────────────────────────
// Hermes Agent Chat
// ─────────────────────────────────────────

let _hermesSessionId = '';
let _hermesPolling = null;

function hermesNewSession() {
    _hermesSessionId = '';
    const msgs = document.getElementById('hermes-messages');
    msgs.innerHTML = `<div class="hermes-welcome" style="text-align:center;color:#64748b;padding:40px 20px">
        <div style="font-size:48px;margin-bottom:12px">&#9742;</div>
        <h3 style="color:#334155;margin-bottom:8px">Hermes Agent 智能运维助手</h3>
        <p style="font-size:14px">输入运维问题开始对话</p>
    </div>`;
    document.getElementById('hermes-status').textContent = '就绪';
    document.getElementById('hermes-status').className = 'badge badge-info';
    document.getElementById('hermes-tokens').textContent = 'Token: 0';
}

function hermesSendInput() {
    const input = document.getElementById('hermes-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    hermesSend(msg);
}

async function hermesSend(message) {
    // Remove welcome if first message
    const welcome = document.querySelector('.hermes-welcome');
    if (welcome) welcome.remove();

    // Add user message
    _hermesAddMessage('user', message);

    // Update status
    const statusEl = document.getElementById('hermes-status');
    statusEl.textContent = '思考中...';
    statusEl.className = 'badge badge-warning';
    document.getElementById('hermes-send-btn').disabled = true;

    // Send to API
    try {
        const resp = await fetch('/api/hermes/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message, session_id: _hermesSessionId}),
        });
        const data = await resp.json();
        if (data.session_id) _hermesSessionId = data.session_id;

        // Poll for response via SSE
        _hermesStartStream(data.session_id);
    } catch (e) {
        _hermesAddMessage('assistant', `Error: ${e.message}`);
        statusEl.textContent = '错误';
        statusEl.className = 'badge badge-danger';
        document.getElementById('hermes-send-btn').disabled = false;
    }
}

function _hermesStartStream(sessionId) {
    const es = new EventSource(`/api/hermes/chat/${sessionId}/stream`);
    es.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'done') {
                es.close();
                const statusEl = document.getElementById('hermes-status');
                statusEl.textContent = data.status === 'idle' ? '就绪' : '错误';
                statusEl.className = data.status === 'idle' ? 'badge badge-info' : 'badge badge-danger';
                document.getElementById('hermes-send-btn').disabled = false;
                // Update token count
                _hermesUpdateTokens(sessionId);
                return;
            }
            if (data.role === 'assistant') {
                _hermesAddMessage('assistant', data.content, data.tool_calls);
            }
        } catch(e) {}
    };
    es.onerror = () => {
        es.close();
        document.getElementById('hermes-send-btn').disabled = false;
        // Try fetching final state
        setTimeout(() => _hermesUpdateTokens(sessionId), 1000);
    };
}

async function _hermesUpdateTokens(sessionId) {
    try {
        const data = await api(`/api/hermes/chat/${sessionId}`);
        if (data) {
            document.getElementById('hermes-tokens').textContent =
                `Token: ${(data.total_tokens || 0).toLocaleString()} | 工具: ${data.tool_calls_count || 0}`;
        }
    } catch(e) {}
}

function _hermesAddMessage(role, content, toolCalls) {
    const msgs = document.getElementById('hermes-messages');
    const div = document.createElement('div');
    div.style.cssText = role === 'user'
        ? 'align-self:flex-end;background:#1e6fd9;color:white;padding:10px 16px;border-radius:16px 16px 4px 16px;max-width:75%;font-size:14px'
        : 'align-self:flex-start;background:#f1f5f9;color:#1a2b42;padding:12px 16px;border-radius:16px 16px 16px 4px;max-width:85%;font-size:14px';

    // Format content with code blocks
    let html = _hermesFormatContent(content);

    // Show tool calls
    if (toolCalls && toolCalls.length > 0) {
        html += '<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(0,0,0,0.1);font-size:12px;color:#64748b">';
        html += `<div style="font-weight:600;margin-bottom:4px">工具调用 (${toolCalls.length}):</div>`;
        for (const tc of toolCalls) {
            html += `<div style="background:rgba(0,0,0,0.05);padding:4px 8px;border-radius:4px;margin:2px 0;font-family:monospace;font-size:11px">`;
            html += `<span style="color:#1e6fd9">${tc.tool}</span>`;
            if (tc.args) html += ` <span style="color:#94a3b8">${tc.args.substring(0, 100)}</span>`;
            html += '</div>';
        }
        html += '</div>';
    }

    div.innerHTML = html;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
}

function _hermesFormatContent(text) {
    if (!text) return '<span style="color:#94a3b8">（无响应）</span>';
    // Convert markdown code blocks
    text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
        `<pre style="background:rgba(0,0,0,0.06);padding:8px 12px;border-radius:6px;margin:6px 0;overflow-x:auto;font-size:12px;font-family:monospace">${code.replace(/</g,'&lt;')}</pre>`
    );
    // Inline code
    text = text.replace(/`([^`]+)`/g, '<code style="background:rgba(0,0,0,0.06);padding:1px 4px;border-radius:3px;font-size:12px">$1</code>');
    // Bold
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Line breaks
    text = text.replace(/\n/g, '<br>');
    return text;
}


// ─────────────────────────────────────────
// Health Check
// ─────────────────────────────────────────

async function healthCheck() {
    const data = await api('/api/health');
    const dot = document.querySelector('#health-dot .dot');
    const text = document.querySelector('#health-dot span:last-child');
    const badge = document.getElementById('cluster-badge');
    const llmWarning = document.getElementById('llm-warning');

    if (data?.status === 'ok') {
        dot.className = 'dot dot-green'; text.textContent = '系统正常';
        badge.className = 'badge badge-success'; badge.textContent = '已连接';
        if (llmWarning) llmWarning.style.display = data.llm_configured ? 'none' : 'flex';
    } else {
        dot.className = 'dot dot-red'; text.textContent = '连接异常';
        badge.className = 'badge badge-danger'; badge.textContent = '连接异常';
    }
}

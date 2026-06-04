#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# PT-002: 多智能体协作范式与故障诊断能力测试
# 用6种范式 (chain/react/reflection/plan_and_execute/debate/voting)
# 对同一故障进行诊断, 验证多模态数据融合与根因定位
# ═══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="social-network"
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --namespace) NAMESPACE="$2"; shift 2 ;;
        --output)    OUTPUT="$2"; shift 2 ;;
        *)           shift ;;
    esac
done

cd "$PROJECT_DIR"
OUTPUT="${OUTPUT:-测试/test_results/pt002_$(date +%Y%m%d_%H%M%S).json}"
mkdir -p "$(dirname "$OUTPUT")"

log() { echo "[PT-002][$(date +%H:%M:%S)] $*"; }

PARADIGMS=("chain" "react" "reflection" "plan_and_execute" "debate" "voting")
FAULT_QUERY="nginx-thrift request timeout with high latency in $NAMESPACE namespace, response time increased from 50ms to 2000ms, multiple 504 errors in access logs"
RESULTS_DIR="$(dirname "$OUTPUT")/pt002_paradigms"
mkdir -p "$RESULTS_DIR"

log "===== 前置检查 ====="
log "列出可用范式:"
python3 main.py paradigm list
log ""

log "===== 步骤1: 注入故障 (CPU 限制导致超时) ====="
kubectl run pt002-cpu-stress \
    --image=containerstack/alpine-stress \
    --restart=Never \
    -n "$NAMESPACE" \
    -- stress --cpu 4 --timeout 300 \
    2>/dev/null || log "Pod可能已存在, 继续..."

sleep 20
log "故障已注入, 等待可观测数据积累..."
log ""

log "===== 步骤2: 依次执行6种范式 ====="
declare -A PARADIGM_TIMES
declare -A PARADIGM_STATUS

for paradigm in "${PARADIGMS[@]}"; do
    log "--- 执行范式: $paradigm ---"
    PSTART=$(date +%s)

    timeout 300 python3 main.py paradigm "$paradigm" \
        $FAULT_QUERY \
        -n "$NAMESPACE" \
        -o "$RESULTS_DIR/${paradigm}.json" \
        2>&1 | tail -20 || true

    PEND=$(date +%s)
    PDUR=$((PEND - PSTART))
    PARADIGM_TIMES[$paradigm]=$PDUR

    if [[ -f "$RESULTS_DIR/${paradigm}.json" ]]; then
        PARADIGM_STATUS[$paradigm]="completed"
        log "$paradigm 完成, 耗时: ${PDUR}秒"
    else
        PARADIGM_STATUS[$paradigm]="failed"
        log "$paradigm 失败, 耗时: ${PDUR}秒"
    fi
    log ""
done

log "===== 步骤3: 汇总对比 ====="
python3 -c "
import json, os, glob

paradigms = ['chain', 'react', 'reflection', 'plan_and_execute', 'debate', 'voting']
results_dir = '$RESULTS_DIR'
summary = {
    'test_case': 'PT-002',
    'test_name': '多智能体协作范式与故障诊断能力测试',
    'fault_query': '''$FAULT_QUERY''',
    'paradigm_results': [],
    'verdict': 'PASS',
    'completed_count': 0,
    'total_count': len(paradigms),
}

for p in paradigms:
    fpath = os.path.join(results_dir, f'{p}.json')
    entry = {'paradigm': p, 'status': 'failed', 'duration_s': 0, 'confidence': 0, 'root_cause': ''}
    if os.path.exists(fpath):
        try:
            data = json.load(open(fpath))
            entry['status'] = data.get('status', 'unknown')
            entry['confidence'] = data.get('confidence', data.get('result', {}).get('confidence', 0))
            rc = data.get('root_cause', '') or data.get('result', {}).get('root_cause', '')
            entry['root_cause'] = str(rc)[:300]
            if entry['status'] in ('completed', 'done'):
                summary['completed_count'] += 1
        except:
            pass
    summary['paradigm_results'].append(entry)

# 至少4种范式成功才算通过
if summary['completed_count'] < 4:
    summary['verdict'] = 'FAIL'

json.dump(summary, open('$OUTPUT', 'w'), indent=2, ensure_ascii=False, default=str)
print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)[:3000])
"

log ""
log "===== 步骤4: 清理 ====="
kubectl delete pod pt002-cpu-stress -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true

log "===== PT-002 完成 ====="
log "结果文件: $OUTPUT"
log "各范式详细结果: $RESULTS_DIR/"

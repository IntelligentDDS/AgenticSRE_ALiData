#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# PT-001: 智能化运维工作流自动形成能力测试
# 验证五阶段 Pipeline (Detection→Hypothesis→Investigation→Reasoning→Recovery)
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
OUTPUT="${OUTPUT:-测试/test_results/pt001_$(date +%Y%m%d_%H%M%S).json}"
mkdir -p "$(dirname "$OUTPUT")"

log() { echo "[PT-001][$(date +%H:%M:%S)] $*"; }

log "===== 前置检查 ====="

# 1. 检查系统健康
log "检查工具健康状态..."
python3 main.py health
log ""

# 2. 检查集群连通性
log "检查集群状态..."
python3 main.py status
log ""

log "===== 步骤1: 注入故障 (CrashLoopBackOff) ====="
# 创建一个必定 crash 的 pod
kubectl run pt001-crash-test \
    --image=busybox \
    --restart=Always \
    -n "$NAMESPACE" \
    -- /bin/sh -c 'echo "PT-001 fault injection"; exit 1' \
    2>/dev/null || log "Pod可能已存在, 继续..."

log "等待30秒让故障生效..."
sleep 30

# 确认故障已注入
log "确认故障状态:"
kubectl get pod pt001-crash-test -n "$NAMESPACE" 2>/dev/null || true
log ""

log "===== 步骤2: 执行五阶段Pipeline ====="
START_TIME=$(date +%s)

python3 main.py pipeline \
    "Pod CrashLoopBackOff detected: pt001-crash-test in namespace $NAMESPACE keeps restarting with exit code 1" \
    -n "$NAMESPACE" \
    > "$OUTPUT.raw" 2>&1 || true

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

log "Pipeline 执行完成, 耗时: ${DURATION}秒"
log ""

log "===== 步骤3: 验证结果 ====="
# 提取结果
if [[ -f "$OUTPUT.raw" ]]; then
    # 提取最后的JSON结果
    python3 -c "
import json, sys

raw = open('$OUTPUT.raw').read()
# 找到最后一个 { 开头的JSON块
lines = raw.split('\n')
json_start = -1
for i in range(len(lines)-1, -1, -1):
    if lines[i].strip().startswith('{'):
        json_start = i
        break

result = {}
if json_start >= 0:
    try:
        json_text = '\n'.join(lines[json_start:])
        result = json.loads(json_text)
    except:
        result = {'raw_output': raw[-2000:]}
else:
    result = {'raw_output': raw[-2000:]}

# 验证各阶段
phases = ['detection', 'hypothesis', 'investigation', 'reasoning', 'recovery']
test_result = {
    'test_case': 'PT-001',
    'test_name': '智能化运维工作流自动形成能力测试',
    'duration_seconds': $DURATION,
    'pipeline_result': result,
    'phase_check': {},
    'verdict': 'PASS'
}

# 检查 pipeline 状态
status = result.get('status', '')
if status in ('completed', 'done'):
    test_result['phase_check']['pipeline_completed'] = True
else:
    test_result['phase_check']['pipeline_completed'] = False
    test_result['verdict'] = 'FAIL'

# 检查根因结论
rca = result.get('result', {})
if isinstance(rca, dict):
    if rca.get('root_cause') or rca.get('report', {}).get('root_cause'):
        test_result['phase_check']['root_cause_identified'] = True
    else:
        test_result['phase_check']['root_cause_identified'] = False
    if rca.get('remediation_suggestion') or rca.get('report', {}).get('remediation_suggestion'):
        test_result['phase_check']['remediation_provided'] = True
    else:
        test_result['phase_check']['remediation_provided'] = False

json.dump(test_result, open('$OUTPUT', 'w'), indent=2, ensure_ascii=False, default=str)
print(json.dumps(test_result, indent=2, ensure_ascii=False, default=str)[:3000])
"
fi

log ""
log "===== 步骤4: 清理故障 ====="
kubectl delete pod pt001-crash-test -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true

log ""
log "===== PT-001 完成 ====="
log "结果文件: $OUTPUT"
log "耗时: ${DURATION}秒"
rm -f "$OUTPUT.raw"

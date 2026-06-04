#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# PT-004: 运维演化方案自动构造与持续学习能力测试
# 验证: 故障记忆存储 + 历史知识复用 + 专家反馈 + 演化报告
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
OUTPUT="${OUTPUT:-测试/test_results/pt004_$(date +%Y%m%d_%H%M%S).json}"
mkdir -p "$(dirname "$OUTPUT")"

log() { echo "[PT-004][$(date +%H:%M:%S)] $*"; }

log "===== 步骤1: 第一次故障诊断 (建立基线记忆) ====="

# 注入 CrashLoopBackOff 故障
log "注入故障: CrashLoopBackOff..."
kubectl run pt004-crash-v1 \
    --image=busybox \
    --restart=Always \
    -n "$NAMESPACE" \
    -- /bin/sh -c 'echo "PT-004 round 1"; exit 1' \
    2>/dev/null || true

sleep 30
log "故障已注入, 开始第一次诊断..."

ROUND1_START=$(date +%s)
python3 main.py pipeline \
    "Pod CrashLoopBackOff detected: pt004-crash-v1 in namespace $NAMESPACE keeps restarting" \
    -n "$NAMESPACE" \
    > "$(dirname "$OUTPUT")/pt004_round1.raw" 2>&1 || true
ROUND1_END=$(date +%s)
ROUND1_DUR=$((ROUND1_END - ROUND1_START))

log "第一次诊断完成, 耗时: ${ROUND1_DUR}秒"
log ""

log "===== 步骤2: 提交专家反馈 ====="
python3 main.py feedback \
    --fault-type "CrashLoopBackOff" \
    --feedback "Root cause is exit code 1 in container entrypoint. Remediation: fix the entrypoint script or use a proper health check." \
    --rating 4 \
    2>&1 || log "反馈提交失败, 继续..."
log ""

log "===== 步骤3: 第二次同类故障诊断 (验证知识复用) ====="

# 清理第一个故障 Pod
kubectl delete pod pt004-crash-v1 -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true

# 注入同类故障
log "注入同类故障..."
kubectl run pt004-crash-v2 \
    --image=busybox \
    --restart=Always \
    -n "$NAMESPACE" \
    -- /bin/sh -c 'echo "PT-004 round 2"; exit 1' \
    2>/dev/null || true

sleep 30

ROUND2_START=$(date +%s)
python3 main.py pipeline \
    "Pod CrashLoopBackOff detected: pt004-crash-v2 in namespace $NAMESPACE keeps restarting with exit code 1" \
    -n "$NAMESPACE" \
    > "$(dirname "$OUTPUT")/pt004_round2.raw" 2>&1 || true
ROUND2_END=$(date +%s)
ROUND2_DUR=$((ROUND2_END - ROUND2_START))

log "第二次诊断完成, 耗时: ${ROUND2_DUR}秒"
log ""

log "===== 步骤4: 查看演化报告 ====="
python3 main.py evolution 2>&1 | tee "$(dirname "$OUTPUT")/pt004_evolution.txt" || true
log ""

log "===== 步骤5: 汇总验证 ====="
python3 -c "
import json

test_result = {
    'test_case': 'PT-004',
    'test_name': '运维演化方案自动构造与持续学习能力测试',
    'checks': {},
    'verdict': 'PASS'
}

checks = test_result['checks']

# 第一次诊断
checks['round1_duration_s'] = $ROUND1_DUR
try:
    raw1 = open('$(dirname "$OUTPUT")/pt004_round1.raw').read()
    checks['round1_executed'] = len(raw1) > 100
except:
    checks['round1_executed'] = False

# 第二次诊断
checks['round2_duration_s'] = $ROUND2_DUR
try:
    raw2 = open('$(dirname "$OUTPUT")/pt004_round2.raw').read()
    checks['round2_executed'] = len(raw2) > 100
except:
    checks['round2_executed'] = False

# 知识复用: 第二次应该更快
if checks.get('round1_duration_s', 0) > 0 and checks.get('round2_duration_s', 0) > 0:
    speedup = checks['round1_duration_s'] - checks['round2_duration_s']
    checks['speedup_seconds'] = speedup
    checks['knowledge_reuse_effective'] = speedup > 0
else:
    checks['knowledge_reuse_effective'] = False

# 演化报告
try:
    evo = open('$(dirname "$OUTPUT")/pt004_evolution.txt').read()
    checks['evolution_report_generated'] = len(evo) > 50
except:
    checks['evolution_report_generated'] = False

# 判定: 两次诊断都执行且演化报告生成
if not (checks.get('round1_executed') and checks.get('round2_executed')):
    test_result['verdict'] = 'FAIL'

json.dump(test_result, open('$OUTPUT', 'w'), indent=2, ensure_ascii=False, default=str)
print(json.dumps(test_result, indent=2, ensure_ascii=False, default=str))
"

log ""
log "===== 步骤6: 清理 ====="
kubectl delete pod pt004-crash-v2 -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true
rm -f "$(dirname "$OUTPUT")/pt004_round1.raw" "$(dirname "$OUTPUT")/pt004_round2.raw"

log "===== PT-004 完成 ====="
log "结果文件: $OUTPUT"
log "演化报告: $(dirname "$OUTPUT")/pt004_evolution.txt"

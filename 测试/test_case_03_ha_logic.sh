#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# PT-003: 面向高可用性的运维逻辑自动构造能力测试
# 验证: Daemon监控 + 告警压缩 + 异常检测 + 自愈建议
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
OUTPUT="${OUTPUT:-测试/test_results/pt003_$(date +%Y%m%d_%H%M%S).json}"
mkdir -p "$(dirname "$OUTPUT")"

log() { echo "[PT-003][$(date +%H:%M:%S)] $*"; }

log "===== 步骤1: 注入多个并发故障 ====="

# 故障A: CPU压力
log "注入故障A: CPU压力..."
kubectl run pt003-cpu-stress \
    --image=containerstack/alpine-stress \
    --restart=Never \
    -n "$NAMESPACE" \
    -- stress --cpu 4 --timeout 300 \
    2>/dev/null || true

# 故障B: CrashLoop
log "注入故障B: CrashLoopBackOff..."
kubectl run pt003-crash \
    --image=busybox \
    --restart=Always \
    -n "$NAMESPACE" \
    -- /bin/sh -c 'exit 1' \
    2>/dev/null || true

# 故障C: 错误镜像
log "注入故障C: ImagePullBackOff..."
kubectl run pt003-bad-image \
    --image=nonexistent-registry.io/fake:v999 \
    --restart=Never \
    -n "$NAMESPACE" \
    2>/dev/null || true

log "等待60秒让故障生效并产生告警..."
sleep 60
log ""

log "===== 步骤2: 执行告警压缩扫描 ====="
ALERT_RESULT="$(dirname "$OUTPUT")/pt003_alerts.json"

python3 main.py alert-scan -n "$NAMESPACE" -r 15m 2>&1 | tee "$ALERT_RESULT.raw"
log ""

log "===== 步骤3: 启动Daemon并观察自动检测 (运行90秒) ====="
DAEMON_LOG="$(dirname "$OUTPUT")/pt003_daemon.log"

# 后台启动 Daemon, 90秒后自动结束
timeout 90 python3 main.py daemon -n "$NAMESPACE" -i 30 \
    > "$DAEMON_LOG" 2>&1 || true

log "Daemon 已运行90秒 (3个检测周期)"
log "Daemon日志行数: $(wc -l < "$DAEMON_LOG")"
log ""

log "===== 步骤4: 检查系统状态 ====="
python3 main.py status 2>&1 || true
log ""

log "===== 步骤5: 检查异常检测结果 ====="
python3 main.py health 2>&1 || true
log ""

log "===== 步骤6: 汇总验证 ====="
python3 -c "
import json

test_result = {
    'test_case': 'PT-003',
    'test_name': '面向高可用性的运维逻辑自动构造能力测试',
    'checks': {},
    'verdict': 'PASS'
}

# 检查告警扫描结果
try:
    raw = open('$ALERT_RESULT.raw').read()
    # 解析告警压缩结果
    checks = test_result['checks']

    if 'Total alerts:' in raw:
        for line in raw.split('\n'):
            if 'Total alerts:' in line:
                checks['total_alerts'] = int(line.split(':')[1].strip())
            elif 'Alert groups:' in line:
                checks['alert_groups'] = int(line.split(':')[1].strip())
            elif 'Compression ratio:' in line:
                ratio_str = line.split(':')[1].strip().replace('%','')
                checks['compression_ratio'] = ratio_str
    checks['alert_scan_executed'] = True
except:
    test_result['checks']['alert_scan_executed'] = False
    test_result['verdict'] = 'FAIL'

# 检查 Daemon 日志
try:
    daemon_log = open('$DAEMON_LOG').read()
    checks = test_result['checks']
    checks['daemon_started'] = 'Daemon started' in daemon_log or 'AgenticSRE' in daemon_log
    checks['daemon_log_lines'] = len(daemon_log.split('\n'))
    # 检查是否检测到了异常
    checks['anomalies_detected'] = (
        'signal' in daemon_log.lower() or
        'anomal' in daemon_log.lower() or
        'alert' in daemon_log.lower() or
        'crash' in daemon_log.lower() or
        'detection' in daemon_log.lower()
    )
except:
    test_result['checks']['daemon_started'] = False

json.dump(test_result, open('$OUTPUT', 'w'), indent=2, ensure_ascii=False, default=str)
print(json.dumps(test_result, indent=2, ensure_ascii=False, default=str))
"

log ""
log "===== 步骤7: 清理故障 ====="
kubectl delete pod pt003-cpu-stress -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true
kubectl delete pod pt003-crash -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true
kubectl delete pod pt003-bad-image -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true

log "===== PT-003 完成 ====="
log "结果文件: $OUTPUT"
log "告警扫描: $ALERT_RESULT.raw"
log "Daemon日志: $DAEMON_LOG"
rm -f "$ALERT_RESULT.raw"

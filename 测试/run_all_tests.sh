#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# AgenticSRE 指标3.9 — 全部测试用例自动执行入口
# 用法:  bash 测试/run_all_tests.sh [--case 1|2|3|4|5] [--namespace social-network]
# 环境:  ssh -J openstack@222.200.180.102 ubuntu@10.10.3.110
# ═══════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$SCRIPT_DIR/test_results"
NAMESPACE="${NAMESPACE:-social-network}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
CASE_FILTER=""

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --case)    CASE_FILTER="$2"; shift 2 ;;
        --namespace) NAMESPACE="$2"; shift 2 ;;
        *)         echo "Unknown option: $1"; exit 1 ;;
    esac
done

mkdir -p "$RESULTS_DIR"
cd "$PROJECT_DIR"

LOG="$RESULTS_DIR/test_run_${TIMESTAMP}.log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
sep() { log "════════════════════════════════════════════════════════"; }

log "AgenticSRE 指标3.9 测试执行  $(date)"
log "Project: $PROJECT_DIR"
log "Namespace: $NAMESPACE"
log "Results: $RESULTS_DIR"
sep

should_run() { [[ -z "$CASE_FILTER" ]] || [[ "$CASE_FILTER" == "$1" ]]; }

# ─────────────────────────────────────────────────────
# PT-001: 智能化运维工作流自动形成
# ─────────────────────────────────────────────────────
if should_run 1; then
    sep
    log "PT-001: 智能化运维工作流自动形成能力测试"
    sep
    bash "$SCRIPT_DIR/test_case_01_pipeline.sh" \
        --namespace "$NAMESPACE" \
        --output "$RESULTS_DIR/pt001_${TIMESTAMP}.json" \
        2>&1 | tee -a "$LOG"
fi

# ─────────────────────────────────────────────────────
# PT-002: 多智能体协作范式
# ─────────────────────────────────────────────────────
if should_run 2; then
    sep
    log "PT-002: 多智能体协作范式与故障诊断能力测试"
    sep
    bash "$SCRIPT_DIR/test_case_02_paradigms.sh" \
        --namespace "$NAMESPACE" \
        --output "$RESULTS_DIR/pt002_${TIMESTAMP}.json" \
        2>&1 | tee -a "$LOG"
fi

# ─────────────────────────────────────────────────────
# PT-003: 高可用运维逻辑自动构造
# ─────────────────────────────────────────────────────
if should_run 3; then
    sep
    log "PT-003: 面向高可用性的运维逻辑自动构造能力测试"
    sep
    bash "$SCRIPT_DIR/test_case_03_ha_logic.sh" \
        --namespace "$NAMESPACE" \
        --output "$RESULTS_DIR/pt003_${TIMESTAMP}.json" \
        2>&1 | tee -a "$LOG"
fi

# ─────────────────────────────────────────────────────
# PT-004: 运维演化方案自动构造
# ─────────────────────────────────────────────────────
if should_run 4; then
    sep
    log "PT-004: 运维演化方案自动构造与持续学习能力测试"
    sep
    bash "$SCRIPT_DIR/test_case_04_evolution.sh" \
        --namespace "$NAMESPACE" \
        --output "$RESULTS_DIR/pt004_${TIMESTAMP}.json" \
        2>&1 | tee -a "$LOG"
fi

# ─────────────────────────────────────────────────────
# PT-005: 故障处理时间对比
# ─────────────────────────────────────────────────────
if should_run 5; then
    sep
    log "PT-005: 故障处理时间对比测试（对比阿里云减少10%）"
    sep
    bash "$SCRIPT_DIR/test_case_05_benchmark.sh" \
        --namespace "$NAMESPACE" \
        --output "$RESULTS_DIR/pt005_${TIMESTAMP}.json" \
        2>&1 | tee -a "$LOG"
fi

sep
log "全部测试完成!  结果目录: $RESULTS_DIR"
log "日志文件: $LOG"

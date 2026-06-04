#!/usr/bin/env python3
"""
AgenticSRE — End-to-End Cluster Evaluation
Runs 6 paradigms x 2 modes (enriched/baseline) x N fault scenarios
on a live K8s cluster with social-network microservices.

Usage:
    python -m eval.e2e_cluster_eval                                  # Full evaluation
    python -m eval.e2e_cluster_eval --scenario sn-cpu-stress         # Single scenario
    python -m eval.e2e_cluster_eval --paradigm react,debate          # Selected paradigms
    python -m eval.e2e_cluster_eval --skip-workload                  # Skip background load
    python -m eval.e2e_cluster_eval --mode enriched                  # Only enriched mode
"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from configs.config_loader import get_config
from paradigms import AgentPool, get_paradigm, paradigm_names, ParadigmResult
from eval.workload_generator import WorkloadGenerator

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"
SCENARIOS_FILE = EVAL_DIR / "fault_scenarios.yaml"

ALL_PARADIGMS = ["chain", "react", "reflection", "plan_and_execute", "debate", "voting"]


@dataclass
class EvalResult:
    """Result of a single (scenario, paradigm, mode) evaluation run."""
    scenario_id: str
    scenario_name: str
    paradigm: str
    mode: str  # "enriched" or "baseline"
    status: str = "pending"
    detection_time_s: float = 0
    confidence: float = 0
    root_cause_match: bool = False
    fault_type_match: bool = False
    has_remediation: bool = False
    score: float = 0
    agent_calls: int = 0
    llm_calls: int = 0
    root_cause: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "paradigm": self.paradigm,
            "mode": self.mode,
            "status": self.status,
            "detection_time_s": round(self.detection_time_s, 2),
            "confidence": round(self.confidence, 3),
            "root_cause_match": self.root_cause_match,
            "fault_type_match": self.fault_type_match,
            "has_remediation": self.has_remediation,
            "score": round(self.score, 3),
            "agent_calls": self.agent_calls,
            "llm_calls": self.llm_calls,
            "root_cause": self.root_cause[:200],
            "error": self.error,
        }


class E2EClusterEval:
    """
    End-to-end cluster evaluation: 6 paradigms x 2 modes x N faults.
    Produces a cross-comparison report (paradigm x mode).
    """

    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.raw_config = self._load_scenarios_raw()
        self.scenarios = self.raw_config.get("scenarios", [])
        self.workload_config = self.raw_config.get("workload", {})
        self.scoring = self.raw_config.get("scoring", {})
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_scenarios_raw(self) -> Dict:
        if SCENARIOS_FILE.exists():
            with open(SCENARIOS_FILE) as f:
                return yaml.safe_load(f) or {}
        logger.warning("Scenarios file not found: %s", SCENARIOS_FILE)
        return {"scenarios": [], "scoring": {}}

    def _run_commands(self, commands: List[str]) -> bool:
        """Execute injection or cleanup commands on the cluster."""
        for cmd in commands:
            try:
                logger.info("  Exec: %s", cmd[:120])
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    logger.warning("  Cmd warning: %s", result.stderr.strip()[:200])
            except Exception as e:
                logger.error("  Cmd failed: %s", e)
                return False
        return True

    def _evaluate(
        self,
        scenario: Dict,
        paradigm_result: ParadigmResult,
        elapsed: float,
        mode: str,
    ) -> EvalResult:
        """Score a paradigm result against the expected outcome."""
        er = EvalResult(
            scenario_id=scenario["id"],
            scenario_name=scenario["name"],
            paradigm=paradigm_result.paradigm_name,
            mode=mode,
            detection_time_s=elapsed,
        )

        expected = scenario.get("expected", {})
        validation = scenario.get("validation", {})

        er.confidence = paradigm_result.confidence
        er.root_cause = paradigm_result.root_cause

        # Root cause keyword match
        root_cause_lower = paradigm_result.root_cause.lower()
        keywords = expected.get("root_cause_contains", [])
        er.root_cause_match = any(kw.lower() in root_cause_lower for kw in keywords) if keywords else False

        # Fault type match
        expected_type = expected.get("fault_type", "")
        er.fault_type_match = (
            expected_type.lower() in paradigm_result.fault_type.lower()
            if expected_type else False
        )

        # Remediation
        er.has_remediation = bool(paradigm_result.remediation_suggestion)

        # Metrics
        er.agent_calls = paradigm_result.metrics.agent_calls
        er.llm_calls = paradigm_result.metrics.llm_calls

        # Composite score
        w = self.scoring
        score = 0.0
        max_time = validation.get("max_detection_time_s", 120)
        time_score = max(0, 1.0 - max(0, elapsed - max_time) / max_time)
        score += w.get("detection_time_weight", 0.2) * time_score

        min_conf = validation.get("min_confidence", 0.5)
        conf_score = min(1.0, er.confidence / min_conf) if min_conf > 0 else er.confidence
        score += w.get("confidence_weight", 0.3) * conf_score

        score += w.get("root_cause_match_weight", 0.3) * (1.0 if er.root_cause_match else 0.0)
        score += w.get("remediation_quality_weight", 0.2) * (1.0 if er.has_remediation else 0.0)

        er.score = score
        er.status = "passed" if score >= 0.5 else "failed"
        return er

    async def _run_one(
        self,
        pool: AgentPool,
        paradigm_name: str,
        scenario: Dict,
        mode: str,
    ) -> EvalResult:
        """Run a single paradigm on a single scenario."""
        scenario_id = scenario["id"]
        namespace = scenario.get("inject", {}).get("namespace", "social-network")

        logger.info("  [%s/%s] %s ...", paradigm_name, mode, scenario_id)
        try:
            paradigm_cls = get_paradigm(paradigm_name)
            paradigm = paradigm_cls(pool)

            start = time.time()
            result = await paradigm.run(
                scenario["description"],
                namespace=namespace,
                log_callback=lambda msg: logger.debug("    [%s] %s", paradigm_name, msg),
            )
            elapsed = time.time() - start

            er = self._evaluate(scenario, result, elapsed, mode)
            logger.info("    score=%.3f conf=%.3f rca_match=%s time=%.1fs",
                         er.score, er.confidence, er.root_cause_match, er.detection_time_s)
            return er

        except Exception as e:
            logger.error("  [%s/%s] on %s FAILED: %s", paradigm_name, mode, scenario_id, e)
            return EvalResult(
                scenario_id=scenario_id,
                scenario_name=scenario["name"],
                paradigm=paradigm_name,
                mode=mode,
                status="error",
                error=str(e),
            )

    async def run_full_evaluation(
        self,
        paradigm_filter: Optional[List[str]] = None,
        scenario_filter: Optional[str] = None,
        skip_workload: bool = False,
        mode_filter: Optional[str] = None,
    ) -> Dict:
        """
        Main evaluation loop:
        For each scenario: inject fault -> run all (paradigm, mode) combos -> cleanup.
        """
        selected_paradigms = paradigm_filter or ALL_PARADIGMS
        modes = [mode_filter] if mode_filter else ["enriched", "baseline"]

        # Filter scenarios
        scenarios = self.scenarios
        if scenario_filter:
            scenarios = [s for s in scenarios if s["id"] == scenario_filter]

        if not scenarios:
            logger.error("No scenarios to run!")
            return {"error": "no scenarios"}

        logger.info("=" * 70)
        logger.info("AgenticSRE E2E Cluster Evaluation")
        logger.info("  Paradigms: %s", selected_paradigms)
        logger.info("  Modes: %s", modes)
        logger.info("  Scenarios: %d", len(scenarios))
        logger.info("  Total runs: %d", len(scenarios) * len(selected_paradigms) * len(modes))
        logger.info("=" * 70)

        all_results: List[EvalResult] = []

        for si, scenario in enumerate(scenarios, 1):
            scenario_id = scenario["id"]
            inject = scenario.get("inject", {})

            logger.info("\n%s", "=" * 60)
            logger.info("Scenario %d/%d: %s — %s", si, len(scenarios), scenario_id, scenario["name"])

            # 1. Start background workload
            workload = None
            if not skip_workload and self.workload_config:
                workload = WorkloadGenerator(self.workload_config)
                await workload.start()

            try:
                # 2. Inject fault
                logger.info("  Injecting fault...")
                self._run_commands(inject.get("commands", []))
                logger.info("  Waiting 20s for fault propagation...")
                await asyncio.sleep(20)

                # 3. Run each (paradigm, mode) combination
                for mode in modes:
                    pool = AgentPool(self.cfg, enrichment_enabled=(mode == "enriched"))

                    for pname in selected_paradigms:
                        er = await self._run_one(pool, pname, scenario, mode)
                        all_results.append(er)

            finally:
                # 4. Cleanup fault
                logger.info("  Cleaning up fault...")
                self._run_commands(inject.get("cleanup", []))

                # 5. Stop workload
                if workload is not None:
                    wl_stats = await workload.stop()
                    logger.info("  Workload stats: %s", wl_stats)

                await asyncio.sleep(5)

        # 5. Build and output report
        report = self._build_comparison_report(all_results)
        self._save_report(report)
        self._print_report(report)
        return report

    def _build_comparison_report(self, results: List[EvalResult]) -> Dict:
        """Build a multi-dimensional comparison report."""
        # Helper to aggregate a list of EvalResults
        def _agg(subset: List[EvalResult]) -> Dict:
            total = len(subset)
            if total == 0:
                return {}
            passed = sum(1 for r in subset if r.status == "passed")
            return {
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "avg_score": round(sum(r.score for r in subset) / total, 3),
                "avg_confidence": round(sum(r.confidence for r in subset) / total, 3),
                "avg_latency_s": round(sum(r.detection_time_s for r in subset) / total, 1),
                "avg_llm_calls": round(sum(r.llm_calls for r in subset) / total, 1),
                "rca_match_rate": round(sum(1 for r in subset if r.root_cause_match) / total, 3),
            }

        # By paradigm
        paradigm_set = sorted(set(r.paradigm for r in results))
        by_paradigm = {p: _agg([r for r in results if r.paradigm == p]) for p in paradigm_set}

        # By mode
        mode_set = sorted(set(r.mode for r in results))
        by_mode = {m: _agg([r for r in results if r.mode == m]) for m in mode_set}

        # By (paradigm, mode) cross
        by_paradigm_mode = {}
        for p in paradigm_set:
            for m in mode_set:
                subset = [r for r in results if r.paradigm == p and r.mode == m]
                if subset:
                    by_paradigm_mode[f"{p}/{m}"] = _agg(subset)

        # By scenario
        scenario_set = sorted(set(r.scenario_id for r in results))
        by_scenario = {s: _agg([r for r in results if r.scenario_id == s]) for s in scenario_set}

        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_runs": len(results),
            "paradigms_tested": paradigm_set,
            "modes_tested": mode_set,
            "scenarios_tested": scenario_set,
            "by_paradigm": by_paradigm,
            "by_mode": by_mode,
            "by_paradigm_mode": by_paradigm_mode,
            "by_scenario": by_scenario,
            "detailed_results": [r.to_dict() for r in results],
        }

    def _save_report(self, report: Dict):
        """Save report to JSON file."""
        report_path = RESULTS_DIR / f"cluster_eval_{int(time.time())}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        logger.info("Report saved: %s", report_path)

    def _print_report(self, report: Dict):
        """Print two comparison tables to terminal."""
        by_pm = report.get("by_paradigm_mode", {})
        by_mode = report.get("by_mode", {})
        paradigms = report.get("paradigms_tested", [])
        modes = report.get("modes_tested", [])
        num_scenarios = len(report.get("scenarios_tested", []))

        # ── Table 1: Paradigm x Mode Cross Comparison ──
        print()
        print("=" * 78)
        print("AgenticSRE Cluster Evaluation — Paradigm x Mode Comparison")
        print("=" * 78)
        print(f"{'Paradigm':<20} {'Mode':<12} {'Score':>6} {'Conf':>6} "
              f"{'Latency':>8} {'LLM':>5} {'RCA':>10}")
        print("-" * 78)

        for pname in paradigms:
            for mode in modes:
                key = f"{pname}/{mode}"
                stats = by_pm.get(key, {})
                if not stats:
                    continue
                match_count = int(stats.get("rca_match_rate", 0) * stats.get("total", 0))
                total = stats.get("total", 0)
                print(f"{pname:<20} {mode:<12} "
                      f"{stats.get('avg_score', 0):>6.3f} "
                      f"{stats.get('avg_confidence', 0):>6.2f} "
                      f"{stats.get('avg_latency_s', 0):>7.1f}s "
                      f"{stats.get('avg_llm_calls', 0):>5.0f} "
                      f"{match_count:>4}/{total}")

        print("=" * 78)

        # ── Table 2: Enriched vs Baseline Summary ──
        if len(modes) >= 2:
            enriched = by_mode.get("enriched", {})
            baseline = by_mode.get("baseline", {})
            if enriched and baseline:
                print()
                print("=" * 56)
                print("Enrichment Effect Summary")
                print("=" * 56)
                print(f"{'Metric':<22} {'Enriched':>10} {'Baseline':>10} {'Delta':>10}")
                print("-" * 56)

                e_score = enriched.get("avg_score", 0)
                b_score = baseline.get("avg_score", 0)
                delta_score = ((e_score - b_score) / b_score * 100) if b_score > 0 else 0

                e_conf = enriched.get("avg_confidence", 0)
                b_conf = baseline.get("avg_confidence", 0)
                delta_conf = ((e_conf - b_conf) / b_conf * 100) if b_conf > 0 else 0

                e_rca = enriched.get("rca_match_rate", 0) * 100
                b_rca = baseline.get("rca_match_rate", 0) * 100
                delta_rca = e_rca - b_rca

                e_lat = enriched.get("avg_latency_s", 0)
                b_lat = baseline.get("avg_latency_s", 0)
                delta_lat = ((e_lat - b_lat) / b_lat * 100) if b_lat > 0 else 0

                print(f"{'Avg Score':<22} {e_score:>10.3f} {b_score:>10.3f} {delta_score:>+9.1f}%")
                print(f"{'Avg Confidence':<22} {e_conf:>10.3f} {b_conf:>10.3f} {delta_conf:>+9.1f}%")
                print(f"{'RCA Match Rate':<22} {e_rca:>9.1f}% {b_rca:>9.1f}% {delta_rca:>+9.1f}%")
                print(f"{'Avg Latency':<22} {e_lat:>9.1f}s {b_lat:>9.1f}s {delta_lat:>+9.1f}%")
                print("=" * 56)

        print()


def main():
    parser = argparse.ArgumentParser(
        description="AgenticSRE E2E Cluster Evaluation (6 paradigms x enriched/baseline)"
    )
    parser.add_argument("--paradigm", help="Comma-separated paradigm names (default: all)")
    parser.add_argument("--scenario", help="Run specific scenario by ID")
    parser.add_argument("--skip-workload", action="store_true", help="Skip background workload")
    parser.add_argument("--mode", choices=["enriched", "baseline"], help="Run only one mode")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    paradigm_filter = args.paradigm.split(",") if args.paradigm else None

    evaluator = E2EClusterEval()
    asyncio.run(evaluator.run_full_evaluation(
        paradigm_filter=paradigm_filter,
        scenario_filter=args.scenario,
        skip_workload=args.skip_workload,
        mode_filter=args.mode,
    ))


if __name__ == "__main__":
    main()

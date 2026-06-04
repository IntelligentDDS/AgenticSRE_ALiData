#!/usr/bin/env python3
"""
AgenticSRE Comparative Runner
Multi-paradigm benchmark comparison: runs all (or selected) paradigms against
the same evaluation tasks and produces a side-by-side comparison report.

Usage:
    python -m eval.comparative_runner                          # All paradigms × all tasks
    python -m eval.comparative_runner --task cpu-stress-001    # Single task
    python -m eval.comparative_runner --paradigm react,debate  # Specific paradigms
    python -m eval.comparative_runner --paradigm chain --task cpu-stress-001
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

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"
TASKS_FILE = EVAL_DIR / "eval_tasks.yaml"


@dataclass
class ParadigmTaskResult:
    """Result of a single paradigm on a single task."""
    task_id: str
    task_name: str
    paradigm: str
    status: str = "pending"
    detection_time_s: float = 0
    confidence: float = 0
    root_cause_match: bool = False
    fault_type_match: bool = False
    has_remediation: bool = False
    score: float = 0
    agent_calls: int = 0
    llm_calls: int = 0
    iterations: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "paradigm": self.paradigm,
            "status": self.status,
            "detection_time_s": round(self.detection_time_s, 2),
            "confidence": round(self.confidence, 3),
            "root_cause_match": self.root_cause_match,
            "fault_type_match": self.fault_type_match,
            "has_remediation": self.has_remediation,
            "score": round(self.score, 3),
            "agent_calls": self.agent_calls,
            "llm_calls": self.llm_calls,
            "iterations": self.iterations,
            "error": self.error,
        }


class ComparativeRunner:
    """
    Runs multiple paradigms against the same evaluation tasks for fair comparison.
    Produces a side-by-side report with accuracy, latency, and cost metrics.
    """

    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.tasks = self._load_tasks()
        self.scoring = self.tasks.get("scoring", {})
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_tasks(self) -> Dict:
        if TASKS_FILE.exists():
            with open(TASKS_FILE) as f:
                return yaml.safe_load(f) or {}
        return {"tasks": [], "scoring": {}}

    def _run_commands(self, commands: List[str], method: str = "kubectl") -> bool:
        """Execute injection or cleanup commands."""
        for cmd in commands:
            try:
                if method == "ssh" and self.cfg.kubernetes.use_ssh:
                    full_cmd = (
                        f"ssh -J {self.cfg.kubernetes.ssh_jump_host} "
                        f"{self.cfg.kubernetes.ssh_target} '{cmd}'"
                    )
                else:
                    full_cmd = cmd
                logger.info(f"  Executing: {full_cmd}")
                subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=60)
            except Exception as e:
                logger.error(f"  Command failed: {e}")
                return False
        return True

    def _evaluate(self, task: Dict, paradigm_result: ParadigmResult, elapsed: float) -> ParadigmTaskResult:
        """Score a paradigm result against the expected outcome of a task."""
        tr = ParadigmTaskResult(
            task_id=task["id"],
            task_name=task["name"],
            paradigm=paradigm_result.paradigm_name,
            detection_time_s=elapsed,
        )

        expected = task.get("expected", {})
        validation = task.get("validation", {})

        tr.confidence = paradigm_result.confidence

        # Root cause match
        root_cause = paradigm_result.root_cause.lower()
        keywords = expected.get("root_cause_contains", [])
        tr.root_cause_match = any(kw.lower() in root_cause for kw in keywords) if keywords else False

        # Fault type match
        expected_type = expected.get("fault_type", "")
        tr.fault_type_match = expected_type.lower() in paradigm_result.fault_type.lower() if expected_type else False

        # Remediation
        tr.has_remediation = bool(paradigm_result.remediation_suggestion)

        # Metrics
        tr.agent_calls = paradigm_result.metrics.agent_calls
        tr.llm_calls = paradigm_result.metrics.llm_calls
        tr.iterations = paradigm_result.metrics.iterations

        # Composite score
        w = self.scoring
        score = 0.0
        max_time = validation.get("max_detection_time_s", 120)
        time_score = max(0, 1.0 - max(0, elapsed - max_time) / max_time)
        score += w.get("detection_time_weight", 0.2) * time_score

        min_conf = validation.get("min_confidence", 0.5)
        conf_score = min(1.0, tr.confidence / min_conf) if min_conf > 0 else tr.confidence
        score += w.get("confidence_weight", 0.3) * conf_score

        score += w.get("root_cause_match_weight", 0.3) * (1.0 if tr.root_cause_match else 0.0)
        score += w.get("remediation_quality_weight", 0.2) * (1.0 if tr.has_remediation else 0.0)

        tr.score = score
        tr.status = "passed" if score >= 0.5 else "failed"
        return tr

    async def run_paradigm_on_task(
        self,
        task: Dict,
        paradigm_name: str,
        pool: AgentPool,
    ) -> ParadigmTaskResult:
        """Run a single paradigm against a single task."""
        task_id = task["id"]
        logger.info(f"  [{paradigm_name}] on {task_id}...")

        try:
            paradigm_cls = get_paradigm(paradigm_name)
            paradigm = paradigm_cls(pool)

            start = time.time()
            result = await paradigm.run(
                task["description"],
                namespace=task.get("inject", {}).get("namespace", ""),
                log_callback=lambda msg: logger.debug(f"    [{paradigm_name}] {msg}"),
            )
            elapsed = time.time() - start

            return self._evaluate(task, result, elapsed)

        except Exception as e:
            logger.error(f"  [{paradigm_name}] on {task_id} FAILED: {e}")
            return ParadigmTaskResult(
                task_id=task_id,
                task_name=task["name"],
                paradigm=paradigm_name,
                status="error",
                error=str(e),
            )

    async def run_comparison(
        self,
        paradigm_filter: Optional[List[str]] = None,
        task_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
    ) -> Dict:
        """
        Run all selected paradigms against all selected tasks.
        Returns a comparison report.
        """
        # Select paradigms
        selected_paradigms = paradigm_filter or paradigm_names()
        logger.info(f"Paradigms: {selected_paradigms}")

        # Select tasks
        tasks = self.tasks.get("tasks", [])
        if task_filter:
            tasks = [t for t in tasks if t["id"] == task_filter]
        if category_filter:
            tasks = [t for t in tasks if t.get("category") == category_filter]
        logger.info(f"Tasks: {len(tasks)}")

        # Shared agent pool for fair comparison
        pool = AgentPool(self.cfg)

        all_results: List[ParadigmTaskResult] = []

        for task in tasks:
            task_id = task["id"]
            inject = task.get("inject", {})
            method = inject.get("method", "kubectl")

            logger.info(f"\n{'='*60}")
            logger.info(f"Task: {task_id} — {task['name']}")

            # Inject fault
            logger.info("  Injecting fault...")
            self._run_commands(inject.get("commands", []), method)
            await asyncio.sleep(15)

            try:
                # Run each paradigm on this task
                for pname in selected_paradigms:
                    tr = await self.run_paradigm_on_task(task, pname, pool)
                    all_results.append(tr)
                    logger.info(f"    {pname}: score={tr.score:.3f} conf={tr.confidence:.3f} "
                                f"agents={tr.agent_calls} llm={tr.llm_calls} time={tr.detection_time_s:.1f}s")
            finally:
                # Cleanup
                logger.info("  Cleaning up...")
                self._run_commands(inject.get("cleanup", []), method)
                await asyncio.sleep(5)

        # Build comparison report
        report = self._build_report(all_results, selected_paradigms)

        # Save
        report_path = RESULTS_DIR / f"comparison_{int(time.time())}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"\nReport saved: {report_path}")

        # Print summary
        self._print_summary(report)

        return report

    def _build_report(self, results: List[ParadigmTaskResult], paradigms: List[str]) -> Dict:
        """Build the comparison report from all results."""
        # Per-paradigm aggregation
        comparison = {}
        for pname in paradigms:
            p_results = [r for r in results if r.paradigm == pname]
            if not p_results:
                continue
            total = len(p_results)
            passed = sum(1 for r in p_results if r.status == "passed")
            comparison[pname] = {
                "accuracy": round(passed / total, 3) if total else 0,
                "avg_score": round(sum(r.score for r in p_results) / total, 3),
                "avg_confidence": round(sum(r.confidence for r in p_results) / total, 3),
                "avg_latency_s": round(sum(r.detection_time_s for r in p_results) / total, 1),
                "avg_llm_calls": round(sum(r.llm_calls for r in p_results) / total, 1),
                "avg_agent_calls": round(sum(r.agent_calls for r in p_results) / total, 1),
                "total_tasks": total,
                "passed": passed,
                "failed": total - passed,
            }

        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "paradigms_tested": paradigms,
            "total_tasks": len(set(r.task_id for r in results)),
            "comparison": comparison,
            "detailed_results": [r.to_dict() for r in results],
        }

    def _print_summary(self, report: Dict):
        """Print a formatted summary table."""
        comparison = report.get("comparison", {})

        print(f"\n{'='*80}")
        print("Multi-Paradigm Comparison Report")
        print(f"{'='*80}")
        print(f"{'Paradigm':<20} {'Accuracy':>8} {'Score':>7} {'Conf':>6} "
              f"{'Latency':>8} {'LLM':>5} {'Agents':>7}")
        print("-" * 80)

        for pname, stats in sorted(comparison.items(), key=lambda x: x[1]["avg_score"], reverse=True):
            print(f"{pname:<20} {stats['accuracy']:>7.1%} {stats['avg_score']:>7.3f} "
                  f"{stats['avg_confidence']:>6.3f} {stats['avg_latency_s']:>7.1f}s "
                  f"{stats['avg_llm_calls']:>5.0f} {stats['avg_agent_calls']:>7.0f}")

        print(f"{'='*80}")


def main():
    parser = argparse.ArgumentParser(description="AgenticSRE Multi-Paradigm Comparative Runner")
    parser.add_argument("--task", help="Run specific task by ID")
    parser.add_argument("--category", help="Filter tasks by category")
    parser.add_argument("--paradigm", help="Comma-separated paradigm names (default: all)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    paradigm_filter = args.paradigm.split(",") if args.paradigm else None

    runner = ComparativeRunner()
    asyncio.run(runner.run_comparison(
        paradigm_filter=paradigm_filter,
        task_filter=args.task,
        category_filter=args.category,
    ))


if __name__ == "__main__":
    main()

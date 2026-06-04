#!/usr/bin/env python3
"""
AgenticSRE Benchmark Runner
Automates fault injection → RCA → evaluation cycle.

Usage:
    python -m eval.benchmark_runner                  # Run all tasks
    python -m eval.benchmark_runner --task cpu-stress-001  # Run single task
    python -m eval.benchmark_runner --category resource    # Run by category
"""

import argparse
import asyncio
import json
import logging
import os
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
from orchestrator.rca_engine import run_rca

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"
TASKS_FILE = EVAL_DIR / "eval_tasks.yaml"


@dataclass
class TaskResult:
    task_id: str
    task_name: str
    status: str = "pending"  # pending, passed, failed, error
    detection_time_s: float = 0
    confidence: float = 0
    root_cause_match: bool = False
    fault_type_match: bool = False
    has_remediation: bool = False
    score: float = 0
    rca_result: Dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "status": self.status,
            "detection_time_s": round(self.detection_time_s, 2),
            "confidence": round(self.confidence, 3),
            "root_cause_match": self.root_cause_match,
            "fault_type_match": self.fault_type_match,
            "has_remediation": self.has_remediation,
            "score": round(self.score, 3),
            "error": self.error,
        }


class BenchmarkRunner:
    """Automated fault injection and RCA evaluation."""

    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.tasks = self._load_tasks()
        self.scoring = self.tasks.get("scoring", {})
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_tasks(self) -> Dict:
        with open(TASKS_FILE) as f:
            return yaml.safe_load(f)

    def _run_commands(self, commands: List[str], method: str = "kubectl") -> bool:
        """Execute injection or cleanup commands."""
        for cmd in commands:
            try:
                if method == "ssh" and self.cfg.kubernetes.use_ssh:
                    full_cmd = f"ssh -J {self.cfg.kubernetes.ssh_jump_host} {self.cfg.kubernetes.ssh_target} '{cmd}'"
                else:
                    full_cmd = cmd

                logger.info(f"  Executing: {full_cmd}")
                result = subprocess.run(
                    full_cmd, shell=True, capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    logger.warning(f"  Command warning: {result.stderr.strip()}")
            except Exception as e:
                logger.error(f"  Command failed: {e}")
                return False
        return True

    def _evaluate_result(self, task: Dict, rca_result: Dict, elapsed: float) -> TaskResult:
        """Score the RCA result against expected outcomes."""
        tr = TaskResult(
            task_id=task["id"],
            task_name=task["name"],
            detection_time_s=elapsed,
            rca_result=rca_result,
        )

        expected = task.get("expected", {})
        validation = task.get("validation", {})
        result_data = rca_result.get("result", {})

        # Confidence
        tr.confidence = result_data.get("confidence", 0)

        # Root cause match
        root_cause = (result_data.get("root_cause", "") or "").lower()
        keywords = expected.get("root_cause_contains", [])
        tr.root_cause_match = any(kw.lower() in root_cause for kw in keywords) if keywords else False

        # Fault type match
        expected_type = expected.get("fault_type", "")
        actual_type = (result_data.get("fault_type", "") or "").lower()
        tr.fault_type_match = expected_type.lower() in actual_type if expected_type else False

        # Remediation suggestion
        tr.has_remediation = bool(result_data.get("remediation_suggestion"))

        # Overall score
        w = self.scoring
        score = 0
        # Detection time score (1.0 if within limit, decreasing after)
        max_time = validation.get("max_detection_time_s", 120)
        time_score = max(0, 1.0 - max(0, elapsed - max_time) / max_time)
        score += w.get("detection_time_weight", 0.2) * time_score

        # Confidence score
        min_conf = validation.get("min_confidence", 0.5)
        conf_score = min(1.0, tr.confidence / min_conf) if min_conf > 0 else tr.confidence
        score += w.get("confidence_weight", 0.3) * conf_score

        # Root cause match score
        score += w.get("root_cause_match_weight", 0.3) * (1.0 if tr.root_cause_match else 0.0)

        # Remediation score
        score += w.get("remediation_quality_weight", 0.2) * (1.0 if tr.has_remediation else 0.0)

        tr.score = score
        tr.status = "passed" if score >= 0.5 else "failed"

        return tr

    async def run_task(self, task: Dict) -> TaskResult:
        """Run a single evaluation task: inject → RCA → evaluate → cleanup."""
        task_id = task["id"]
        logger.info(f"\n{'='*60}")
        logger.info(f"🧪 Running task: {task_id} — {task['name']}")

        inject = task.get("inject", {})
        method = inject.get("method", "kubectl")

        try:
            # 1. Inject fault
            logger.info("  📌 Injecting fault...")
            self._run_commands(inject.get("commands", []), method)

            # 2. Wait for fault to manifest
            logger.info("  ⏳ Waiting for fault to propagate...")
            await asyncio.sleep(15)

            # 3. Run RCA
            logger.info("  🔍 Running RCA pipeline...")
            start = time.time()
            rca_result = await run_rca(
                incident_query=task["description"],
                namespace=inject.get("namespace", ""),
                config=self.cfg,
                log_callback=lambda msg: logger.info(f"    {msg}"),
            )
            elapsed = time.time() - start
            logger.info(f"  ⏱ RCA completed in {elapsed:.1f}s")

            # 4. Evaluate
            tr = self._evaluate_result(task, rca_result, elapsed)
            logger.info(f"  📊 Score: {tr.score:.3f} — {tr.status}")

        except Exception as e:
            logger.error(f"  ❌ Task failed: {e}")
            tr = TaskResult(task_id=task_id, task_name=task["name"], status="error", error=str(e))

        finally:
            # 5. Cleanup
            logger.info("  🧹 Cleaning up...")
            self._run_commands(inject.get("cleanup", []), method)

        return tr

    async def run_all(self, task_filter: Optional[str] = None, category_filter: Optional[str] = None) -> Dict:
        """Run all matching tasks and produce a report."""
        tasks = self.tasks.get("tasks", [])

        if task_filter:
            tasks = [t for t in tasks if t["id"] == task_filter]
        if category_filter:
            tasks = [t for t in tasks if t.get("category") == category_filter]

        logger.info(f"🏁 Running {len(tasks)} evaluation tasks")

        results = []
        for task in tasks:
            tr = await self.run_task(task)
            results.append(tr)
            await asyncio.sleep(5)  # gap between tasks

        # Summary
        total = len(results)
        passed = sum(1 for r in results if r.status == "passed")
        avg_score = sum(r.score for r in results) / total if total else 0
        avg_conf = sum(r.confidence for r in results) / total if total else 0

        report = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_tasks": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 3) if total else 0,
            "avg_score": round(avg_score, 3),
            "avg_confidence": round(avg_conf, 3),
            "results": [r.to_dict() for r in results],
        }

        # Save report
        report_path = RESULTS_DIR / f"eval_{int(time.time())}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"\n📁 Report saved: {report_path}")

        # Print summary
        print(f"\n{'='*60}")
        print(f"📊 Evaluation Summary")
        print(f"{'='*60}")
        print(f"  Total tasks:   {total}")
        print(f"  Passed:        {passed}")
        print(f"  Failed:        {total - passed}")
        print(f"  Pass rate:     {report['pass_rate']:.1%}")
        print(f"  Avg score:     {avg_score:.3f}")
        print(f"  Avg confidence:{avg_conf:.3f}")
        print(f"{'='*60}")

        for r in results:
            icon = "✅" if r.status == "passed" else "❌" if r.status == "failed" else "⚠️"
            print(f"  {icon} [{r.task_id}] {r.task_name}: score={r.score:.3f}, conf={r.confidence:.3f}")

        return report


def main():
    parser = argparse.ArgumentParser(description="AgenticSRE Benchmark Runner")
    parser.add_argument("--task", help="Run specific task by ID")
    parser.add_argument("--category", help="Run tasks by category")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    runner = BenchmarkRunner()
    asyncio.run(runner.run_all(task_filter=args.task, category_filter=args.category))


if __name__ == "__main__":
    main()

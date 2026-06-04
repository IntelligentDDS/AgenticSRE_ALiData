#!/usr/bin/env python3
"""
AgenticSRE — Main CLI Entry Point

Usage:
    python main.py daemon              # Start 7x24 continuous daemon
    python main.py rca "query"         # Run single RCA analysis
    python main.py pipeline "query"    # Run full 5-phase pipeline
    python main.py paradigm NAME "q"   # Run single paradigm (chain/react/reflection/...)
    python main.py compare "query"     # Compare all paradigms on one query
    python main.py feedback             # Submit expert feedback (supervised learning)
    python main.py evolution            # Print system evolution report
    python main.py web                 # Start web dashboard
    python main.py status              # Check daemon / cluster status
    python main.py alert-scan          # Run alert compression scan
    python main.py health              # Health check all tools
"""

import argparse
import asyncio
import json
import logging
import sys

from configs.config_loader import get_config
from orchestrator.rca_engine import run_rca
from orchestrator.pipeline import Pipeline
from orchestrator.daemon import run_daemon


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_daemon(args):
    """Start 7x24 continuous monitoring daemon."""
    cfg = get_config()
    if args.namespace:
        cfg.daemon.default_namespace = args.namespace
    if args.interval:
        cfg.daemon.poll_interval_seconds = args.interval

    print("🚀 Starting AgenticSRE Daemon...")
    print(f"   Poll interval: {cfg.daemon.poll_interval_seconds}s")
    print(f"   Namespace: {cfg.daemon.default_namespace or 'all'}")
    print("   Press Ctrl+C to stop\n")

    run_daemon(cfg, log_callback=lambda msg: print(msg))


def cmd_rca(args):
    """Run single RCA analysis."""
    cfg = get_config()
    query = " ".join(args.query)
    print(f"🔍 Running RCA: {query}")
    print(f"   Namespace: {args.namespace or 'all'}\n")

    result = asyncio.run(
        run_rca(query, namespace=args.namespace or "", config=cfg,
                log_callback=lambda msg: print(msg))
    )

    print("\n" + "═" * 60)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n📁 Result saved to {args.output}")


def cmd_pipeline(args):
    """Run full 5-phase pipeline."""
    cfg = get_config()
    query = " ".join(args.query)
    pipeline = Pipeline(cfg)

    print(f"🚀 Running Pipeline: {query}")
    print(f"   Namespace: {args.namespace or 'all'}\n")

    result = asyncio.run(
        pipeline.run(query, namespace=args.namespace or "",
                     log_callback=lambda msg: print(msg))
    )

    print("\n" + "═" * 60)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))


def cmd_web(args):
    """Start web dashboard."""
    import uvicorn
    port = args.port or 8080
    print(f"🌐 Starting AgenticSRE Dashboard on port {port}...")
    uvicorn.run("web_app.app:app", host="0.0.0.0", port=port, reload=args.reload)


def cmd_status(args):
    """Check daemon & cluster status."""
    from tools import build_tool_registry, LLMClient
    cfg = get_config()
    registry = build_tool_registry(cfg)

    print("🏥 AgenticSRE Status Check\n")

    # K8s health
    k8s_health = registry.get("k8s_health")
    if k8s_health:
        result = k8s_health.execute()
        if result.success:
            print("✅ Kubernetes cluster:")
            data = result.data
            print(f"   Nodes: {data.get('nodes', {}).get('total', '?')}")
            print(f"   Pods: {data.get('pods', {}).get('total', '?')}")
            print(f"   Warnings: {len(data.get('warning_events', []))}")
        else:
            print(f"❌ Kubernetes: {result.error}")
    else:
        print("⚠️ K8s health tool not available")

    # Tool health
    print("\n📋 Tool Health:")
    results = registry.health_check_all()
    for name, healthy in results.items():
        icon = "✅" if healthy else "❌"
        print(f"   {icon} {name}")


def cmd_alert_scan(args):
    """Run alert compression scan."""
    from tools import build_tool_registry, LLMClient
    from agents import AlertAgent

    cfg = get_config()
    llm = LLMClient(cfg.llm)
    registry = build_tool_registry(cfg)
    agent = AlertAgent(llm, registry)

    print("🔔 Running alert compression scan...\n")
    result = agent.compress_and_recommend(
        namespace=args.namespace or "",
        time_range=args.range or "15m",
    )

    print(f"Total alerts: {result.get('total_alerts', 0)}")
    print(f"Alert groups: {result.get('num_groups', 0)}")
    print(f"Compression ratio: {result.get('compression_ratio', 0):.1%}")

    for g in result.get("groups", []):
        print(f"\n  [{g.get('severity', 'unknown')}] {g.get('group_label', 'group')}")
        print(f"    Alerts: {g.get('alert_count', 0)}")
        if g.get("root_cause_recommendation"):
            print(f"    💡 {g['root_cause_recommendation']}")


def cmd_health(args):
    """Health check all tools."""
    from tools import build_tool_registry
    cfg = get_config()
    registry = build_tool_registry(cfg)

    print("Tool Health Check\n")
    results = registry.health_check_all()
    total = len(results)
    healthy = sum(1 for v in results.values() if v)

    for name, ok in results.items():
        print(f"  {'OK' if ok else 'FAIL'} {name}")

    print(f"\n{healthy}/{total} tools healthy")


def cmd_paradigm(args):
    """Run a single paradigm on an incident query."""
    from paradigms import AgentPool, get_paradigm, list_paradigms

    cfg = get_config()
    paradigm_name = args.paradigm_name
    query = " ".join(args.query)

    if paradigm_name == "list":
        print("Available paradigms:\n")
        for p in list_paradigms():
            print(f"  {p['name']:<20} {p['description']}")
        return

    print(f"Running paradigm: {paradigm_name}")
    print(f"  Query: {query}")
    print(f"  Namespace: {args.namespace or 'all'}\n")

    pool = AgentPool(cfg)
    paradigm_cls = get_paradigm(paradigm_name)
    paradigm = paradigm_cls(pool)

    result = asyncio.run(
        paradigm.run(query, namespace=args.namespace or "",
                     log_callback=lambda msg: print(msg))
    )

    print("\n" + "=" * 60)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        print(f"\nResult saved to {args.output}")


def cmd_compare(args):
    """Compare multiple paradigms on evaluation tasks."""
    from eval.comparative_runner import ComparativeRunner

    cfg = get_config()
    paradigm_filter = args.paradigms.split(",") if args.paradigms else None

    print("Multi-Paradigm Comparison\n")
    if paradigm_filter:
        print(f"  Paradigms: {paradigm_filter}")
    else:
        print("  Paradigms: all")
    if args.task:
        print(f"  Task: {args.task}")
    print()

    runner = ComparativeRunner(cfg)
    asyncio.run(runner.run_comparison(
        paradigm_filter=paradigm_filter,
        task_filter=args.task,
        category_filter=args.category,
    ))


def cmd_feedback(args):
    """Submit expert feedback to activate supervised learning."""
    from memory.expert_feedback import ExpertFeedbackStore
    from memory.context_learner import ContextLearner
    from memory.fault_context_store import FaultContextStore
    from tools.llm_client import LLMClient

    cfg = get_config()

    # Initialize stores
    store = FaultContextStore(cfg) if cfg.memory.enabled else None
    feedback_store = ExpertFeedbackStore()

    # Initialize context learner for supervised learning
    learner = None
    if store is not None:
        try:
            llm = LLMClient(cfg.llm)
            learner = ContextLearner(llm, store, cfg)
        except Exception as e:
            print(f"  Warning: LLM unavailable, feedback stored without learning: {e}")

    result = feedback_store.submit_feedback(
        incident_id=args.incident_id,
        expert_diagnosis=args.diagnosis,
        comment=args.comment or "",
        context_learner=learner,
    )

    print(f"\n{'=' * 55}")
    print(f"  Expert Feedback Submitted")
    print(f"{'=' * 55}")
    print(f"  Feedback ID     : {result['feedback_id']}")
    print(f"  Incident ID     : {result['incident_id']}")
    print(f"  Learning Status : {result['learning_status']}")
    print(f"  Rules Generated : {result['rules_generated']}")
    print(f"{'=' * 55}")

    stats = feedback_store.get_feedback_stats()
    print(f"\n  Total feedback entries : {stats['total']}")
    print(f"  With rules generated  : {stats['with_rules']}")
    print(f"  Total rules generated : {stats['total_rules_generated']}")
    print(f"  Success rate          : {stats['success_rate']:.0%}")
    print(f"{'=' * 55}\n")


def cmd_cluster_eval(args):
    """Run E2E cluster evaluation (6 paradigms x enriched/baseline)."""
    from eval.e2e_cluster_eval import E2EClusterEval

    cfg = get_config()
    paradigm_filter = args.paradigm.split(",") if args.paradigm else None

    print("AgenticSRE E2E Cluster Evaluation\n")
    if paradigm_filter:
        print(f"  Paradigms: {paradigm_filter}")
    else:
        print("  Paradigms: all (6)")
    if args.scenario:
        print(f"  Scenario: {args.scenario}")
    if args.mode:
        print(f"  Mode: {args.mode}")
    print()

    evaluator = E2EClusterEval(cfg)
    asyncio.run(evaluator.run_full_evaluation(
        paradigm_filter=paradigm_filter,
        scenario_filter=args.scenario,
        skip_workload=args.skip_workload,
        mode_filter=args.mode,
    ))


def cmd_evolution(args):
    """Print the system evolution report."""
    from memory.evolution_tracker import EvolutionTracker

    tracker = EvolutionTracker.from_config()
    report = tracker.get_evolution_report()

    print(f"\n{'=' * 60}")
    print(f"  AgenticSRE Evolution Report")
    print(f"{'=' * 60}")

    if report.get("total_snapshots", 0) == 0:
        print(f"  {report.get('summary', 'No data.')}")
        print(f"{'=' * 60}\n")
        return

    tr = report.get("time_range", {})
    print(f"  Snapshots      : {report['total_snapshots']}")
    print(f"  Time Range     : {tr.get('first', 'N/A')} - {tr.get('last', 'N/A')}")
    print(f"  Span           : {tr.get('span_hours', 0)}h")

    trends = report.get("trends", {})

    print(f"\n{'─' * 60}")
    print(f"  Knowledge Base Growth")
    rg = trends.get("rule_growth", {})
    print(f"    Initial rules    : {rg.get('initial', 0)}")
    print(f"    Current rules    : {rg.get('current', 0)}")
    print(f"    Net growth       : +{rg.get('net_growth', 0)}")

    print(f"\n{'─' * 60}")
    print(f"  Diagnostic Confidence")
    conf = trends.get("confidence", {})
    print(f"    Average          : {conf.get('average', 0):.1%}")
    print(f"    Latest           : {conf.get('latest', 0):.1%}")
    print(f"    Trend            : {conf.get('trend', 'N/A')}")

    print(f"\n{'─' * 60}")
    print(f"  Response Latency")
    lat = trends.get("latency", {})
    print(f"    Average          : {lat.get('average_seconds', 0):.1f}s")
    print(f"    Latest           : {lat.get('latest_seconds', 0):.1f}s")

    print(f"\n{'─' * 60}")
    print(f"  Quality (Judge)")
    jq = trends.get("judge_quality", {})
    print(f"    Average score    : {jq.get('average_score', 0):.3f}")
    print(f"    Reviews needed   : {jq.get('reviews_needed', 0)}")

    print(f"\n{'─' * 60}")
    print(f"  Summary: {report.get('summary', '')}")
    print(f"{'=' * 60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="AgenticSRE — Multi-Agent Intelligent Operations System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # daemon
    p = sub.add_parser("daemon", help="Start 7x24 continuous daemon")
    p.add_argument("-n", "--namespace", default="", help="K8s namespace scope")
    p.add_argument("-i", "--interval", type=int, help="Poll interval (seconds)")

    # rca
    p = sub.add_parser("rca", help="Run single RCA analysis")
    p.add_argument("query", nargs="+", help="Incident description")
    p.add_argument("-n", "--namespace", default="", help="K8s namespace scope")
    p.add_argument("-o", "--output", help="Save result to JSON file")

    # pipeline
    p = sub.add_parser("pipeline", help="Run full 5-phase pipeline")
    p.add_argument("query", nargs="+", help="Incident description")
    p.add_argument("-n", "--namespace", default="", help="K8s namespace scope")

    # web
    p = sub.add_parser("web", help="Start web dashboard")
    p.add_argument("-p", "--port", type=int, default=8080, help="Port (default: 8080)")
    p.add_argument("--reload", action="store_true", help="Enable auto-reload")

    # status
    sub.add_parser("status", help="Check daemon / cluster status")

    # alert-scan
    p = sub.add_parser("alert-scan", help="Run alert compression scan")
    p.add_argument("-n", "--namespace", default="", help="K8s namespace scope")
    p.add_argument("-r", "--range", default="15m", help="Time range (default: 15m)")

    # health
    sub.add_parser("health", help="Health check all tools")

    # paradigm
    p = sub.add_parser("paradigm", help="Run a single paradigm (use 'list' to see all)")
    p.add_argument("paradigm_name", help="Paradigm name (chain/react/reflection/plan_and_execute/debate/voting/list)")
    p.add_argument("query", nargs="*", default=[], help="Incident description")
    p.add_argument("-n", "--namespace", default="", help="K8s namespace scope")
    p.add_argument("-o", "--output", help="Save result to JSON file")

    # compare
    p = sub.add_parser("compare", help="Compare paradigms on evaluation tasks")
    p.add_argument("--task", help="Run specific task by ID")
    p.add_argument("--category", help="Filter tasks by category")
    p.add_argument("--paradigms", help="Comma-separated paradigm names (default: all)")

    # feedback
    p = sub.add_parser("feedback", help="Submit expert feedback (activates supervised learning)")
    p.add_argument("--incident-id", required=True, help="Incident ID for feedback")
    p.add_argument("--diagnosis", required=True, help="Expert diagnosis / ground truth")
    p.add_argument("--comment", default="", help="Optional comment")

    # evolution
    sub.add_parser("evolution", help="Print system evolution report (improvement trends)")

    # cluster-eval
    p = sub.add_parser("cluster-eval", help="Run E2E cluster evaluation (6 paradigms x enriched/baseline)")
    p.add_argument("--paradigm", help="Comma-separated paradigm names (default: all)")
    p.add_argument("--scenario", help="Run specific scenario by ID")
    p.add_argument("--skip-workload", action="store_true", help="Skip background workload")
    p.add_argument("--mode", choices=["enriched", "baseline"], help="Run only one mode")

    args = parser.parse_args()
    setup_logging(args.verbose)

    commands = {
        "daemon": cmd_daemon,
        "rca": cmd_rca,
        "pipeline": cmd_pipeline,
        "paradigm": cmd_paradigm,
        "compare": cmd_compare,
        "feedback": cmd_feedback,
        "evolution": cmd_evolution,
        "web": cmd_web,
        "status": cmd_status,
        "alert-scan": cmd_alert_scan,
        "health": cmd_health,
        "cluster-eval": cmd_cluster_eval,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

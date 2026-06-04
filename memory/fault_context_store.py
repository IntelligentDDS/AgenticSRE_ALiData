"""
AgenticSRE Fault Context Store
Persistent storage for diagnostic rules and fault contexts.
Supports ChromaDB (primary) and JSON fallback.
WeRCA-style continuous fault context learning.
"""

import json
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FaultContextStore:
    """
    Persistent storage for:
    - Diagnostic rules: "If condition, then conclusion" patterns
    - Fault contexts: Historical fault records with full evidence
    
    Primary: ChromaDB with cosine similarity
    Fallback: JSON files with simple TF-IDF matching
    """

    def __init__(self, config=None):
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.mem_cfg = cfg.memory
        self.db_path = Path(self.mem_cfg.db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        
        self._backend = None
        self._init_backend()

    def _init_backend(self):
        """Initialize ChromaDB or fallback to JSON."""
        if self.mem_cfg.backend == "chromadb":
            try:
                import chromadb
                self._client = chromadb.PersistentClient(path=str(self.db_path / "chromadb"))
                self._rules_col = self._client.get_or_create_collection(
                    name=self.mem_cfg.rules_collection,
                    metadata={"hnsw:space": "cosine"},
                )
                self._faults_col = self._client.get_or_create_collection(
                    name=self.mem_cfg.faults_collection,
                    metadata={"hnsw:space": "cosine"},
                )
                self._backend = "chromadb"
                logger.info("FaultContextStore: using ChromaDB backend")
            except Exception as e:
                logger.warning(f"ChromaDB init failed: {e}, falling back to JSON")
                self._backend = "json"
        else:
            self._backend = "json"
        
        if self._backend == "json":
            self._rules_file = self.db_path / "rules.json"
            self._faults_file = self.db_path / "faults.json"
            self._load_json()

    def _load_json(self):
        """Load JSON fallback data."""
        self._rules_data = []
        self._faults_data = []
        if self._rules_file.exists():
            try:
                self._rules_data = json.loads(self._rules_file.read_text())
            except Exception:
                pass
        if self._faults_file.exists():
            try:
                self._faults_data = json.loads(self._faults_file.read_text())
            except Exception:
                pass

    def _save_json(self):
        """Save JSON fallback data."""
        self._rules_file.write_text(json.dumps(self._rules_data, indent=2, ensure_ascii=False))
        self._faults_file.write_text(json.dumps(self._faults_data, indent=2, ensure_ascii=False))

    # ── Rule Operations ──

    def add_rule(self, rule: Dict) -> str:
        """Add a diagnostic rule. Returns rule_id."""
        rule_id = f"rule-{hashlib.md5(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:10]}"
        rule["rule_id"] = rule_id
        rule["timestamp"] = time.time()
        rule.setdefault("created_at", rule["timestamp"])
        rule.setdefault("updated_at", rule["timestamp"])
        rule.setdefault("source", "unknown")
        rule.setdefault("status", "active")
        rule.setdefault("usage_count", 0)
        rule.setdefault("last_used", 0)
        rule.setdefault("quality_score", float(rule.get("confidence", 0.5) or 0.5))
        rule.setdefault("positive_votes", 0)
        rule.setdefault("negative_votes", 0)
        
        if self._backend == "chromadb":
            text = f"{rule.get('condition', '')} -> {rule.get('conclusion', '')}"
            self._rules_col.upsert(
                ids=[rule_id],
                documents=[text],
                metadatas=[{k: str(v) for k, v in rule.items() if isinstance(v, (str, int, float, bool))}],
            )
        else:
            # Check for duplicates
            if not any(r.get("rule_id") == rule_id for r in self._rules_data):
                self._rules_data.append(rule)
                self._save_json()
        
        logger.info(f"Added rule: {rule_id}")
        return rule_id

    def query_similar_rules(self, query: str, n: int = 5) -> List[Dict]:
        """Find rules similar to the query."""
        if self._backend == "chromadb":
            try:
                results = self._rules_col.query(query_texts=[query], n_results=min(n, 10))
                rules = []
                for i, doc in enumerate(results.get("documents", [[]])[0]):
                    meta = results.get("metadatas", [[]])[0][i] if results.get("metadatas") else {}
                    distance = results.get("distances", [[]])[0][i] if results.get("distances") else 0
                    rules.append({**meta, "text": doc, "similarity": 1 - distance})
                return rules
            except Exception as e:
                logger.error(f"ChromaDB query failed: {e}")
                return []
        else:
            # Simple keyword matching fallback
            query_lower = query.lower()
            scored = []
            for rule in self._rules_data:
                if rule.get("status", "active") != "active":
                    continue
                text = f"{rule.get('condition', '')} {rule.get('conclusion', '')}".lower()
                # Simple overlap score
                query_words = set(query_lower.split())
                text_words = set(text.split())
                overlap = len(query_words & text_words)
                if overlap > 0:
                    scored.append((rule, overlap / max(len(query_words), 1)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [r for r, _ in scored[:n]]

    def record_rule_usage(self, rule_ids: List[str], positive: Optional[bool] = None) -> Dict:
        """Update usage and quality metadata for rules."""
        if not rule_ids:
            return {"updated": 0}

        now = time.time()
        updated = 0
        if self._backend == "json":
            wanted = set(rule_ids)
            for rule in self._rules_data:
                if rule.get("rule_id") not in wanted:
                    continue
                rule["usage_count"] = int(rule.get("usage_count", 0)) + 1
                rule["last_used"] = now
                rule["updated_at"] = now
                if positive is True:
                    rule["positive_votes"] = int(rule.get("positive_votes", 0)) + 1
                elif positive is False:
                    rule["negative_votes"] = int(rule.get("negative_votes", 0)) + 1

                pos = int(rule.get("positive_votes", 0))
                neg = int(rule.get("negative_votes", 0))
                base = float(rule.get("confidence", rule.get("quality_score", 0.5)) or 0.5)
                if pos + neg:
                    vote_score = pos / max(pos + neg, 1)
                    rule["quality_score"] = round(0.6 * base + 0.4 * vote_score, 3)
                updated += 1
            if updated:
                self._save_json()
            return {"updated": updated}

        # ChromaDB stores scalar metadata only; mirror the JSON-backend vote/quality update.
        try:
            result = self._rules_col.get(ids=rule_ids, include=["metadatas", "documents"])
            ids = result.get("ids", [])
            metadatas = result.get("metadatas", [])
            documents = result.get("documents", [])
            for idx, rule_id in enumerate(ids):
                meta = dict(metadatas[idx] or {})
                meta["usage_count"] = str(int(float(meta.get("usage_count", 0) or 0)) + 1)
                meta["last_used"] = str(now)
                meta["updated_at"] = str(now)

                pos = int(float(meta.get("positive_votes", 0) or 0))
                neg = int(float(meta.get("negative_votes", 0) or 0))
                if positive is True:
                    pos += 1
                elif positive is False:
                    neg += 1
                meta["positive_votes"] = str(pos)
                meta["negative_votes"] = str(neg)

                base = float(meta.get("confidence", meta.get("quality_score", 0.5)) or 0.5)
                if pos + neg:
                    vote_score = pos / (pos + neg)
                    meta["quality_score"] = str(round(0.6 * base + 0.4 * vote_score, 3))

                self._rules_col.upsert(ids=[rule_id], documents=[documents[idx]], metadatas=[meta])
                updated += 1
        except Exception as e:
            logger.warning("record_rule_usage failed: %s", e)
        return {"updated": updated}

    def governance_report(self, stale_after_days: int = 30) -> Dict:
        """Summarize memory health, stale rules, and potential conflicts."""
        rules = self.list_rules(limit=1000)
        faults = self.list_faults(limit=1000)
        now = time.time()
        stale_cutoff = now - stale_after_days * 86400

        stale = [
            r for r in rules
            if float(r.get("last_used", 0) or 0) < stale_cutoff
            and float(r.get("timestamp", now) or now) < stale_cutoff
        ]
        low_quality = [
            r for r in rules
            if float(r.get("quality_score", r.get("confidence", 1)) or 0) < 0.45
        ]

        buckets: Dict[str, List[Dict]] = {}
        for rule in rules:
            key = f"{rule.get('namespace', 'general')}::{rule.get('fault_type', '')}::{rule.get('condition', '')[:60].lower()}"
            buckets.setdefault(key, []).append(rule)
        conflicts = []
        for items in buckets.values():
            conclusions = {str(r.get("conclusion", "")).strip().lower() for r in items if r.get("conclusion")}
            if len(items) > 1 and len(conclusions) > 1:
                conflicts.append({
                    "condition": items[0].get("condition", ""),
                    "rule_ids": [r.get("rule_id") for r in items],
                    "conclusions": list(conclusions)[:5],
                })

        active = sum(1 for r in rules if r.get("status", "active") == "active")
        reviewed = sum(1 for r in rules if r.get("source") in {"manual", "supervised"})
        quality_values = [float(r.get("quality_score", r.get("confidence", 0)) or 0) for r in rules]
        avg_quality = sum(quality_values) / max(len(quality_values), 1)

        return {
            "backend": self._backend,
            "rules_count": len(rules),
            "faults_count": len(faults),
            "active_rules": active,
            "reviewed_rules": reviewed,
            "avg_quality_score": round(avg_quality, 3),
            "stale_rules": len(stale),
            "low_quality_rules": len(low_quality),
            "conflicts": conflicts[:20],
            "health_score": round(max(0.0, min(1.0, 0.45 + 0.25 * avg_quality + 0.2 * (reviewed / max(len(rules), 1)) - 0.05 * len(conflicts))), 3),
        }

    def list_rules(self, limit: int = 500) -> List[Dict]:
        """Return stored rules across backends."""
        if self._backend == "json":
            return list(self._rules_data[-limit:])
        try:
            result = self._rules_col.get(limit=limit)
            rules = []
            for i, doc_id in enumerate(result.get("ids", [])):
                meta = result.get("metadatas", [])[i] if result.get("metadatas") else {}
                doc = result.get("documents", [])[i] if result.get("documents") else ""
                rules.append({**meta, "rule_id": doc_id, "text": doc})
            return rules
        except Exception as e:
            logger.warning("list_rules failed: %s", e)
            return []

    def list_faults(self, limit: int = 500) -> List[Dict]:
        """Return stored fault contexts across backends."""
        if self._backend == "json":
            return list(self._faults_data[-limit:])
        try:
            result = self._faults_col.get(limit=limit)
            faults = []
            for i, doc_id in enumerate(result.get("ids", [])):
                meta = result.get("metadatas", [])[i] if result.get("metadatas") else {}
                doc = result.get("documents", [])[i] if result.get("documents") else ""
                faults.append({**meta, "fault_id": doc_id, "text": doc})
            return faults
        except Exception as e:
            logger.warning("list_faults failed: %s", e)
            return []

    # ── Fault Context Operations ──

    def add_fault(self, fault: Dict) -> str:
        """Add a historical fault context. Returns fault_id."""
        fault_id = f"fault-{hashlib.md5(json.dumps(fault, sort_keys=True, default=str).encode()).hexdigest()[:10]}"
        fault["fault_id"] = fault_id
        fault["timestamp"] = time.time()
        
        if self._backend == "chromadb":
            text = f"{fault.get('description', '')} {fault.get('root_cause', '')} {fault.get('fault_type', '')}"
            self._faults_col.upsert(
                ids=[fault_id],
                documents=[text],
                metadatas=[{k: str(v)[:500] for k, v in fault.items() 
                           if isinstance(v, (str, int, float, bool))}],
            )
        else:
            if not any(f.get("fault_id") == fault_id for f in self._faults_data):
                self._faults_data.append(fault)
                self._save_json()
        
        logger.info(f"Added fault context: {fault_id}")
        return fault_id

    def query_similar_faults(self, query: str, n: int = 5) -> List[Dict]:
        """Find fault contexts similar to the query."""
        if self._backend == "chromadb":
            try:
                results = self._faults_col.query(query_texts=[query], n_results=min(n, 10))
                faults = []
                for i, doc in enumerate(results.get("documents", [[]])[0]):
                    meta = results.get("metadatas", [[]])[0][i] if results.get("metadatas") else {}
                    faults.append({**meta, "text": doc})
                return faults
            except Exception as e:
                logger.error(f"ChromaDB fault query failed: {e}")
                return []
        else:
            query_lower = query.lower()
            scored = []
            for fault in self._faults_data:
                text = f"{fault.get('description', '')} {fault.get('root_cause', '')}".lower()
                query_words = set(query_lower.split())
                text_words = set(text.split())
                overlap = len(query_words & text_words)
                if overlap > 0:
                    scored.append((fault, overlap / max(len(query_words), 1)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return [r for r, _ in scored[:n]]

    def get_historical_context(self, incident_query: str) -> Dict:
        """Get combined historical context for hypothesis generation."""
        similar_rules = self.query_similar_rules(incident_query, n=self.mem_cfg.max_similar_results)
        similar_faults = self.query_similar_faults(incident_query, n=self.mem_cfg.max_similar_results)
        
        return {
            "rules": similar_rules,
            "faults": similar_faults,
            "rules_count": len(similar_rules),
            "faults_count": len(similar_faults),
        }

    def stats(self) -> Dict:
        """Get store statistics."""
        if self._backend == "chromadb":
            governance = self.governance_report()
            return {
                "backend": "chromadb",
                "rules_count": self._rules_col.count(),
                "faults_count": self._faults_col.count(),
                "health_score": governance.get("health_score", 0),
                "avg_quality_score": governance.get("avg_quality_score", 0),
                "stale_rules": governance.get("stale_rules", 0),
                "low_quality_rules": governance.get("low_quality_rules", 0),
                "conflicts": len(governance.get("conflicts", [])),
            }
        else:
            governance = self.governance_report()
            return {
                "backend": "json",
                "rules_count": len(self._rules_data),
                "faults_count": len(self._faults_data),
                "health_score": governance.get("health_score", 0),
                "avg_quality_score": governance.get("avg_quality_score", 0),
                "stale_rules": governance.get("stale_rules", 0),
                "low_quality_rules": governance.get("low_quality_rules", 0),
                "conflicts": len(governance.get("conflicts", [])),
            }

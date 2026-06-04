"""
AgenticSRE RCA Judge
Quality assessment for RCA reasoning using rule-based + LLM evaluation.
SOW: "多智能体行为的可观测性与验证技术"
"""

import re
import math
import logging
from typing import Any, Dict, List, Optional, Tuple

from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


class RCAJudge:
    """
    Quality judge for RCA reasoning output.
    Combines rule-based linguistic analysis with optional LLM evaluation.
    
    Outputs a judge_level (0-3):
        0: High quality — confident, well-reasoned
        1: Good quality — mostly confident
        2: Medium quality — some uncertainty
        3: Low quality — should flag for review
    """

    # Confidence indicators (WeRCA-style linguistic patterns)
    HIGH_CONFIDENCE_PATTERNS = [
        r"root cause is clearly",
        r"definitely caused by",
        r"strong evidence shows",
        r"confirmed that",
        r"conclusively",
        r"high confidence",
        r"multiple signals confirm",
    ]

    MEDIUM_CONFIDENCE_PATTERNS = [
        r"likely caused by",
        r"most probable",
        r"evidence suggests",
        r"consistent with",
        r"indicates that",
        r"appears to be",
    ]

    LOW_CONFIDENCE_PATTERNS = [
        r"possibly",
        r"might be",
        r"unclear",
        r"insufficient evidence",
        r"cannot determine",
        r"uncertain",
        r"further investigation needed",
        r"no clear root cause",
        r"inconclusive",
    ]

    LLM_JUDGE_PROMPT = """You are an expert evaluator of SRE root cause analysis quality.

Evaluate the following RCA reasoning for:
1. Logical coherence: Does the reasoning flow logically?
2. Evidence quality: Is the conclusion supported by concrete evidence?
3. Specificity: Is the root cause specific or vague?
4. Actionability: Can an SRE act on this analysis?

RCA Reasoning:
{reasoning}

Root Cause Conclusion:
{root_cause}

Confidence: {confidence}

Rate the quality as a level (0-3):
- 0: Excellent — well-reasoned, specific, actionable
- 1: Good — mostly sound, minor gaps
- 2: Fair — some logical gaps or vague conclusions
- 3: Poor — contradictory, vague, or unsupported

Respond in JSON:
{{
    "judge_level": 1,
    "coherence_score": 0.8,
    "evidence_score": 0.7,
    "specificity_score": 0.9,
    "actionability_score": 0.8,
    "feedback": "brief explanation of the rating"
}}"""

    def __init__(self, llm: Optional[LLMClient] = None, config=None):
        self.llm = llm
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.threshold = cfg.memory.judge_threshold
        self.llm_weight = cfg.memory.judge_llm_weight

    def judge(self, reasoning: str, root_cause: str, confidence: float) -> Dict:
        """
        Evaluate RCA quality. Returns combined judge result.
        """
        # Rule-based judge
        rule_result = self._rule_based_judge(reasoning, confidence)
        
        # LLM judge (optional)
        llm_result = None
        if self.llm:
            try:
                llm_result = self._llm_judge(reasoning, root_cause, confidence)
            except Exception as e:
                logger.warning(f"LLM judge failed: {e}")

        # Combine scores
        if llm_result and "judge_level" in llm_result:
            combined_score = (
                (1 - self.llm_weight) * rule_result["score"] +
                self.llm_weight * (1 - llm_result["judge_level"] / 3)
            )
            combined_level = self._score_to_level(combined_score)
        else:
            combined_score = rule_result["score"]
            combined_level = rule_result["level"]

        needs_review = combined_score < self.threshold

        return {
            "judge_level": combined_level,
            "combined_score": round(combined_score, 3),
            "needs_review": needs_review,
            "rule_based": rule_result,
            "llm_based": llm_result,
            "threshold": self.threshold,
        }

    def _rule_based_judge(self, reasoning: str, confidence: float) -> Dict:
        """Rule-based quality assessment using linguistic patterns."""
        text_lower = reasoning.lower()
        
        # Pattern matching
        high_count = sum(1 for p in self.HIGH_CONFIDENCE_PATTERNS if re.search(p, text_lower))
        med_count = sum(1 for p in self.MEDIUM_CONFIDENCE_PATTERNS if re.search(p, text_lower))
        low_count = sum(1 for p in self.LOW_CONFIDENCE_PATTERNS if re.search(p, text_lower))
        
        # Pattern-based score
        pattern_score = (high_count * 1.0 + med_count * 0.5 - low_count * 0.5)
        max_patterns = max(len(self.HIGH_CONFIDENCE_PATTERNS), 1)
        pattern_score = max(0, min(pattern_score / max_patterns, 1.0))
        
        # Length factor (too short = suspicious, very long = good)
        length = len(reasoning)
        length_factor = min(1.0, length / 500)  # Normalize at 500 chars
        
        # Confidence factor
        conf_factor = confidence
        
        # Sigmoid normalization of combined score
        raw_score = 0.4 * pattern_score + 0.3 * conf_factor + 0.3 * length_factor
        score = 1 / (1 + math.exp(-6 * (raw_score - 0.5)))  # Sigmoid
        
        return {
            "score": round(score, 3),
            "level": self._score_to_level(score),
            "high_patterns": high_count,
            "medium_patterns": med_count,
            "low_patterns": low_count,
            "length": length,
        }

    def _llm_judge(self, reasoning: str, root_cause: str, confidence: float) -> Dict:
        """LLM-based quality evaluation."""
        result = self.llm.json_chat([
            {"role": "system", "content": "You are an expert RCA quality evaluator."},
            {"role": "user", "content": self.LLM_JUDGE_PROMPT.format(
                reasoning=reasoning[:4000],
                root_cause=root_cause[:500],
                confidence=confidence,
            )}
        ])
        return result

    def _score_to_level(self, score: float) -> int:
        """Convert score to judge level (0-3)."""
        if score >= 0.8:
            return 0
        elif score >= 0.6:
            return 1
        elif score >= 0.4:
            return 2
        else:
            return 3

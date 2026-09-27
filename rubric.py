"""
Vera Elite — Local Quality Rubric
================================
Deterministic 5-dimension scorer used by the web console.

The scoring maths deliberately mirrors `LocalHeuristicProvider` inside
`judge_simulator.py` so the live score shown in the console is directly
comparable with the official harness output — and, unlike the LLM judge,
it needs no API key, so the console works fully offline.

The five dimensions match the official magicpin challenge rubric:
  1. Specificity & Grounding          2. Category Voice & Taboo Fit
  3. Merchant Context Personalization 4. Decision & Routing Quality
  5. Engagement Compulsion & CTA
"""

import re
from typing import Any, Dict, List, Optional

MAX_PER_DIMENSION = 10
MAX_TOTAL = MAX_PER_DIMENSION * 5

# Phrases that signal the bot stalled into qualification instead of acting.
QUALIFYING_PHRASES = ["would you", "do you", "can you tell", "what if", "how about"]

# Phrases that signal action mode (intent handoff succeeded).
ACTIONING_PHRASES = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]

# Verifiable source citations. Kept aligned with the judge harness list.
CITATION_RE = re.compile(
    r"\b(JIDA|DCI|IDA|DGCI|GST|Practo|circular|meta-analysis|journal|issue|guideline)\b",
    re.IGNORECASE,
)
PRICE_RE = re.compile(r"₹\s?\d[\d,]*")
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")
PERCENT_RE = re.compile(r"[+-]?\d+(?:\.\d+)?%")


def _identity(merchant: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (merchant or {}).get("identity", {}) or {}


def extract_facts(body: str) -> Dict[str, List[str]]:
    """
    Pull the verifiable anchors out of a composed body.
    Used by the console to prove *where* each number came from.
    """
    return {
        "numbers": [m.group(0) for m in NUMBER_RE.finditer(body)],
        "percentages": [m.group(0) for m in PERCENT_RE.finditer(body)],
        "prices": [m.group(0) for m in PRICE_RE.finditer(body)],
        "citations": [m.group(0) for m in CITATION_RE.finditer(body)],
    }


def is_qualification_free(body: str) -> bool:
    """True when the message moves straight to execution with no stalling question."""
    lowered = (body or "").lower()
    return not any(p in lowered for p in QUALIFYING_PHRASES)


def is_actioning(body: str) -> bool:
    """True when the message is in action mode (intent handoff succeeded)."""
    lowered = (body or "").lower()
    return any(p in lowered for p in ACTIONING_PHRASES)


def score_message(
    body: str,
    cta: str = "none",
    category: Optional[Dict[str, Any]] = None,
    merchant: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Score a composed message across the 5 official rubric dimensions.

    Returns a JSON-serialisable dict with per-dimension score + reason,
    the extracted verifiable facts, and a total out of 50.
    """
    body = body or ""
    category = category or {}
    merchant = merchant or {}

    facts = extract_facts(body)
    num_count = len(facts["numbers"])
    has_citation = bool(facts["citations"])
    has_price = bool(facts["prices"])

    # 1. Specificity — verifiable quantitative anchors + a citation or a price.
    specificity = min(MAX_PER_DIMENSION, 4 + min(4, num_count) + (2 if (has_citation or has_price) else 0))
    spec_reason = f"{num_count} verifiable quantitative anchor(s), "
    spec_reason += "with a source citation / price." if (has_citation or has_price) else "but no citation or price anchor."

    # 2. Category fit — taboo vocabulary must be absent.
    taboos = [t.split("(")[0].strip().lower() for t in category.get("voice", {}).get("vocab_taboo", [])]
    taboo_hit = next((t for t in taboos if t and t in body.lower()), None)
    category_fit = 6 if taboo_hit else MAX_PER_DIMENSION
    tone = category.get("voice", {}).get("tone", "uncalibrated")
    if taboo_hit:
        cat_reason = f"Taboo phrase '{taboo_hit}' breaks the {tone} register."
    else:
        cat_reason = f"{tone} register respected; no taboo vocabulary used."

    # 3. Merchant fit — owner first name or business name must appear.
    owner = _identity(merchant).get("owner_first_name", "")
    business = _identity(merchant).get("name", "")
    lowered = body.lower()
    has_owner = bool(owner) and owner.lower() in lowered
    has_business = bool(business) and business.lower() in lowered
    merchant_fit = MAX_PER_DIMENSION if (has_owner or has_business) else 8
    if has_owner:
        fit_reason = f"Addresses the owner by name ({owner})."
    elif has_business:
        fit_reason = "Names the business explicitly."
    else:
        fit_reason = "No owner or business name in the body — generic."

    # 4. Decision quality — the message must actually resolve to something.
    decision = MAX_PER_DIMENSION if len(body) > 30 else 6
    dec_reason = (
        "Resolves the trigger into a concrete, executable next step."
        if decision == MAX_PER_DIMENSION
        else "Body too short to carry a grounded decision."
    )

    # 5. Engagement compulsion — a low-friction ask plus a real CTA.
    has_question = "?" in body or "reply" in lowered
    engagement = MAX_PER_DIMENSION if (has_question and cta != "none") else 8
    eng_reason = (
        f"Single-binary CTA ({cta}) closes a low-friction ask."
        if engagement == MAX_PER_DIMENSION
        else "CTA is missing or the ask is not frictionless."
    )

    dimensions = {
        "specificity": {"score": specificity, "reason": spec_reason},
        "category_fit": {"score": category_fit, "reason": cat_reason},
        "merchant_fit": {"score": merchant_fit, "reason": fit_reason},
        "decision_quality": {"score": decision, "reason": dec_reason},
        "engagement": {"score": engagement, "reason": eng_reason},
    }

    total = sum(d["score"] for d in dimensions.values())
    pct = (total / MAX_TOTAL) * 100

    if pct >= 80:
        verdict = "EXCELLENT"
    elif pct >= 60:
        verdict = "GOOD"
    elif pct >= 40:
        verdict = "NEEDS IMPROVEMENT"
    else:
        verdict = "BELOW EXPECTATIONS"

    return {
        "total": total,
        "max": MAX_TOTAL,
        "pct": round(pct, 1),
        "verdict": verdict,
        "dimensions": dimensions,
        "facts": facts,
        "taboo_hit": taboo_hit,
        "qualification_free": is_qualification_free(body),
        "actioning": is_actioning(body),
    }


def aggregate(scores: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Roll a list of score_message() results into a console-friendly average.
    """
    if not scores:
        return {
            "total": 0, "max": MAX_TOTAL, "pct": 0.0, "verdict": "NO DATA",
            "samples": 0, "dimensions": {}, "fact_coverage": {"numbers": 0, "percentages": 0, "prices": 0, "citations": 0},
        }

    keys = ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement"]
    dims: Dict[str, Any] = {}
    total = 0
    for key in keys:
        avg = round(sum(s["dimensions"][key]["score"] for s in scores) / len(scores), 1)
        dims[key] = {"score": avg, "max": MAX_PER_DIMENSION}
        total += avg

    coverage = {k: sum(len(s["facts"][k]) for s in scores) for k in ("numbers", "percentages", "prices", "citations")}
    pct = (total / MAX_TOTAL) * 100

    if pct >= 80:
        verdict = "EXCELLENT"
    elif pct >= 60:
        verdict = "GOOD"
    elif pct >= 40:
        verdict = "NEEDS IMPROVEMENT"
    else:
        verdict = "BELOW EXPECTATIONS"

    return {
        "total": round(total, 1),
        "max": MAX_TOTAL,
        "pct": round(pct, 1),
        "verdict": verdict,
        "samples": len(scores),
        "dimensions": dims,
        "fact_coverage": coverage,
    }

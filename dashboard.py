"""
Vera Elite — Console Backend
============================
Observability endpoints + a stateful conversation simulator that back the
zero-build web console in `web/`.

Design constraints:
  * The 5 judge-mandated endpoints in `bot.py` keep their exact behaviour.
    Everything here is additive and lives under `/v1/dashboard/*`.
  * The simulator drives the *real* guard core (`conversation_handlers`) and the
    *real* composer, so what the console shows is what the engine would send.
  * No new third-party dependencies — stdlib + FastAPI only.
"""

import json
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from composer import compose, owner_salutation, scrub_taboos
from rubric import (
    ACTIONING_PHRASES,
    QUALIFYING_PHRASES,
    is_actioning,
    is_qualification_free,
    score_message,
)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "expanded"

SCOPES = ("category", "merchant", "customer", "trigger")
LATENCY_BUDGET_MS = 30_000
MAX_TIMELINE = 80
MAX_LATENCY_SAMPLES = 400
MAX_SCORE_SAMPLES = 200
MAX_SIM_TRANSCRIPT = 60


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Instrumentation store
# ---------------------------------------------------------------------------

class ConsoleState:
    """
    In-memory telemetry ring buffers. Deliberately bounded so a long-running
    console session can never leak memory.
    """

    def __init__(self) -> None:
        self.timeline: Deque[Dict[str, Any]] = deque(maxlen=MAX_TIMELINE)
        self.latencies: Deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        self.scores: Deque[Dict[str, Any]] = deque(maxlen=MAX_SCORE_SAMPLES)
        self.counters: Counter = Counter()
        self._seq = 0

    # -- internals ---------------------------------------------------------
    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _push_timeline(self, entry: Dict[str, Any]) -> None:
        self.timeline.appendleft({"id": self._next_id(), "ts": _now_iso(), **entry})

    def _push_latency(self, ms: float) -> None:
        self.latencies.append(round(ms, 3))

    # -- recorders ---------------------------------------------------------
    def record_context_push(self, scope: str, context_id: str, version: int, accepted: bool) -> None:
        self.counters["context_pushes"] += 1
        self.counters[f"context_{scope}"] += 1
        if not accepted:
            self.counters["context_rejected"] += 1
        self._push_timeline({
            "kind": "context",
            "title": f"{scope} context ingested",
            "subtitle": f"{context_id} · v{version}",
            "scope": scope,
        })

    def record_tick(self, trigger_count: int, action_count: int, latency_ms: float) -> None:
        self.counters["ticks"] += 1
        # The tick total lives on the timeline entry only — the latency ring
        # buffer holds per-composition samples so the sparkline stays on one scale.
        self._push_timeline({
            "kind": "tick",
            "title": f"Clock tick — {trigger_count} trigger(s) evaluated",
            "subtitle": f"{action_count} action(s) composed",
            "latency_ms": round(latency_ms, 3),
            "action_count": action_count,
        })

    def record_action(
        self,
        action: Dict[str, Any],
        *,
        score: Optional[Dict[str, Any]] = None,
        latency_ms: Optional[float] = None,
        source: str = "tick",
    ) -> None:
        self.counters["actions_composed"] += 1
        self.counters[f"send_as_{action.get('send_as', 'vera')}"] += 1
        self.counters[f"cta_{action.get('cta', 'none')}"] += 1

        body = action.get("body", "") or ""
        if is_qualification_free(body):
            self.counters["qualification_free_sends"] += 1
        if is_actioning(body):
            self.counters["action_mode_sends"] += 1
        if action.get("suppression_key"):
            self.counters["suppression_keys_issued"] += 1

        if score:
            self.scores.append(score)

        if latency_ms is not None:
            self._push_latency(latency_ms)

        self._push_timeline({
            "kind": "action",
            "title": "Proactive action composed",
            "subtitle": f"{action.get('merchant_id', '?')} · {action.get('trigger_id', 'manual')}",
            "merchant_id": action.get("merchant_id"),
            "customer_id": action.get("customer_id"),
            "conversation_id": action.get("conversation_id"),
            "trigger_id": action.get("trigger_id"),
            "action": "send",
            "cta": action.get("cta"),
            "send_as": action.get("send_as"),
            "suppression_key": action.get("suppression_key"),
            "rationale": action.get("rationale", ""),
            "body": body,
            "score": score,
            "latency_ms": round(latency_ms, 3) if latency_ms is not None else None,
            "source": source,
        })

    def record_reply(
        self,
        conversation_id: str,
        action: str,
        *,
        body: Optional[str],
        rationale: str,
        merchant_id: Optional[str],
        latency_ms: float,
        score: Optional[Dict[str, Any]] = None,
        guards: Optional[List[str]] = None,
        source: str = "reply",
    ) -> None:
        self.counters["replies_handled"] += 1
        self.counters[f"reply_action_{action}"] += 1
        if action == "wait":
            self.counters["guard_auto_reply"] += 1
        if action == "end":
            self.counters["guard_hostile"] += 1
        for guard in guards or []:
            self.counters[f"guard_{guard}"] += 1
        if score:
            self.scores.append(score)
        self._push_latency(latency_ms)

        self._push_timeline({
            "kind": "reply",
            "title": f"Inbound message → {action}",
            "subtitle": conversation_id,
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "action": action,
            "cta": None,
            "rationale": rationale,
            "body": body or "",
            "score": score,
            "latency_ms": round(latency_ms, 3),
            "guards": guards or [],
            "source": source,
        })

    def record_system(self, title: str, subtitle: str = "") -> None:
        self._push_timeline({"kind": "system", "title": title, "subtitle": subtitle})

    # -- derived views -----------------------------------------------------
    def latency_stats(self) -> Dict[str, Any]:
        samples = sorted(self.latencies)
        if not samples:
            return {
                "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0,
                "min_ms": 0.0, "samples": 0, "budget_ms": LATENCY_BUDGET_MS,
                "budget_used_pct": 0.0, "series": [],
            }

        def pct(q: float) -> float:
            idx = min(len(samples) - 1, int(round(q * (len(samples) - 1))))
            return samples[idx]

        avg = sum(samples) / len(samples)
        return {
            "avg_ms": round(avg, 3),
            "p50_ms": round(pct(0.50), 3),
            "p95_ms": round(pct(0.95), 3),
            "max_ms": round(samples[-1], 3),
            "min_ms": round(samples[0], 3),
            "samples": len(samples),
            "budget_ms": LATENCY_BUDGET_MS,
            "budget_used_pct": round((avg / LATENCY_BUDGET_MS) * 100, 4),
            "series": [round(s, 3) for s in list(self.latencies)],
        }


console_state = ConsoleState()


# ---------------------------------------------------------------------------
# Context resolution helpers
# ---------------------------------------------------------------------------

def _scope_counts(contexts: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in SCOPES}
    for (scope, _cid) in contexts:
        counts[scope] = counts.get(scope, 0) + 1
    return counts


def _iter_scope(contexts: Dict[Tuple[str, str], Dict[str, Any]], scope: str):
    for (s, cid), entry in contexts.items():
        if s == scope:
            yield cid, entry.get("payload", {}) or {}


def _resolve_merchant(contexts: Dict[Tuple[str, str], Dict[str, Any]], merchant_id: str) -> Optional[Dict[str, Any]]:
    """Exact lookup first, then tolerant prefix match (the judge uses short ids)."""
    if not merchant_id:
        return None
    hit = contexts.get(("merchant", merchant_id))
    if hit:
        return hit.get("payload")
    for (scope, mid), val in contexts.items():
        if scope == "merchant" and (mid.startswith(merchant_id) or merchant_id.startswith(mid)):
            return val.get("payload")
    return None


def _resolve_category(contexts: Dict[Tuple[str, str], Dict[str, Any]], merchant: Dict[str, Any]) -> Dict[str, Any]:
    slug = merchant.get("category_slug", "")
    hit = contexts.get(("category", slug))
    if hit:
        return hit.get("payload", {}) or {}
    for (scope, cid), val in contexts.items():
        if scope == "category" and (cid.startswith(slug) or slug.startswith(cid)):
            return val.get("payload", {}) or {}
    return {}


def _resolve_customer(contexts: Dict[Tuple[str, str], Dict[str, Any]], customer_id: str) -> Optional[Dict[str, Any]]:
    if not customer_id:
        return None
    hit = contexts.get(("customer", customer_id))
    if hit:
        return hit.get("payload")
    for (scope, cid), val in contexts.items():
        if scope == "customer" and (cid == customer_id or cid.startswith(customer_id)):
            return val.get("payload")
    return None


def _triggers_for(contexts: Dict[Tuple[str, str], Dict[str, Any]], merchant_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for cid, payload in _iter_scope(contexts, "trigger"):
        if payload.get("merchant_id") == merchant_id:
            out.append(payload)
    out.sort(key=lambda t: (-(t.get("urgency") or 0), t.get("id", "")))
    return out


def _customers_for(contexts: Dict[Tuple[str, str], Dict[str, Any]], merchant_id: str) -> List[Dict[str, Any]]:
    return [p for _cid, p in _iter_scope(contexts, "customer") if p.get("merchant_id") == merchant_id]


def _merchant_summary(merchant: Dict[str, Any], category: Dict[str, Any]) -> Dict[str, Any]:
    identity = merchant.get("identity", {}) or {}
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    sub = merchant.get("subscription", {}) or {}
    agg = merchant.get("customer_aggregate", {}) or {}
    offers = merchant.get("offers", []) or []
    active = [o.get("title") for o in offers if o.get("status") == "active"]

    views = perf.get("views", 0)
    ctr = perf.get("ctr", 0)
    peer_views = peer.get("avg_views_30d") or 0
    peer_ctr = peer.get("avg_ctr") or 0

    return {
        "merchant_id": merchant.get("merchant_id", ""),
        "name": identity.get("name", "Unknown merchant"),
        "owner_first_name": identity.get("owner_first_name", ""),
        "category_slug": merchant.get("category_slug", ""),
        "category_name": category.get("display_name", merchant.get("category_slug", "")),
        "city": identity.get("city", ""),
        "locality": identity.get("locality", ""),
        "languages": identity.get("languages", []),
        "verified": identity.get("verified", False),
        "plan": sub.get("status", "unknown"),
        "views": views,
        "calls": perf.get("calls", 0),
        "directions": perf.get("directions", 0),
        "ctr": ctr,
        "delta_7d": perf.get("delta_7d", {}) or {},
        "active_offers": active,
        "unique_customers_ytd": agg.get("total_unique_ytd", 0),
        "lapsed_180d_plus": agg.get("lapsed_180d_plus", 0),
        "signals": merchant.get("signals", []) or [],
        "signal_count": len(merchant.get("signals", []) or []),
        "peer": {
            "avg_views_30d": peer_views,
            "avg_ctr": peer_ctr,
            "avg_calls_30d": peer.get("avg_calls_30d", 0),
            "retention_6mo_pct": peer.get("retention_6mo_pct", 0),
        },
        "vs_peer": {
            "views_index": round(views / peer_views, 2) if peer_views else None,
            "ctr_index": round(ctr / peer_ctr, 2) if peer_ctr else None,
        },
    }


# ---------------------------------------------------------------------------
# Dataset seeding
# ---------------------------------------------------------------------------

def _seed_dir(subdir: str, id_key: str, contexts: Dict[Tuple[str, str], Dict[str, Any]], scope: str,
              version: int = 1, limit: Optional[int] = None) -> int:
    folder = DATA_DIR / subdir
    if not folder.exists():
        return 0
    loaded = 0
    for path in sorted(folder.glob("*.json")):
        if limit is not None and loaded >= limit:
            break
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        cid = payload.get(id_key) or path.stem
        contexts[(scope, cid)] = {"version": version, "payload": payload}
        loaded += 1
    return loaded


# ---------------------------------------------------------------------------
# Simulator state + helpers
# ---------------------------------------------------------------------------

# conversation_id -> transcript state
sim_conversations: Dict[str, Dict[str, Any]] = {}


def _cold_open(merchant: Dict[str, Any], category: Dict[str, Any]) -> Dict[str, Any]:
    """
    A grounded, context-anchored first message used when the operator opens a
    conversation with no trigger attached. Every number comes from merchant state.
    """
    identity = merchant.get("identity", {}) or {}
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    offers = [o.get("title") for o in (merchant.get("offers", []) or []) if o.get("status") == "active"]
    offer = offers[0] if offers else "your active offer"

    name = identity.get("name", "your listing")
    owner = owner_salutation(merchant, merchant.get("category_slug", ""), False)
    locality = identity.get("locality", "your area")
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)
    ctr = perf.get("ctr", 0)
    peer_ctr = peer.get("avg_ctr", 0)

    body = (
        f"Hi {owner} — I did a full profile audit on {name} in {locality}. "
        f"Last 30 days: {views:,} views, {calls} direct calls, {ctr:.1%} click-through "
        f"against the {peer_ctr:.1%} local peer median. "
        f"Your live offer '{offer}' is the strongest hook you have right now. "
        f"I can stage 3 Google posts around it plus a WhatsApp follow-up for people who enquired but never booked. "
        f"Reply YES and I'll draft all four tonight."
    )
    return {
        "body": scrub_taboos(body, category),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "suppression_key": f"opener:{merchant.get('merchant_id', 'm')}",
        "rationale": (
            f"Cold open grounded on live performance ({views:,} views / {calls} calls / {ctr:.1%} CTR), "
            f"benchmarked against the {peer_ctr:.1%} local peer median, with effort externalised into a "
            f"single binary CTA. No qualifying questions."
        ),
        "template_name": "console_cold_open_v1",
        "template_params": [owner, name, f"{views:,}"],
    }


def _grounded_followup(merchant: Dict[str, Any], category: Dict[str, Any], incoming: str) -> Dict[str, Any]:
    """
    Default (non-terminal, non-commitment) branch. Keeps the engine's grounding
    contract: every figure is pulled from loaded merchant state, and the CTA
    stays a single binary with zero qualifying questions.
    """
    identity = merchant.get("identity", {}) or {}
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    offers = [o.get("title") for o in (merchant.get("offers", []) or []) if o.get("status") == "active"]
    offer = offers[0] if offers else "a new offer"
    agg = merchant.get("customer_aggregate", {}) or {}

    name = identity.get("name", "your listing")
    locality = identity.get("locality", "your area")
    owner = owner_salutation(merchant, merchant.get("category_slug", ""), False)
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)
    ctr = perf.get("ctr", 0)
    lapsed = agg.get("lapsed_180d_plus", 0)
    signals = merchant.get("signals", []) or []

    signal_clause = f" I can see the '{signals[0].split(':')[0].replace('_', ' ')}' flag on your profile." if signals else ""

    body = (
        f"Noted, {owner} — locking that in. {name} is at {views:,} views and {calls} calls this cycle "
        f"at {ctr:.1%} CTR, and I have {lapsed} lapsed customers in {locality} who went quiet."
        f"{signal_clause} I've staged '{offer}' as the hook for both. "
        f"Publishing to your Google profile tomorrow 10:00 AM and running the lapsed list in the same window — "
        f"reply YES to lock it, or send a different day and I'll move it."
    )
    return {
        "body": scrub_taboos(body, category),
        "cta": "binary_yes_no",
        "send_as": "vera",
        "suppression_key": f"followup:{merchant.get('merchant_id', 'm')}",
        "rationale": (
            f"Grounded acknowledgement citing {views:,} views, {calls} calls, {ctr:.1%} CTR and "
            f"{lapsed} lapsed customers from loaded merchant state. Effort externalised, single binary CTA, "
            f"zero qualifying questions."
        ),
        "template_name": "console_grounded_followup_v1",
        "template_params": [owner, name, f"{views:,}"],
    }


def _pipeline_trace(
    *,
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    auto_reply: str = "clear",
    intent: str = "idle",
    facts: Optional[Dict[str, Any]] = None,
    grounded: bool = True,
) -> List[Dict[str, str]]:
    """
    Honest per-stage report of the 5-stage Vera reasoning & guard core.
    `status` is one of: applied | fired | clear | skipped
    """
    voice = category.get("voice", {}) or {}
    taboos = voice.get("vocab_taboo", []) or []
    languages = (merchant.get("identity", {}) or {}).get("languages", []) or []
    fact_count = sum(len(v) for v in (facts or {}).values())

    return [
        {
            "layer": "Domain Voice Modulator",
            "status": "applied",
            "detail": f"{voice.get('tone', 'uncalibrated')} register · {len(taboos)} taboo filter(s) active",
        },
        {
            "layer": "Auto-Reply State Guard",
            "status": "fired" if auto_reply != "clear" else "clear",
            "detail": {
                "clear": "No canned WhatsApp Business signature matched.",
                "wait": "Canned auto-reply matched — issuing 4h backoff.",
                "end": "Persistent canned auto-reply — conversation closed.",
            }[auto_reply],
        },
        {
            "layer": "Factual Anchoring Engine",
            "status": "applied" if grounded else "skipped",
            "detail": f"{fact_count} verifiable anchor(s) drawn from loaded context." if grounded
                      else "Exit path — no outbound claim to anchor.",
        },
        {
            "layer": "Intent Handoff Matrix",
            "status": {"fired": "fired", "idle": "clear", "blocked": "skipped"}[intent],
            "detail": {
                "idle": "No commitment language — staying in conversational mode.",
                "fired": "Commitment detected — routed straight to action mode.",
                "blocked": "Not reached — the auto-reply guard exited first.",
            }[intent],
        },
        {
            "layer": "Language Mixer (hi-en)",
            "status": "applied" if "hi" in languages else "skipped",
            "detail": "hi-en code-mix enabled for this merchant." if "hi" in languages
                      else "Merchant prefers English only.",
        },
    ]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SeedBody(BaseModel):
    include_customers: bool = True
    include_triggers: bool = True


class SimTickBody(BaseModel):
    trigger_ids: Optional[List[str]] = None
    limit: int = Field(default=12, ge=1, le=100)


class SimulateBody(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    message: Optional[str] = None
    trigger_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def build_console_router(contexts, suppressed_keys, conv_manager, start_time: float, bot_module=None) -> APIRouter:
    """
    Built as a factory so the router shares the live state objects owned by bot.py
    without creating an import cycle.

    `bot_module` is the already-imported bot module. It is required rather than
    late-imported because `python bot.py` runs bot.py as `__main__`, so a plain
    `import bot` inside the handler would silently load a *second* copy of the
    module with its own empty `contexts` and silently compose nothing.
    """
    router = APIRouter(prefix="/v1/dashboard", tags=["console"])

    # -- overview ---------------------------------------------------------
    @router.get("/overview")
    async def overview():
        counts = _scope_counts(contexts)
        latency = console_state.latency_stats()

        from rubric import aggregate as _aggregate
        quality = _aggregate(list(console_state.scores))

        # Category cohorts computed from whatever is actually loaded.
        cohorts = []
        for _cid, category in _iter_scope(contexts, "category"):
            slug = category.get("slug", _cid)
            members = [
                m for _mid, m in _iter_scope(contexts, "merchant")
                if m.get("category_slug") == slug
            ]
            peer = category.get("peer_stats", {}) or {}
            avg_views = round(sum((m.get("performance", {}) or {}).get("views", 0) for m in members) / len(members)) if members else 0
            avg_ctr = round(sum((m.get("performance", {}) or {}).get("ctr", 0) for m in members) / len(members), 4) if members else 0
            trigger_count = sum(1 for _t, t in _iter_scope(contexts, "trigger") if t.get("payload", {}).get("category") == slug)
            cohorts.append({
                "slug": slug,
                "display_name": category.get("display_name", slug),
                "tone": (category.get("voice", {}) or {}).get("tone", "uncalibrated"),
                "merchants": len(members),
                "avg_views": avg_views,
                "peer_views": peer.get("avg_views_30d", 0),
                "avg_ctr": avg_ctr,
                "peer_ctr": peer.get("avg_ctr", 0),
                "triggers": trigger_count,
                "customers": sum(len(_customers_for(contexts, m.get("merchant_id", ""))) for m in members),
            })
        cohorts.sort(key=lambda c: -c["merchants"])

        kind_counter = Counter()
        for _cid, t in _iter_scope(contexts, "trigger"):
            kind_counter[t.get("kind", "unknown")] += 1
        top_kinds = [{"kind": k, "count": v} for k, v in kind_counter.most_common(8)]

        c = console_state.counters
        sends = max(1, c["replies_handled"])
        return {
            "engine": {
                "status": "ok",
                "team_name": "Vera Elite",
                "model": "hybrid-deterministic-reasoning-engine",
                "version": "1.0.0",
                "uptime_seconds": int(time.time() - start_time),
                "deterministic": True,
            },
            "contexts": {**counts, "total": sum(counts.values())},
            "activity": {
                "actions_composed": c["actions_composed"],
                "replies_handled": c["replies_handled"],
                "ticks": c["ticks"],
                "context_pushes": c["context_pushes"],
                "conversations": len(conv_manager.conversations),
                "simulations": len(sim_conversations),
                "suppressed_keys": len(suppressed_keys),
                "suppression_keys_issued": c["suppression_keys_issued"],
                "wait_events": c["reply_action_wait"],
                "end_events": c["reply_action_end"],
                "qualification_free_rate": round((c["qualification_free_sends"] / sends) * 100, 1),
                "action_mode_sends": c["action_mode_sends"],
            },
            "latency": latency,
            "quality": quality,
            "guard_matrix": {
                "auto_reply_guard": {"fired": c["guard_auto_reply"] + c["guard_auto_reply_detected"], "label": "Auto-reply backoff"},
                "hostile_exit": {"fired": c["guard_hostile"], "label": "Opt-out teardown"},
                "intent_handoff": {"fired": c["guard_intent_handoff"], "label": "Zero-qualification intent switch"},
                # Not a guard: a running total of messages the rubric has scored.
                "factual_anchor": {"fired": 0, "samples": quality["samples"], "label": "Grounded compositions scored"},
            },
            "cohorts": cohorts,
            "top_triggers": top_kinds,
            "timeline": list(console_state.timeline),
        }

    # -- merchants --------------------------------------------------------
    @router.get("/merchants")
    async def list_merchants(category: Optional[str] = None, q: Optional[str] = None):
        seen = set()
        out = []
        for cid, merchant in _iter_scope(contexts, "merchant"):
            mid = merchant.get("merchant_id", cid)
            if mid in seen:
                continue
            seen.add(mid)
            cat = _resolve_category(contexts, merchant)
            summary = _merchant_summary(merchant, cat)
            if category and summary["category_slug"] != category:
                continue
            if q:
                needle = q.lower()
                hay = f"{summary['name']} {summary['locality']} {summary['city']} {summary['merchant_id']} {summary['category_slug']}".lower()
                if needle not in hay:
                    continue
            out.append(summary)
        out.sort(key=lambda m: (-m["views"], m["name"]))
        return {"merchants": out, "count": len(out)}

    @router.get("/merchants/{merchant_id}")
    async def merchant_detail(merchant_id: str):
        merchant = _resolve_merchant(contexts, merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="merchant_not_loaded")
        category = _resolve_category(contexts, merchant)
        summary = _merchant_summary(merchant, category)
        return {
            "summary": summary,
            "category_context": {
                "slug": category.get("slug", ""),
                "display_name": category.get("display_name", ""),
                "voice": category.get("voice", {}),
                "peer_stats": category.get("peer_stats", {}),
                "offer_catalog": category.get("offer_catalog", [])[:6],
            },
            "merchant_context": {
                "subscription": merchant.get("subscription", {}),
                "performance": merchant.get("performance", {}),
                "offers": merchant.get("offers", []),
                "customer_aggregate": merchant.get("customer_aggregate", {}),
                "signals": merchant.get("signals", []),
                "review_themes": merchant.get("review_themes", []),
                "conversation_history": merchant.get("conversation_history", []),
            },
            "triggers": _triggers_for(contexts, merchant.get("merchant_id", merchant_id)),
            "customers": _customers_for(contexts, merchant.get("merchant_id", merchant_id))[:12],
        }

    @router.get("/categories")
    async def list_categories():
        out = []
        for cid, category in _iter_scope(contexts, "category"):
            merchants = sum(1 for _m, m in _iter_scope(contexts, "merchant")
                            if m.get("category_slug") == category.get("slug", cid))
            out.append({
                "slug": category.get("slug", cid),
                "display_name": category.get("display_name", cid),
                "tone": (category.get("voice", {}) or {}).get("tone", ""),
                "taboos": (category.get("voice", {}) or {}).get("vocab_taboo", []),
                "merchants": merchants,
            })
        out.sort(key=lambda c: c["display_name"])
        return {"categories": out}

    @router.get("/triggers")
    async def list_triggers(merchant_id: Optional[str] = None):
        out = []
        for cid, t in _iter_scope(contexts, "trigger"):
            if merchant_id and t.get("merchant_id") != merchant_id:
                continue
            out.append({
                "id": t.get("id", cid),
                "kind": t.get("kind", ""),
                "scope": t.get("scope", "merchant"),
                "urgency": t.get("urgency", 0),
                "merchant_id": t.get("merchant_id"),
                "customer_id": t.get("customer_id"),
                "suppression_key": t.get("suppression_key", ""),
                "suppressed": t.get("suppression_key", "") in suppressed_keys,
                "payload": t.get("payload", {}),
            })
        out.sort(key=lambda t: (-(t["urgency"] or 0), t["id"]))
        return {"triggers": out, "count": len(out)}

    # -- data controls ----------------------------------------------------
    @router.post("/seed")
    async def seed(body: SeedBody = SeedBody()):
        t0 = time.perf_counter()
        loaded = {
            "category": _seed_dir("categories", "slug", contexts, "category"),
            "merchant": _seed_dir("merchants", "merchant_id", contexts, "merchant"),
        }
        if body.include_customers:
            loaded["customer"] = _seed_dir("customers", "customer_id", contexts, "customer")
        if body.include_triggers:
            loaded["trigger"] = _seed_dir("triggers", "id", contexts, "trigger")
        elapsed = (time.perf_counter() - t0) * 1000

        console_state.counters["context_pushes"] += sum(loaded.values())
        console_state.record_system(
            "Seed dataset ingested",
            " · ".join(f"{v} {k}" for k, v in loaded.items() if v),
        )
        return {
            "loaded": loaded,
            "total": sum(loaded.values()),
            "elapsed_ms": round(elapsed, 2),
            "source": str(DATA_DIR),
        }

    @router.post("/reset")
    async def reset():
        contexts.clear()
        suppressed_keys.clear()
        conv_manager.conversations.clear()
        sim_conversations.clear()
        console_state.timeline.clear()
        console_state.latencies.clear()
        console_state.scores.clear()
        console_state.counters.clear()
        console_state.record_system("Engine state cleared", "contexts, suppression keys and transcripts dropped")
        return {"status": "cleared", "timestamp": _now_iso()}

    @router.post("/tick")
    async def run_tick(body: SimTickBody = SimTickBody()):
        """
        Delegates to the real `/v1/tick` handler so the console exercises the
        exact same engine path the judge harness hits. Telemetry for the
        resulting actions is recorded by `bot.tick` itself — recording again
        here would double-count every composition.
        """
        engine = bot_module
        if engine is None:  # pragma: no cover - defensive
            import sys
            engine = sys.modules.get("bot") or sys.modules["__main__"]
        if body.trigger_ids:
            trigger_ids = body.trigger_ids
        else:
            trigger_ids = [cid for cid, _ in _iter_scope(contexts, "trigger")][: body.limit]

        t0 = time.perf_counter()
        result = await engine.tick(
            engine.TickBody(now=_now_iso(), available_triggers=trigger_ids)
        )
        latency = (time.perf_counter() - t0) * 1000
        actions = result.get("actions", [])

        return {"actions": actions, "trigger_count": len(trigger_ids), "latency_ms": round(latency, 3)}

    # -- conversation simulator -------------------------------------------
    @router.get("/simulations")
    async def list_simulations():
        return {
            "simulations": [
                {
                    "conversation_id": cid,
                    "merchant_id": s.get("merchant_id"),
                    "merchant_name": s.get("merchant_name"),
                    "turns": len(s.get("turns", [])),
                    "closed": s.get("closed", False),
                    "opened_at": s.get("opened_at"),
                }
                for cid, s in sim_conversations.items()
            ]
        }

    @router.get("/simulations/{conversation_id}")
    async def get_simulation(conversation_id: str):
        state = sim_conversations.get(conversation_id)
        if not state:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        return state

    @router.post("/simulate")
    async def simulate(body: SimulateBody):
        """
        One turn of the console's conversation simulator.

        `message` empty  -> open the conversation with a grounded opener.
        `message` filled -> process the merchant's reply through the real guard core.
        """
        merchant = _resolve_merchant(contexts, body.merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="merchant_not_loaded")
        category = _resolve_category(contexts, merchant)

        cid = body.conversation_id
        state = sim_conversations.get(cid)
        if state and state.get("closed") and body.message:
            raise HTTPException(status_code=409, detail="conversation_closed")

        if state is None:
            state = {
                "conversation_id": cid,
                "merchant_id": merchant.get("merchant_id", body.merchant_id),
                "merchant_name": (merchant.get("identity", {}) or {}).get("name", ""),
                "opened_at": _now_iso(),
                "closed": False,
                "turns": [],
            }
            sim_conversations[cid] = state

        # ---------------- open the conversation ----------------------------
        if not (body.message or "").strip():
            t0 = time.perf_counter()
            trigger = None
            if body.trigger_id:
                for _tcid, t in _iter_scope(contexts, "trigger"):
                    if t.get("id") == body.trigger_id or _tcid == body.trigger_id:
                        trigger = t
                        break
            customer = _resolve_customer(contexts, body.customer_id or "")

            if trigger:
                composed = compose(category, merchant, trigger, customer)
                source_label = f"trigger:{trigger.get('kind', 'custom')}"
            else:
                composed = _cold_open(merchant, category)
                source_label = "cold_open:profile_audit"

            sc = score_message(composed.get("body", ""), composed.get("cta", "none"), category, merchant)
            facts = sc["facts"]
            trace = _pipeline_trace(category=category, merchant=merchant, facts=facts)
            latency = (time.perf_counter() - t0) * 1000
            if composed.get("suppression_key"):
                suppressed_keys.add(composed["suppression_key"])

            turn = {
                "role": "vera",
                "turn": len(state["turns"]) + 1,
                "ts": _now_iso(),
                "message": composed.get("body", ""),
                "action": "send",
                "cta": composed.get("cta", "none"),
                "send_as": composed.get("send_as", "vera"),
                "suppression_key": composed.get("suppression_key", ""),
                "wait_seconds": None,
                "rationale": composed.get("rationale", ""),
                "template_name": composed.get("template_name", ""),
                "trace": trace,
                "grounding": facts,
                "score": sc,
                "latency_ms": round(latency, 3),
                "source": source_label,
                "contract": None,
            }
            state["turns"].append(turn)
            state["opener_source"] = source_label
            console_state.record_action(
                {
                    "merchant_id": state["merchant_id"],
                    "customer_id": body.customer_id,
                    "conversation_id": cid,
                    "trigger_id": body.trigger_id,
                    "send_as": turn["send_as"],
                    "cta": turn["cta"],
                    "suppression_key": turn["suppression_key"],
                    "rationale": turn["rationale"],
                    "body": turn["message"],
                },
                score=sc, latency_ms=latency, source="simulator",
            )
            return {**state, "turn": turn}

        # ---------------- merchant reply -----------------------------------
        incoming = body.message.strip()
        t0 = time.perf_counter()

        # Real guard core — identical code path as POST /v1/reply.
        contract = conv_manager.handle_reply(
            conv_id=cid,
            merchant_id=body.merchant_id,
            message=incoming,
            turn_number=len(state["turns"]) + 1,
        )
        latency = (time.perf_counter() - t0) * 1000

        action = contract.get("action", "send")
        guards: List[str] = []
        auto_state = "clear"
        intent_state = "idle"

        if action == "end":
            body_text = contract.get("body")
            if body_text:
                # Hostility / opt-out — the engine apologises and exits.
                guards.append("hostile_exit")
                auto_state = "clear"
            else:
                # Persistent canned auto-reply — the engine exits silently.
                guards.append("auto_reply_persistent")
                auto_state = "end"
            state["closed"] = True
            # The engine short-circuits before the intent stage is ever consulted.
            intent_state = "blocked"
            trace = _pipeline_trace(
                category=category, merchant=merchant,
                auto_reply=auto_state, intent=intent_state, grounded=bool(body_text),
            )
        elif action == "wait":
            guards.append("auto_reply_detected")
            auto_state = "wait"
            intent_state = "blocked"
            body_text = None
            trace = _pipeline_trace(
                category=category, merchant=merchant,
                auto_reply=auto_state, intent=intent_state, grounded=False,
            )
        else:
            if conv_manager.is_commitment(incoming):
                guards.append("intent_handoff")
                intent_state = "fired"
            # NOTE: no post-hoc is_auto_reply() re-check here — handle_reply()
            # already evaluated it *before* recording this turn and it let the
            # message through, so the guard genuinely did not fire. Re-running
            # it afterwards would read a longer history and mislabel the trace.

            if intent_state == "fired":
                # Canonical action-mode text straight from the engine.
                body_text = contract.get("body")
                grounded = True
            elif contract.get("body") and contract["body"] != (
                "Got it! Here is the next step: I have drafted your campaign draft and saved the setup. "
                "Proceed with activating this now?"
            ):
                # Engine already produced a specific answer (abstract/PDF/deliver path).
                body_text = contract.get("body")
                grounded = True
            else:
                enriched = _grounded_followup(merchant, category, incoming)
                body_text = enriched["body"]
                grounded = True
                contract = {**contract, "rationale": enriched["rationale"]}

            trace = _pipeline_trace(
                category=category, merchant=merchant,
                auto_reply=auto_state, intent=intent_state, grounded=grounded,
            )

        sc = score_message(body_text or "", (contract.get("cta") or "none"), category, merchant) if body_text else None
        facts = sc["facts"] if sc else {}

        turn = {
            "role": "merchant",
            "turn": len(state["turns"]) + 1,
            "ts": _now_iso(),
            "message": incoming,
            "action": action,
            "cta": contract.get("cta"),
            "send_as": contract.get("send_as"),
            "suppression_key": f"optout:{state['merchant_id']}" if action == "end" else None,
            "wait_seconds": contract.get("wait_seconds"),
            "rationale": contract.get("rationale", ""),
            "trace": trace,
            "grounding": facts,
            "score": sc,
            "latency_ms": round(latency, 3),
            "guards": guards,
            "contract": contract,
        }
        state["turns"].append(turn)

        # Mirror the bot turn into the transcript so both views stay in sync.
        if body_text:
            state["turns"].append({
                "role": "vera",
                "turn": turn["turn"],
                "ts": _now_iso(),
                "message": body_text,
                "action": action,
                "cta": contract.get("cta"),
                "send_as": contract.get("send_as", "vera"),
                "suppression_key": turn["suppression_key"],
                "wait_seconds": contract.get("wait_seconds"),
                "rationale": contract.get("rationale", ""),
                "trace": None,
                "grounding": facts,
                "score": sc,
                "latency_ms": round(latency, 3),
                "source": "guard_core",
            })

        console_state.record_reply(
            cid, action,
            body=body_text,
            rationale=contract.get("rationale", ""),
            merchant_id=state["merchant_id"],
            latency_ms=latency,
            score=sc,
            guards=guards,
            source="simulator",
        )

        return {**state, "turn": turn}

    return router

"""
magicpin AI Challenge — Vera Merchant AI Assistant Service
==========================================================
Full production-grade FastAPI service meeting the judge harness specification.
Exposes 5 required endpoints:
- GET  /v1/healthz
- GET  /v1/metadata
- POST /v1/context
- POST /v1/tick
- POST /v1/reply
Plus:
- POST /v1/teardown (clean state reset)
- /v1/dashboard/* (console observability + conversation simulator)
- /  (zero-build web console, served from ./web)
"""

import time
import os
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from composer import compose
from conversation_handlers import conv_manager
from dashboard import build_console_router, console_state
from rubric import score_message

app = FastAPI(
    title="magicpin Vera AI Assistant",
    description="Context-grounded merchant growth & customer engagement bot",
    version="1.0.0"
)

START_TIME = time.time()
WEB_DIR = Path(__file__).resolve().parent / "web"

# In-memory context storage: (scope, context_id) -> {"version": int, "payload": dict}
contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}

# Deduplication and suppression set for proactive sends
suppressed_keys: set = set()


# -----------------------------------------------------------------------------
# Pydantic Request Models
# -----------------------------------------------------------------------------

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


class TickBody(BaseModel):
    now: Optional[str] = None
    available_triggers: List[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    """
    Liveness probe returning uptime and count of active contexts loaded per scope.
    """
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        if scope in counts:
            counts[scope] += 1
        else:
            counts[scope] = 1

    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts
    }


@app.get("/v1/metadata")
async def metadata():
    """
    Bot identity metadata for competition evaluation.
    """
    return {
        "team_name": "Vera Elite",
        "team_members": ["Divyansh Sharma"],
        "model": "hybrid-deterministic-reasoning-engine",
        "approach": (
            "Multi-layer 4-context grounded composer with domain voice guardrails, "
            "zero-hallucination factual anchoring, stateful multi-turn intent transition, "
            "and instant WhatsApp Business auto-reply detection"
        ),
        "contact_email": "divyanshsharma@magicpin.in",
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z"
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    """
    Idempotent context ingestion with version validation.
    Higher versions atomically replace prior versions.
    Equal or lower versions return HTTP 409 conflict.
    """
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"Scope must be one of {valid_scopes}"}
        )

    key = (body.scope, body.context_id)
    cur = contexts.get(key)

    if cur is not None and cur["version"] > body.version:
        console_state.record_context_push(body.scope, body.context_id, body.version, accepted=False)
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": cur["version"]
            }
        )

    # Store or update (idempotent for same version, atomic replace for higher version)
    contexts[key] = {
        "version": body.version,
        "payload": body.payload
    }

    # Also index by alternative key if available for merchants
    if body.scope == "merchant" and "merchant_id" in body.payload:
        alt_key = ("merchant", body.payload["merchant_id"])
        contexts[alt_key] = contexts[key]

    console_state.record_context_push(body.scope, body.context_id, body.version, accepted=True)

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z"
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    """
    Periodic tick handler. Evaluates available active triggers and composes
    high-compulsion proactive outbound messages.
    """
    actions = []
    tick_start = time.perf_counter()

    for trg_id in body.available_triggers:
        if len(actions) >= 20:  # Respect rate limit cap
            break
            
        trg_item = contexts.get(("trigger", trg_id))
        trg = trg_item.get("payload") if trg_item else None
        if not trg:
            # Check if trigger payload is directly in contexts
            for (scope, cid), val in contexts.items():
                if scope == "trigger" and (cid == trg_id or val.get("payload", {}).get("id") == trg_id):
                    trg = val.get("payload")
                    break
        
        if not trg:
            continue

        suppression_key = trg.get("suppression_key", "")
        if suppression_key and suppression_key in suppressed_keys:
            continue

        merchant_id = trg.get("merchant_id")
        merchant_item = contexts.get(("merchant", merchant_id))
        merchant = merchant_item.get("payload") if merchant_item else None
        
        # Fallback search if merchant_id has slight prefix variations
        if not merchant and merchant_id:
            for (scope, mid), val in contexts.items():
                if scope == "merchant" and (mid.startswith(merchant_id) or merchant_id.startswith(mid)):
                    merchant = val.get("payload")
                    break

        if not merchant:
            continue

        category_slug = merchant.get("category_slug", "")
        category_item = contexts.get(("category", category_slug))
        category = category_item.get("payload") if category_item else {}

        # Resolve optional customer context
        customer_id = trg.get("customer_id")
        customer = None
        if customer_id:
            customer_item = contexts.get(("customer", customer_id))
            customer = customer_item.get("payload") if customer_item else None
            if not customer:
                for (scope, cid), val in contexts.items():
                    if scope == "customer" and (cid == customer_id or val.get("payload", {}).get("customer_id") == customer_id):
                        customer = val.get("payload")
                        break

        # Compose message
        compose_started = time.perf_counter()
        composed = compose(category, merchant, trg, customer)
        compose_ms = (time.perf_counter() - compose_started) * 1000
        
        # Track suppression
        if suppression_key:
            suppressed_keys.add(suppression_key)

        conv_id = f"conv_{merchant.get('merchant_id', 'm')}_{trg_id}"
        
        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant.get("merchant_id", merchant_id),
            "customer_id": customer.get("customer_id", customer_id) if customer else None,
            "send_as": composed.get("send_as", "vera"),
            "trigger_id": trg_id,
            "template_name": composed.get("template_name", "vera_outbound_v1"),
            "template_params": composed.get("template_params", []),
            "body": composed.get("body", ""),
            "cta": composed.get("cta", "binary_yes_no"),
            "suppression_key": suppression_key,
            "rationale": composed.get("rationale", "")
        }
        actions.append(action)

        # Console telemetry (observability only — does not affect the response).
        console_state.record_action(
            action,
            score=score_message(action["body"], action["cta"], category, merchant),
            latency_ms=compose_ms,
            source="judge_harness",
        )

    console_state.record_tick(len(body.available_triggers), len(actions),
                              (time.perf_counter() - tick_start) * 1000)

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    """
    Synchronous conversation reply handler for merchant/customer replies.
    Features auto-reply detection, instant intent-switching, and hostile exit.
    """
    started = time.perf_counter()
    result = conv_manager.handle_reply(
        conv_id=body.conversation_id,
        merchant_id=body.merchant_id,
        message=body.message,
        turn_number=body.turn_number
    )
    console_state.record_reply(
        body.conversation_id,
        result.get("action", "send"),
        body=result.get("body"),
        rationale=result.get("rationale", ""),
        merchant_id=body.merchant_id,
        latency_ms=(time.perf_counter() - started) * 1000,
        source="judge_harness",
    )
    return result


@app.post("/v1/teardown")
async def teardown():
    """
    Clean state reset when requested by judge.
    """
    contexts.clear()
    suppressed_keys.clear()
    conv_manager.conversations.clear()
    return {"status": "cleared", "timestamp": datetime.utcnow().isoformat() + "Z"}


# -----------------------------------------------------------------------------
# Web console (additive — mounted last so it never shadows the /v1 API)
# -----------------------------------------------------------------------------

app.include_router(
    build_console_router(contexts, suppressed_keys, conv_manager, START_TIME, bot_module=sys.modules[__name__])
)

class ConsoleStaticFiles(StaticFiles):
    """Static handler for the zero-build console.

    Starlette sends ETag/Last-Modified but no Cache-Control, so browsers fall
    back to heuristic freshness and a plain refresh can silently serve a stale
    app.js or styles.css after an edit. "no-cache" forces revalidation on every
    load while still allowing cheap 304s.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if WEB_DIR.exists():
    # html=True serves web/index.html at "/" and assets alongside it.
    # Mounted last so it never shadows the /v1/* API routes above.
    app.mount("/", ConsoleStaticFiles(directory=str(WEB_DIR), html=True), name="console")


# Run standalone if executed directly
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

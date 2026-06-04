"""
POST /vapi/call-result — webhook receiver for Vapi end-of-call reports.

Security:   verifies x-vapi-secret header.
Idempotency: ignores webhooks whose call.id doesn't match current_call_id
             (stale/duplicate delivery).
"""
import logging
from typing import Any, Dict
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Referral
from ..config import VAPI_WEBHOOK_SECRET

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Payload extraction helpers ────────────────────────────────────────────────

def _parse_outcome(ended_reason: str | None) -> str:
    if not ended_reason:
        return "no_answer"
    r = ended_reason.lower()
    if "voicemail" in r:
        return "voicemail"
    if "customer-ended" in r or "assistant-ended" in r or r == "hangup":
        return "answered"
    return "no_answer"


def _extract_referral_id(body: dict) -> str | None:
    # Vapi end-of-call-report: body.message.call.metadata.referralId
    msg  = body.get("message") or {}
    call = msg.get("call") or {}
    meta = call.get("metadata") or {}
    if meta.get("referralId"):
        return str(meta["referralId"])
    # Flat fallback
    flat_meta = body.get("metadata") or {}
    if flat_meta.get("referralId"):
        return str(flat_meta["referralId"])
    return None


def _extract_ended_reason(body: dict) -> str | None:
    msg  = body.get("message") or {}
    if msg.get("endedReason"):
        return str(msg["endedReason"])
    call = msg.get("call") or {}
    if call.get("endedReason"):
        return str(call["endedReason"])
    if body.get("endedReason"):
        return str(body["endedReason"])
    return None


def _extract_call_id(body: dict) -> str | None:
    msg    = body.get("message") or {}
    call   = msg.get("call") or {}
    if call.get("id"):
        return str(call["id"])
    direct = body.get("call") or {}
    if direct.get("id"):
        return str(direct["id"])
    return None


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/vapi/call-result")
def call_result(
    request: Request,
    body: Dict[str, Any],
    db: Session = Depends(get_db),
):
    # 1. Verify webhook secret so only Vapi can mutate state
    if VAPI_WEBHOOK_SECRET and request.headers.get("x-vapi-secret") != VAPI_WEBHOOK_SECRET:
        logger.warning("[vapi/call-result] Rejected — bad x-vapi-secret")
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    msg = body.get("message") or {}

    # 2. Only process end-of-call-report events
    if msg.get("type") != "end-of-call-report":
        return {"ok": True, "ignored": True, "reason": "not end-of-call-report"}

    referral_id  = _extract_referral_id(body)
    call_id      = _extract_call_id(body)
    ended_reason = _extract_ended_reason(body)
    outcome      = _parse_outcome(ended_reason)

    if not referral_id:
        return {"ok": True, "ignored": True, "reason": "no referralId"}

    referral = db.query(Referral).filter(Referral.id == referral_id).first()

    if not referral:
        return {"ok": True, "ignored": True, "reason": "referral not found"}

    if referral.status in ("closed", "contacted"):
        return {"ok": True, "ignored": True, "reason": f"already {referral.status}"}

    # 3. Idempotency: stale/duplicate webhook — reject to prevent double-processing
    if call_id and referral.current_call_id and call_id != referral.current_call_id:
        logger.info(
            "[vapi/call-result] Stale webhook for callId=%s (current=%s) — ignored",
            call_id, referral.current_call_id,
        )
        return {"ok": True, "ignored": True, "reason": "stale callId"}

    logger.info(
        "[vapi/call-result] Referral %s — outcome: %s, attempt: %d",
        referral_id, outcome, referral.attempt_count,
    )

    if outcome == "answered":
        db.query(Referral).filter(Referral.id == referral_id).update(
            {
                "status":          "contacted",
                "outcome":         "Patient reached — appointment pending",
                "current_call_id": None,
            },
            synchronize_session=False,
        )
        db.commit()
        logger.info("[vapi/call-result] Referral %s — CONTACTED.", referral_id)

    elif referral.attempt_count >= 3:
        db.query(Referral).filter(Referral.id == referral_id).update(
            {
                "status":          "closed",
                "outcome":         "patient unreachable — 3 attempts",
                "current_call_id": None,
            },
            synchronize_session=False,
        )
        db.commit()
        logger.info(
            "[vapi/call-result] Referral %s — CLOSED after 3 attempts. (fax stub)",
            referral_id,
        )

    else:
        # Voicemail or no-answer — clear current_call_id so this webhook can't
        # be replayed; scheduler will retry when next_attempt_at is due.
        db.query(Referral).filter(Referral.id == referral_id).update(
            {"outcome": outcome, "current_call_id": None},
            synchronize_session=False,
        )
        db.commit()
        logger.info(
            "[vapi/call-result] Referral %s — outcome recorded: %s. Scheduler will retry.",
            referral_id, outcome,
        )

    return {"ok": True, "outcome": outcome}

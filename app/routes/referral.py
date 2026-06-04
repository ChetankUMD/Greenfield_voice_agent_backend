"""
Referral management endpoints.

POST /referral/start      — trigger attempt 1 (or re-trigger) for a referral
POST /referral/{id}/reset — reset a referral back to referral_received for re-testing
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Referral
from ..vapi import place_outbound_call
from ..config import RETRY_INTERVAL_MINUTES

logger = logging.getLogger(__name__)
router = APIRouter()


class StartReferralBody(BaseModel):
    referral_id: Optional[str] = None


@router.post("/referral/start")
def start_referral(body: StartReferralBody, db: Session = Depends(get_db)):
    if body.referral_id:
        referral = db.query(Referral).filter(Referral.id == body.referral_id).first()
    else:
        referral = (
            db.query(Referral)
            .filter(Referral.patient_name == "James Patterson")
            .first()
        )

    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found.")

    if referral.status in ("closed", "contacted"):
        raise HTTPException(
            status_code=409, detail=f"Referral is already {referral.status}."
        )

    now = datetime.now(timezone.utc).isoformat()
    next_attempt_at = (
        datetime.now(timezone.utc) + timedelta(minutes=RETRY_INTERVAL_MINUTES)
    ).isoformat()
    new_attempt_count = referral.attempt_count + 1
    old_status = referral.status

    # Claim the attempt in DB BEFORE placing the call so the scheduler can't
    # simultaneously fire for the same referral.
    db.query(Referral).filter(Referral.id == referral.id).update(
        {
            "status":          "in_progress",
            "attempt_count":   new_attempt_count,
            "last_attempt_at": now,
            "next_attempt_at": next_attempt_at,
            "current_call_id": None,
            "outcome":         None,
        },
        synchronize_session=False,
    )
    db.commit()

    try:
        call_id = place_outbound_call(referral.phone, referral.id)
        db.query(Referral).filter(Referral.id == referral.id).update(
            {"current_call_id": call_id}, synchronize_session=False
        )
        db.commit()

        return {
            "ok":             True,
            "referral_id":    referral.id,
            "patient":        referral.patient_name,
            "call_id":        call_id,
            "attempt":        new_attempt_count,
            "next_attempt_at": next_attempt_at,
        }

    except Exception as exc:
        # Roll back the claim so it can be retried
        db.query(Referral).filter(Referral.id == referral.id).update(
            {
                "attempt_count":   new_attempt_count - 1,
                "status":          old_status,
                "last_attempt_at": None,
                "next_attempt_at": None,
                "current_call_id": None,
            },
            synchronize_session=False,
        )
        db.commit()
        logger.error("Failed to place outbound call: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to place outbound call. {exc}",
        )


@router.post("/referral/{referral_id}/reset")
def reset_referral(referral_id: str, db: Session = Depends(get_db)):
    referral = db.query(Referral).filter(Referral.id == referral_id).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Referral not found.")

    db.query(Referral).filter(Referral.id == referral_id).update(
        {
            "status":          "referral_received",
            "attempt_count":   0,
            "last_attempt_at": None,
            "next_attempt_at": None,
            "current_call_id": None,
            "outcome":         None,
        },
        synchronize_session=False,
    )
    db.commit()

    return {"ok": True, "referral_id": referral_id, "status": "referral_received"}

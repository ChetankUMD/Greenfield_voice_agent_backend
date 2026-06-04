"""
Referral retry scheduler — runs every 60 s and drives the outbound-call campaign.

Retry logic (must match Node.js behaviour exactly):

1. Close exhausted referrals that never received a final webhook:
     status = 'in_progress' AND attempt_count >= 3 AND next_attempt_at <= now

2. Find referrals due for their next attempt:
     status = 'in_progress' AND attempt_count < 3 AND next_attempt_at <= now

3. Atomic claim per referral (prevents duplicate calls when ticks overlap):
     UPDATE … WHERE id = <id>
                AND status = 'in_progress'
                AND attempt_count = <value we read>   ← pin prevents double-fire
                AND next_attempt_at <= now
     SET attempt_count += 1, last_attempt_at = now, next_attempt_at = now + interval
     → if rows_updated == 0: another tick already claimed it — skip

4. Place the call; on failure roll back the claim so the next tick retries.
"""
import logging
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from .db import SessionLocal
from .models import Referral
from .vapi import place_outbound_call
from .config import RETRY_INTERVAL_MINUTES

logger = logging.getLogger(__name__)


def _retry_tick() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).isoformat()
        next_at = (
            datetime.now(timezone.utc) + timedelta(minutes=RETRY_INTERVAL_MINUTES)
        ).isoformat()

        # ── Step 1: close exhausted referrals that never got a final webhook ──
        db.query(Referral).filter(
            Referral.status == "in_progress",
            Referral.attempt_count >= 3,
            Referral.next_attempt_at <= now,
        ).update(
            {
                "status": "closed",
                "outcome": "patient unreachable — 3 attempts",
                "current_call_id": None,
            },
            synchronize_session=False,
        )
        db.commit()

        # ── Step 2: find referrals due for a retry ─────────────────────────
        due = (
            db.query(Referral)
            .filter(
                Referral.status == "in_progress",
                Referral.attempt_count < 3,
                Referral.next_attempt_at <= now,
            )
            .all()
        )

        for ref in due:
            saved_count = ref.attempt_count  # capture before any update

            # ── Step 3: atomic claim ──────────────────────────────────────
            rows = (
                db.query(Referral)
                .filter(
                    Referral.id == ref.id,
                    Referral.status == "in_progress",
                    Referral.attempt_count == saved_count,
                    Referral.next_attempt_at <= now,
                )
                .update(
                    {
                        "attempt_count": saved_count + 1,
                        "last_attempt_at": now,
                        "next_attempt_at": next_at,
                        "current_call_id": None,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()

            if rows == 0:
                logger.info("[scheduler] Referral %s already claimed — skipping", ref.id)
                continue

            # ── Step 4: place the call ────────────────────────────────────
            try:
                logger.info(
                    "[scheduler] Placing call for referral %s, attempt %d",
                    ref.id, saved_count + 1,
                )
                call_id = place_outbound_call(ref.phone, ref.id)
                db.query(Referral).filter(Referral.id == ref.id).update(
                    {"current_call_id": call_id}, synchronize_session=False
                )
                db.commit()
                logger.info(
                    "[scheduler] Call placed for referral %s, callId=%s", ref.id, call_id
                )

            except Exception as exc:
                logger.error("[scheduler] Failed to call referral %s: %s", ref.id, exc)
                # Roll back the claim; set next_attempt_at = now so the next tick retries immediately
                db.query(Referral).filter(Referral.id == ref.id).update(
                    {
                        "attempt_count": saved_count,
                        "last_attempt_at": None,
                        "next_attempt_at": now,
                    },
                    synchronize_session=False,
                )
                db.commit()

    except Exception as exc:
        logger.error("[scheduler] Unexpected error in retry tick: %s", exc)
        db.rollback()
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    logger.info(
        "Referral retry scheduler started (retry interval: %d min, poll: every 60 s).",
        RETRY_INTERVAL_MINUTES,
    )
    scheduler = BackgroundScheduler()
    scheduler.add_job(_retry_tick, "interval", seconds=60, id="referral_retry")
    scheduler.start()
    return scheduler

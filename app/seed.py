"""
Database seed: open appointment slots for the next 10 business days plus a
test referral for James Patterson.

Provider schedules (UTC hours, matching the Node.js version):
  Dr. Sarah Chen      SF      Mon/Wed/Fri  09:00–17:00  30-min slots
  Dr. Sarah Chen      Oakland Tue/Thu      10:00–16:00  30-min slots
  Dr. Marcus Webb     SF      Tue/Thu      08:00–16:00  30-min slots
  Jennifer Park, NP   SF      Mon–Fri      08:00–12:00  30-min slots
"""
import uuid
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from .models import Slot, Referral

logger = logging.getLogger(__name__)

SLOT_INTERVAL_MIN = 30

# Each entry: (provider, location, python_weekdays, start_hour_utc, end_hour_utc)
# Python weekday(): 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
_SCHEDULES = [
    ("Dr. Sarah Chen",    "SF",      [0, 2, 4],    9,  17),
    ("Dr. Sarah Chen",    "Oakland", [1, 3],       10,  16),
    ("Dr. Marcus Webb",   "SF",      [1, 3],        8,  16),
    ("Jennifer Park, NP", "SF",      [0, 1, 2, 3, 4], 8, 12),
]


def seed(db: Session) -> None:
    slot_count = db.query(Slot).count()
    if slot_count > 0:
        logger.info("Database already seeded — skipping slots.")
    else:
        logger.info("Seeding database with appointment slots…")
        _seed_slots(db)

    # Always ensure the James Patterson test referral exists
    existing = (
        db.query(Referral).filter(Referral.patient_name == "James Patterson").first()
    )
    if not existing:
        _seed_referral(db)


def _seed_slots(db: Session) -> None:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    business_days_seeded = 0
    cursor = today
    slots: list[Slot] = []

    while business_days_seeded < 10:
        # weekday() 0–4 = Mon–Fri
        if cursor.weekday() <= 4:
            for provider, location, days, start_h, end_h in _SCHEDULES:
                if cursor.weekday() not in days:
                    continue

                slot_start = cursor.replace(hour=start_h, minute=0)
                day_end = cursor.replace(hour=end_h, minute=0)

                while slot_start < day_end:
                    slot_end = slot_start + timedelta(minutes=SLOT_INTERVAL_MIN)
                    if slot_end > day_end:
                        break
                    slots.append(
                        Slot(
                            id=str(uuid.uuid4()),
                            provider=provider,
                            location=location,
                            start_iso=slot_start.isoformat(),
                            end_iso=slot_end.isoformat(),
                            status="open",
                        )
                    )
                    slot_start = slot_end

            business_days_seeded += 1

        cursor += timedelta(days=1)

    db.bulk_save_objects(slots)
    db.commit()
    logger.info("Seeding complete. %d slots created across 10 business days.", len(slots))


def _seed_referral(db: Session) -> None:
    referral = Referral(
        id=str(uuid.uuid4()),
        patient_name="James Patterson",
        dob="1958-04-22",
        phone="+12403984254",
        insurance_carrier="Aetna PPO",
        member_id="AET-992847162",
        group_number=None,
        referring_provider="Dr. Michael Torres, Bay Area Internal Medicine",
        npi=None,
        reason="Exertional chest pain, ST changes on EKG",
        urgency="Routine",
        status="referral_received",
        attempt_count=0,
    )
    db.add(referral)
    db.commit()
    logger.info("Test referral created for James Patterson (id=%s).", referral.id)

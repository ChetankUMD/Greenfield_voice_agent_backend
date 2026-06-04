from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Slot, Appointment, Referral

router = APIRouter()


@router.get("/admin/state")
def admin_state(db: Session = Depends(get_db)):
    slots = db.query(Slot).order_by(Slot.start_iso).all()
    appointments = db.query(Appointment).order_by(Appointment.created_at).all()
    referrals = db.query(Referral).all()

    def _row(obj):
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

    return {
        "slots":        [_row(s) for s in slots],
        "appointments": [_row(a) for a in appointments],
        "referrals":    [_row(r) for r in referrals],
    }

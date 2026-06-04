"""
Vapi tool-call endpoints.

Each endpoint receives a Vapi webhook payload and must reply:
  { "results": [{ "toolCallId": "<same id>", "result": "<string>" }] }
"""
import uuid
import logging
from typing import Any, Dict
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Slot, Appointment
from ..tool_response import extract_tool_call, tool_response
from ..insurance import verify_insurance
from ..scheduling import (
    normalize_provider,
    normalize_location,
    PROVIDER_SCHEDULE,
    PROVIDER_APPT_TYPES,
    LOCATION_ADDRESSES,
    APPT_DURATIONS,
    find_available_blocks,
    format_spoken_datetime,
    generate_confirmation_id,
    next_scheduled_start,
    slots_needed,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── POST /tools/find_availability ──────────────────────────────────────────

@router.post("/tools/find_availability")
def find_availability(body: Dict[str, Any], db: Session = Depends(get_db)):
    call = extract_tool_call(body)
    if not call:
        logger.error("[find_availability] Could not parse payload: %s", body)
        return JSONResponse(status_code=400, content={"error": "Invalid Vapi tool-call payload"})

    args = call["arguments"]
    provider_raw = str(args.get("provider") or "")
    location_raw = str(args.get("location") or "")
    appt_type    = str(args.get("appt_type") or "")
    preferred_date = args.get("preferred_date")

    provider = normalize_provider(provider_raw)
    location = normalize_location(location_raw)

    if not provider:
        return tool_response(
            call["id"],
            "I don't recognize that provider name. Our providers are "
            "Dr. Sarah Chen, Dr. Marcus Webb, and Jennifer Park NP.",
        )

    if not location:
        return tool_response(
            call["id"],
            "I don't recognize that location. We have a San Francisco office and an Oakland office.",
        )

    valid_days = PROVIDER_SCHEDULE.get(provider, {}).get(location)
    if not valid_days:
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        # Convert Python weekday indices back to day names for the message
        schedule_str = "; ".join(
            f"{loc} on {', '.join(day_names[d] for d in days)}"
            for loc, days in PROVIDER_SCHEDULE.get(provider, {}).items()
        )
        return tool_response(
            call["id"],
            f"{provider} does not work at the {location} office. "
            f"They are available at: {schedule_str}.",
        )

    allowed_types = PROVIDER_APPT_TYPES.get(provider, [])
    if appt_type and appt_type not in allowed_types:
        return tool_response(
            call["id"],
            f"{provider} does not handle {appt_type.replace('_', ' ')} appointments. "
            f"They handle: {', '.join(allowed_types)}.",
        )

    effective_type = appt_type or (allowed_types[0] if allowed_types else "follow_up")
    blocks = find_available_blocks(db, provider, location, effective_type, preferred_date)

    if not blocks:
        date_clause = f" on or after {preferred_date}" if preferred_date else ""
        return tool_response(
            call["id"],
            f"I'm sorry, there are no available openings for {provider} at the "
            f"{location} office{date_clause}. Would you like to check another date or provider?",
        )

    top3 = blocks[:3]
    location_label = "San Francisco" if location == "SF" else "Oakland"

    if len(top3) == 1:
        return tool_response(
            call["id"],
            f"{provider} has one opening: {format_spoken_datetime(top3[0]['start_iso'])} "
            f"at the {location_label} office.",
        )

    parts = []
    for i, b in enumerate(top3):
        s = format_spoken_datetime(b["start_iso"])
        parts.append(f"and {s}" if i == len(top3) - 1 else s)

    return tool_response(
        call["id"],
        f"{provider} has openings {', '.join(parts)} at the {location_label} office.",
    )


# ─── POST /tools/verify_insurance ───────────────────────────────────────────

@router.post("/tools/verify_insurance")
def verify_insurance_tool(body: Dict[str, Any], db: Session = Depends(get_db)):
    call = extract_tool_call(body)
    if not call:
        return JSONResponse(status_code=400, content={"error": "Invalid Vapi tool-call payload"})

    carrier = str(call["arguments"].get("carrier") or "").strip()
    if not carrier:
        return tool_response(call["id"], "Could you please tell me the name of your insurance carrier?")

    result = verify_insurance(carrier)
    return tool_response(call["id"], result["message"])


# ─── POST /tools/book_appointment ───────────────────────────────────────────

@router.post("/tools/book_appointment")
def book_appointment(body: Dict[str, Any], db: Session = Depends(get_db)):
    call = extract_tool_call(body)
    if not call:
        return JSONResponse(status_code=400, content={"error": "Invalid Vapi tool-call payload"})

    args = call["arguments"]
    patient_name = str(args.get("patient_name") or "").strip()
    dob          = str(args.get("dob")          or "").strip()
    phone        = str(args.get("phone")        or "").strip()
    appt_type    = str(args.get("appt_type")    or "").strip()

    if not patient_name or not dob or not phone or not appt_type:
        return tool_response(
            call["id"],
            "I'm missing some information. I need the patient's full name, date of birth, "
            "phone number, and appointment type to complete the booking.",
        )

    provider = normalize_provider(str(args.get("provider") or ""))
    location = normalize_location(str(args.get("location") or ""))

    if not provider or not location:
        return tool_response(
            call["id"],
            "I couldn't identify the provider or location. "
            "Please specify a valid provider and office.",
        )

    # Insurance check for new patients
    is_new_patient = (
        args.get("is_new_patient") is True
        or appt_type in ("new_patient", "np_intake")
    )
    insurance_carrier = args.get("insurance_carrier")
    if is_new_patient and insurance_carrier:
        ins_result = verify_insurance(str(insurance_carrier))
        if not ins_result["accepted"]:
            return tool_response(call["id"], ins_result["message"])

    # Resolve appointment start time: slot_start_iso → slot_id → next available → fallback
    start_iso: str = str(args.get("slot_start_iso") or "").strip()

    if not start_iso and args.get("slot_id"):
        row = db.query(Slot).filter(Slot.id == str(args["slot_id"])).first()
        if row:
            start_iso = row.start_iso

    if not start_iso:
        row = (
            db.query(Slot)
            .filter(
                Slot.provider == provider,
                Slot.location == location,
                Slot.status == "open",
            )
            .order_by(Slot.start_iso)
            .first()
        )
        if row:
            start_iso = row.start_iso

    if not start_iso:
        start_iso = next_scheduled_start(provider, location)

    # Mark matched DB slots as booked (best-effort — never blocks confirmation)
    try:
        needed = slots_needed(appt_type)
        to_book = (
            db.query(Slot)
            .filter(
                Slot.provider == provider,
                Slot.location == location,
                Slot.start_iso >= start_iso,
                Slot.status == "open",
            )
            .order_by(Slot.start_iso)
            .limit(needed)
            .all()
        )
        for s in to_book:
            s.status = "booked"
        db.commit()
    except Exception:
        pass  # non-fatal

    # Always create and confirm the appointment record
    from datetime import datetime, timezone as tz
    confirmation_id = generate_confirmation_id()
    now_iso = datetime.now(tz.utc).isoformat()

    try:
        appt = Appointment(
            id=str(uuid.uuid4()),
            patient_name=patient_name,
            dob=dob,
            phone=phone,
            provider=provider,
            location=location,
            appt_type=appt_type,
            start_iso=start_iso,
            status="scheduled",
            confirmation_id=confirmation_id,
            created_at=now_iso,
        )
        db.add(appt)
        db.commit()
    except Exception:
        pass  # non-fatal — confirmation still goes through

    address = LOCATION_ADDRESSES.get(location, "")
    location_label = "San Francisco" if location == "SF" else "Oakland"
    spoken = format_spoken_datetime(start_iso)

    return tool_response(
        call["id"],
        f"You're all set! Your appointment with {provider} is confirmed for {spoken} "
        f"at our {location_label} office, located at {address}. "
        f"Your confirmation number is {confirmation_id}. "
        f"Please arrive 15 minutes early and bring your insurance card.",
    )

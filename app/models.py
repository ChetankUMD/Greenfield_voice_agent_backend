"""
SQLAlchemy models for Greenfield Cardiology Voice Agent.

Datetime fields (last_attempt_at, next_attempt_at, start_iso, end_iso, created_at) are
stored as ISO 8601 UTC strings (e.g. "2025-01-15T09:00:00.000000+00:00") so they remain
compatible with the Vapi tool contract and with the OCR pipeline that inserts referrals.
"""
import uuid
from sqlalchemy import Column, LargeBinary, String, Integer, Text
from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Slot(Base):
    """A 30-minute availability block for a provider at a location."""

    __tablename__ = "slots"

    id = Column(String(36), primary_key=True, default=_uuid)
    provider = Column(String(100), nullable=False)
    location = Column(String(50), nullable=False)
    # ISO 8601 UTC string, e.g. "2025-01-15T09:00:00.000000+00:00"
    start_iso = Column(Text, nullable=False)
    end_iso = Column(Text, nullable=False)
    # "open" | "booked"
    status = Column(String(20), nullable=False, default="open")


class Appointment(Base):
    """A confirmed patient appointment."""

    __tablename__ = "appointments"

    id = Column(String(36), primary_key=True, default=_uuid)
    patient_name = Column(String(200), nullable=False)
    dob = Column(String(20), nullable=False)
    phone = Column(String(30), nullable=False)
    provider = Column(String(100), nullable=False)
    location = Column(String(50), nullable=False)
    appt_type = Column(String(50), nullable=False)
    start_iso = Column(Text, nullable=False)
    # "scheduled" | "cancelled"
    status = Column(String(20), nullable=False, default="scheduled")
    confirmation_id = Column(String(20), nullable=False)
    created_at = Column(Text, nullable=False)


class ProcessedDocument(Base):
    """
    Audit record for every document processed by the OCR pipeline.

    Stores classification result, extracted fields (JSON), review flags,
    and (for referrals) whether the referral was pushed downstream or denied.
    """

    __tablename__ = "processed_documents"

    id = Column(String(36), primary_key=True, default=_uuid)
    filename = Column(String(500), nullable=False)
    # SHA-256 hex of raw uploaded bytes — used for duplicate detection
    file_hash = Column(String(64), nullable=True)
    # "referral" | "insurance_card" | "lab_result"
    doc_type = Column(String(50), nullable=False)
    # Stored as string to avoid float precision issues
    classification_confidence = Column(String(10), nullable=False)
    # JSON: per-field {value, confidence, needs_review}
    extracted_fields = Column(Text, nullable=False)
    # JSON: [{field, reason, confidence}]
    review_flags = Column(Text, nullable=False)
    # JSON: [{test_name, value, reference_range}] — populated for lab_result only
    out_of_range_flags = Column(Text, nullable=True)
    # "true" | "false" — populated for referral only
    pushed_downstream = Column(String(5), nullable=True)
    deny_back_letter = Column(Text, nullable=True)
    # FKs to type-specific tables
    referral_id = Column(String(36), nullable=True)
    insurance_card_id = Column(String(36), nullable=True)
    lab_result_id = Column(String(36), nullable=True)
    # Duplicate detection
    is_duplicate = Column(String(5), nullable=True)   # "true" | "false"
    duplicate_of = Column(String(36), nullable=True)  # doc id of first seen
    # How many extraction attempts were made (retry mechanism)
    extraction_attempts = Column(Integer, nullable=True)
    created_at = Column(Text, nullable=False)


class ProcessingJob(Base):
    """
    Tracks an async OCR processing job submitted via POST /process.

    Lifecycle:  queued → processing → done
                                    → failed

    file_bytes is stored temporarily so the background task can access
    the uploaded file after the HTTP request has closed. It is nulled out
    once the job reaches a terminal state (done / failed) to free space.
    """

    __tablename__ = "processing_jobs"

    id = Column(String(36), primary_key=True, default=_uuid)
    filename = Column(String(500), nullable=False)
    # Raw uploaded bytes — cleared after processing completes
    file_bytes = Column(LargeBinary, nullable=True)
    # "queued" | "processing" | "done" | "failed"
    status = Column(String(20), nullable=False, default="queued")
    error_message = Column(Text, nullable=True)
    # FK to processed_documents once pipeline completes
    document_id = Column(String(36), nullable=True)
    # Full ProcessingResult JSON stored on success
    result_json = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)
    started_at = Column(Text, nullable=True)
    completed_at = Column(Text, nullable=True)


class InsuranceCard(Base):
    """Extracted insurance card fields (confidence >= 0.7 only; others stored as NULL)."""

    __tablename__ = "insurance_cards"

    id = Column(String(36), primary_key=True, default=_uuid)
    processed_document_id = Column(String(36), nullable=False)
    member_name = Column(String(200), nullable=True)
    member_id = Column(String(50), nullable=True)
    group_number = Column(String(50), nullable=True)
    payer_id = Column(String(50), nullable=True)
    co_pay_specialist = Column(String(50), nullable=True)
    effective_date = Column(String(30), nullable=True)
    created_at = Column(Text, nullable=False)


class LabResult(Base):
    """Patient-level lab result metadata. Test rows are in lab_test_rows."""

    __tablename__ = "lab_results"

    id = Column(String(36), primary_key=True, default=_uuid)
    processed_document_id = Column(String(36), nullable=False)
    patient_name = Column(String(200), nullable=True)
    dob = Column(String(20), nullable=True)
    ordering_provider_name = Column(String(200), nullable=True)
    report_date = Column(String(30), nullable=True)
    created_at = Column(Text, nullable=False)


class LabTestRow(Base):
    """One test result row from a lab report (confidence >= 0.7 only)."""

    __tablename__ = "lab_test_rows"

    id = Column(String(36), primary_key=True, default=_uuid)
    lab_result_id = Column(String(36), nullable=False)
    test_name = Column(String(200), nullable=False)
    value = Column(String(100), nullable=True)
    unit = Column(String(50), nullable=True)
    reference_range = Column(String(100), nullable=True)
    out_of_range = Column(String(5), nullable=True)   # "true" | "false"
    confidence = Column(String(10), nullable=True)
    created_at = Column(Text, nullable=False)


class Referral(Base):
    """
    An inbound referral that triggers an outbound patient-contact call campaign.

    Shared with the OCR pipeline: the OCR service inserts rows here after
    extracting referral data from faxed documents.

    Status lifecycle:
        referral_received → in_progress → contacted  (patient answered)
                                        → closed     (3 unanswered attempts)
    """

    __tablename__ = "referrals"

    id = Column(String(36), primary_key=True, default=_uuid)

    # ── Patient demographics ─────────────────────────────────────────────────
    patient_name = Column(String(200), nullable=False)
    dob = Column(String(20), nullable=False, comment="YYYY-MM-DD")
    phone = Column(String(30), nullable=False, comment="E.164 format preferred")

    # ── Insurance ────────────────────────────────────────────────────────────
    insurance_carrier = Column(String(100), nullable=False)
    member_id = Column(String(50), nullable=False)
    group_number = Column(String(50), nullable=True)

    # ── Referring provider ───────────────────────────────────────────────────
    referring_provider = Column(String(200), nullable=False)
    npi = Column(String(20), nullable=True, comment="Referring provider NPI")

    # ── Clinical ─────────────────────────────────────────────────────────────
    reason = Column(Text, nullable=False)
    urgency = Column(String(50), nullable=False, comment="e.g. Routine, Urgent, STAT")

    # ── Outbound call tracking ───────────────────────────────────────────────
    # "referral_received" | "in_progress" | "contacted" | "closed"
    status = Column(String(30), nullable=False, default="referral_received")
    attempt_count = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(Text, nullable=True, comment="ISO 8601 UTC")
    next_attempt_at = Column(Text, nullable=True, comment="ISO 8601 UTC")
    # Vapi call.id for the active in-flight call; NULL when no call is in progress
    current_call_id = Column(Text, nullable=True)
    # Final or intermediate call outcome: "answered" | "voicemail" | "no_answer"
    # | "Patient reached — appointment pending" | "patient unreachable — 3 attempts"
    outcome = Column(Text, nullable=True)

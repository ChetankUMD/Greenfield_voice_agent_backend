"""
Output schemas for the OCR pipeline.
"""
from typing import Any, Literal, Optional
from pydantic import BaseModel


class FieldValue(BaseModel):
    value: Optional[str]
    confidence: float
    needs_review: bool = False


class LabTestValue(BaseModel):
    test_name: str
    value: Optional[str]
    unit: Optional[str]
    reference_range: Optional[str]
    confidence: float
    out_of_range: bool = False


class ReviewFlag(BaseModel):
    field: str
    reason: str
    confidence: float


class OutOfRangeFlag(BaseModel):
    test_name: str
    value: str
    reference_range: str


class ProcessingResult(BaseModel):
    document_id: str
    filename: str
    document_type: str
    classification_confidence: float
    fields: dict[str, Any]
    review_flags: list[ReviewFlag]
    out_of_range_flags: list[OutOfRangeFlag]
    # Referral-specific
    pushed_downstream: Optional[bool]
    referral_id: Optional[str]
    deny_back_letter: Optional[str]
    # Type-specific DB record IDs
    insurance_card_id: Optional[str] = None
    lab_result_id: Optional[str] = None
    # Duplicate detection
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None
    # Retry tracking
    extraction_attempts: int = 1


# ── Async job schemas ─────────────────────────────────────────────────────────

class JobSubmitResponse(BaseModel):
    """Returned immediately by POST /process (HTTP 202)."""
    job_id: str
    status: Literal["queued"]
    filename: str
    poll_url: str


class JobStatusResponse(BaseModel):
    """Returned by GET /process/{job_id}."""
    job_id: str
    # "queued" | "processing" | "done" | "failed"
    status: str
    filename: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    # Populated when status == "done"
    result: Optional[ProcessingResult] = None
    # Populated when status == "failed"
    error: Optional[str] = None

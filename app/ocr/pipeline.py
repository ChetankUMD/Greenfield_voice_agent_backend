"""
OCR pipeline for medical document processing.

Phases:
  1   Ingest        — accept file bytes, convert PDF/image to JPEG
  2   Classify      — Claude Vision → referral | insurance_card | lab_result + confidence
  2b  Duplicate     — SHA-256 file hash checked against processed_documents
  3   Extract       — Claude Vision → type-specific fields with per-field confidence
  3b  Retry         — if >50% of required fields are low-confidence, retry up to MAX_RETRIES
  4   Threshold     — confidence < CONFIDENCE_THRESHOLD OR null → needs_review flag
  5   Lab rules     — compare test values against reference ranges, flag out-of-range
  6   Referral gate — complete → insert to referrals; incomplete → deny-back letter
  6b  Type storage  — write InsuranceCard / LabResult+LabTestRow rows
  7   Audit storage — write ProcessedDocument row (always)
  8   Return        — structured JSON (ProcessingResult)

Constants:
  CONFIDENCE_THRESHOLD = 0.7   — field below this is flagged for human review
  RETRY_TRIGGER        = 0.5   — retry if >50% of required fields are flagged
  MAX_RETRIES          = 2     — maximum extra extraction attempts (3 total)
"""
import base64
import hashlib
import io
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import anthropic
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import ANTHROPIC_API_KEY
from ..models import InsuranceCard, LabResult, LabTestRow, ProcessedDocument, Referral
from .exceptions import ClassificationError, ExtractionError, IngestError, OCRError, StorageError
from .schemas import (
    FieldValue,
    LabTestValue,
    OutOfRangeFlag,
    ProcessingResult,
    ReviewFlag,
)

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.7
RETRY_TRIGGER = 0.5
MAX_RETRIES = 2

REFERRAL_REQUIRED_FIELDS = [
    "patient_name", "dob", "phone", "insurance_carrier", "member_id",
    "group_number", "referring_provider_name", "referring_provider_npi",
    "reason_for_referral", "urgency",
]
INSURANCE_CARD_REQUIRED_FIELDS = [
    "member_name", "member_id", "group_number", "payer_id",
    "co_pay_specialist", "effective_date",
]
LAB_RESULT_SCALAR_FIELDS = [
    "patient_name", "dob", "ordering_provider_name", "report_date",
]
_REQUIRED_FIELDS_BY_TYPE = {
    "referral": REFERRAL_REQUIRED_FIELDS,
    "insurance_card": INSURANCE_CARD_REQUIRED_FIELDS,
    "lab_result": LAB_RESULT_SCALAR_FIELDS,
}

# ── Prompts ───────────────────────────────────────────────────────────────────
#
# Prompt caching strategy
# ───────────────────────
# Anthropic caches the largest static prefix it can find in each request.
# Minimum cacheable size for claude-sonnet-4-6: 1024 tokens.
#
# What changes per call:
#   • The image bytes (always different → never cached)
#   • The short user instruction ("classify this" / "extract referral fields")
#
# What is static:
#   • _OCR_SYSTEM_PROMPT — role, all field definitions, all JSON templates,
#     confidence rules.  Marked with cache_control → cached after first call.
#   • The per-doc-type user template — marked with a second cache_control →
#     separately cached per doc type (all referral calls share one cache slot,
#     all insurance card calls share another, etc.).
#
# Cache economics (claude-sonnet-4-6):
#   cache write  = 1.25× base input price (first call only)
#   cache read   = 0.10× base input price (all subsequent calls within 5 min)
#   → ~90 % cost reduction on the cached token count.

# ── Shared system prompt (static, ≥1024 tokens, cached) ──────────────────────

_OCR_SYSTEM_PROMPT = """
You are a medical document OCR specialist for Greenfield Cardiology. You extract \
structured information from faxed medical documents with high precision. \
You process three document types: patient referrals, insurance cards, and laboratory results.

## Output Rules (apply to every response)

1. Return ONLY valid JSON — no markdown fences, no prose, no preamble, no trailing text.
2. For every scalar field provide exactly: {"value": <string|null>, "confidence": <float>}
3. Set value to null and confidence to 0.0 when a field is absent or completely illegible.
4. Never invent, guess, or infer a value you cannot clearly see in the document.
5. Preserve exact text found in the document; do not reformat unless a field rule requires it.
6. Dates: prefer YYYY-MM-DD; preserve original format when the year/month/day order is ambiguous.

## Confidence Scoring

Rate your certainty that you read the field correctly:

  1.0 — clearly printed, unambiguous, fully readable
  0.9 — clearly readable with negligible uncertainty
  0.8 — readable but slightly blurred, faint, or small font
  0.7 — partially readable or partially obscured; meaning is still clear
  0.6 — significant legibility difficulty; meaningful uncertainty remains
  0.0 — field not found anywhere in the document, or completely illegible

## Document Type: REFERRAL

A referral is a form sent from one provider to another requesting specialist care.

### Required fields

patient_name
  Preserve as written (e.g. "James Patterson" or "Patterson, James A.").
  Look in: patient demographics box, usually near the top.

dob
  Patient date of birth. Format as YYYY-MM-DD when possible.
  Labels: "DOB:", "Date of Birth:", "Birth Date:", "D.O.B."

phone
  Patient contact phone. Preserve original format; do not normalise.
  Labels: "Phone:", "Tel:", "Cell:", "Contact:", "Ph:"

insurance_carrier
  Full name of the insurance company as printed.
  Examples: "Blue Cross Blue Shield", "Aetna", "UnitedHealthcare", "Cigna"

member_id
  Exact alphanumeric member or subscriber ID.
  Labels: "Member ID:", "Subscriber ID:", "Policy #:", "ID #:"

group_number
  Exact group number or group name/ID.
  Labels: "Group #:", "Group No:", "Grp:", "Group Name:"

referring_provider_name
  Full name including prefix/credentials if shown.
  Example: "Dr. Sarah Chen, MD" or "John Smith NP"
  Labels: "Referring Provider:", "From:", "Referred by:", "Ordering Physician:"

referring_provider_npi
  10-digit National Provider Identifier number only (digits only).
  Labels: "NPI:", "NPI #:", "National Provider Identifier:"

reason_for_referral
  Complete clinical reason, diagnosis, or chief complaint text.
  Labels: "Reason:", "Diagnosis:", "ICD-10:", "Chief Complaint:", "Reason for Referral:"

urgency
  Normalize to exactly one of: Routine, Urgent, or STAT.
  Common source values → mapping:
    Routine / Non-urgent / Elective / Standard     → Routine
    Urgent / Priority / Expedited / Soon           → Urgent
    STAT / Emergency / Immediate / Critical        → STAT
  May appear as a checkbox, rubber stamp, or typed text.

### Referral JSON template

{
  "patient_name":           {"value": null, "confidence": 0.0},
  "dob":                    {"value": null, "confidence": 0.0},
  "phone":                  {"value": null, "confidence": 0.0},
  "insurance_carrier":      {"value": null, "confidence": 0.0},
  "member_id":              {"value": null, "confidence": 0.0},
  "group_number":           {"value": null, "confidence": 0.0},
  "referring_provider_name":{"value": null, "confidence": 0.0},
  "referring_provider_npi": {"value": null, "confidence": 0.0},
  "reason_for_referral":    {"value": null, "confidence": 0.0},
  "urgency":                {"value": null, "confidence": 0.0}
}

## Document Type: INSURANCE_CARD

An insurance membership card or insurance information document.

### Required fields

member_name  — name exactly as printed on the card
member_id    — member or subscriber ID (e.g. "XYZ123456789")
group_number — group number or plan ID (labels: "Group", "Grp", "Plan")
payer_id     — electronic payer ID, typically 5 alphanumeric characters used for claims
               (may appear on the back; labels: "Payer ID", "Electronic Payer ID")
co_pay_specialist — dollar amount for specialist office visits; include $ if shown
                    (look in copay grid under "Specialist", "SPC", "Specialty Care")
effective_date — coverage start date; format YYYY-MM-DD when possible
                 (labels: "Effective:", "Eff. Date:", "Coverage From:", "Valid From:")

### Insurance card JSON template

{
  "member_name":      {"value": null, "confidence": 0.0},
  "member_id":        {"value": null, "confidence": 0.0},
  "group_number":     {"value": null, "confidence": 0.0},
  "payer_id":         {"value": null, "confidence": 0.0},
  "co_pay_specialist":{"value": null, "confidence": 0.0},
  "effective_date":   {"value": null, "confidence": 0.0}
}

## Document Type: LAB_RESULT

A laboratory test results report.

### Scalar fields

patient_name          — full patient name
dob                   — date of birth, format YYYY-MM-DD
ordering_provider_name — name of the ordering/requesting physician
report_date           — date the report was generated or specimen collected, format YYYY-MM-DD

### Array field: test_values

Extract EVERY individual test result visible in the document. Do not skip any rows.
For each test entry:

  test_name       — test name as printed (e.g. "Hemoglobin", "WBC", "Glucose", "TSH")
  value           — result as printed (e.g. "12.5", "Negative", "Reactive", "142")
  unit            — unit of measure (e.g. "g/dL", "K/uL", "mg/dL") — null if absent
  reference_range — normal range as printed (e.g. "13.5-17.5", ">60", "<100",
                    "Negative") — null if not shown
  confidence      — your confidence in this specific test row

### Lab result JSON template

{
  "patient_name":           {"value": null, "confidence": 0.0},
  "dob":                    {"value": null, "confidence": 0.0},
  "ordering_provider_name": {"value": null, "confidence": 0.0},
  "report_date":            {"value": null, "confidence": 0.0},
  "test_values": [
    {
      "test_name": "...",
      "value": "...",
      "unit": "...",
      "reference_range": "...",
      "confidence": 0.0
    }
  ]
}

## Handling Difficult Documents

- Handwritten text: extract what you can; lower confidence to reflect legibility.
- Faint or light print: examine carefully before assigning low confidence.
- Stamps / watermarks: may contain urgency codes or routing information.
- Tables: read every row systematically; never skip a test value row.
- Multi-column layouts: read each column independently from top to bottom.
- Cut-off or edge text: provide your best reading with reduced confidence.
- Fax artefacts (streaks, speckles): do not mistake artefacts for characters.
""".strip()

# ── Short user messages (one per task, also cached per doc-type) ──────────────

_CLASSIFY_USER = (
    "Classify this document. "
    "Return JSON only: "
    '{{"document_type": "referral|insurance_card|lab_result", "confidence": 0.00}}'
)

_EXTRACT_USER = {
    "referral":       "Extract all fields using the REFERRAL JSON template. Return JSON only.",
    "insurance_card": "Extract all fields using the INSURANCE_CARD JSON template. Return JSON only.",
    "lab_result":     "Extract all fields including every test row using the LAB_RESULT JSON template. Return JSON only.",
}

_RETRY_PREFIX = (
    "IMPORTANT: A previous extraction attempt produced low confidence scores. "
    "Please examine the document much more carefully — check all areas including "
    "margins, headers, footers, tables, checkboxes, stamps, handwriting, and faint print. "
    "Extract everything you can see, even if only partially legible.\n\n"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _elapsed(t0: float) -> str:
    return f"{time.perf_counter() - t0:.2f}s"


# ── Phase 1 — Ingest ──────────────────────────────────────────────────────────

def _ingest(file_bytes: bytes, filename: str) -> tuple[bytes, str]:
    t0 = time.perf_counter()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    size_kb = len(file_bytes) / 1024

    try:
        if ext == "pdf":
            try:
                import fitz  # PyMuPDF
            except ImportError:
                raise IngestError(
                    "pymupdf is not installed. Run: pip install pymupdf"
                )
            try:
                doc = fitz.open(stream=file_bytes, filetype="pdf")
            except Exception as e:
                raise IngestError(f"Could not open PDF '{filename}': {e}") from e

            if len(doc) == 0:
                raise IngestError(f"PDF '{filename}' contains no pages.")

            page = doc[0]
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            img_bytes = pix.tobytes("jpeg")
            doc.close()

            logger.info(
                "[OCR] [ingest] %s  size=%.1fKB  pdf→jpeg  out=%.1fKB  (%s)",
                filename, size_kb, len(img_bytes) / 1024, _elapsed(t0),
            )
            return img_bytes, "image/jpeg"

        # Image path
        try:
            img = Image.open(io.BytesIO(file_bytes))
        except UnidentifiedImageError:
            raise IngestError(
                f"'{filename}' is not a readable image. "
                f"Accepted formats: PDF, JPEG, PNG, TIFF, WebP."
            )
        except Exception as e:
            raise IngestError(f"Could not open image '{filename}': {e}") from e

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        img_bytes = buf.getvalue()

        logger.info(
            "[OCR] [ingest] %s  size=%.1fKB  %s→jpeg  out=%.1fKB  (%s)",
            filename, size_kb, ext or "img", len(img_bytes) / 1024, _elapsed(t0),
        )
        return img_bytes, "image/jpeg"

    except IngestError:
        raise
    except Exception as e:
        raise IngestError(f"Unexpected ingest error for '{filename}': {e}") from e


# ── Claude vision helper ───────────────────────────────────────────────────────

def _call_claude(
    image_bytes: bytes,
    media_type: str,
    user_text: str,
    *,
    label: str = "call",
) -> str:
    """
    Call Claude Vision with prompt caching enabled.

    Cache layout (static prefix first, variable image last):

      system  [cache_control] ← _OCR_SYSTEM_PROMPT  (~1 400 tokens, cached)
      user[0] text [cache_control] ← short task instruction   (cached per doc-type)
      user[1] image                ← document image   (NOT cached, changes every call)

    The API response includes usage.cache_read_input_tokens / cache_creation_input_tokens.
    Both are logged so you can verify the cache is working.

    Raises ExtractionError on any API failure.
    """
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=2, timeout=60.0)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": _OCR_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        # ① Text instruction — cached per doc-type
                        {
                            "type": "text",
                            "text": user_text,
                            "cache_control": {"type": "ephemeral"},
                        },
                        # ② Image — NOT cached (different every call)
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                    ],
                }
            ],
        )
    except anthropic.AuthenticationError as e:
        raise ExtractionError(
            "Anthropic API authentication failed — check ANTHROPIC_API_KEY in .env"
        ) from e
    except anthropic.RateLimitError as e:
        raise ExtractionError(
            "Anthropic API rate limit exceeded — wait a moment and retry"
        ) from e
    except anthropic.APITimeoutError as e:
        raise ExtractionError(
            "Anthropic API request timed out after 60s — try again"
        ) from e
    except anthropic.APIConnectionError as e:
        raise ExtractionError(
            f"Could not reach Anthropic API (network error): {e}"
        ) from e
    except anthropic.APIStatusError as e:
        raise ExtractionError(
            f"Anthropic API returned HTTP {e.status_code}: {e.message}"
        ) from e
    except anthropic.APIError as e:
        raise ExtractionError(f"Anthropic API error: {e}") from e

    usage = response.usage
    cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_status  = "HIT" if cache_read > 0 else ("WRITE" if cache_created > 0 else "MISS")

    logger.info(
        "[OCR] [claude/%s] in=%d  out=%d  cache_read=%d  cache_write=%d  [%s]",
        label,
        usage.input_tokens,
        usage.output_tokens,
        cache_read,
        cache_created,
        cache_status,
    )
    return response.content[0].text


def _parse_json(text: str, context: str = "") -> dict:
    """Extract a JSON object from a Claude response, stripping markdown fences."""
    cleaned = re.sub(r"```(?:json)?\n?", "", text).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        snippet = text[:200].replace("\n", " ")
        raise ValueError(
            f"No JSON object found in Claude response"
            + (f" ({context})" if context else "")
            + f". Got: {snippet!r}"
        )
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Claude returned malformed JSON"
            + (f" ({context})" if context else "")
            + f": {e}"
        ) from e


# ── Phase 2 — Classify ────────────────────────────────────────────────────────

def _classify(image_bytes: bytes, media_type: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    try:
        raw = _call_claude(image_bytes, media_type, _CLASSIFY_USER, label="classify")
    except ExtractionError as e:
        raise ClassificationError(f"Claude API failed during classification: {e}") from e

    try:
        data = _parse_json(raw, context="classification")
    except ValueError as e:
        raise ClassificationError(f"Could not parse classification response: {e}") from e

    doc_type = str(data.get("document_type", "")).lower().strip()
    if doc_type not in ("referral", "insurance_card", "lab_result"):
        raise ClassificationError(
            f"Unrecognised document type {doc_type!r}. "
            f"Expected: referral, insurance_card, or lab_result."
        )

    confidence = float(data.get("confidence", 0.0))
    logger.info(
        "[OCR] [classify] type=%s  confidence=%.2f  (%s)",
        doc_type, confidence, _elapsed(t0),
    )
    return doc_type, confidence


# ── Phase 2b — Duplicate detection ───────────────────────────────────────────

def _compute_file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def _check_duplicate(file_hash: str, db: Session) -> tuple[bool, Optional[str]]:
    try:
        existing = (
            db.query(ProcessedDocument)
            .filter(ProcessedDocument.file_hash == file_hash)
            .order_by(ProcessedDocument.created_at)
            .first()
        )
    except SQLAlchemyError as e:
        # Non-fatal — log and continue without duplicate detection
        logger.warning("[OCR] [duplicate] DB query failed, skipping check: %s", e)
        return False, None

    if existing:
        logger.warning(
            "[OCR] [duplicate] file_hash=%s already seen as document_id=%s",
            file_hash[:12] + "...", existing.id,
        )
        return True, existing.id

    logger.debug("[OCR] [duplicate] hash=%s — no prior match", file_hash[:12] + "...")
    return False, None


# ── Phase 3 — Extract (with retry) ───────────────────────────────────────────

def _extract_raw(image_bytes: bytes, media_type: str, doc_type: str, attempt: int = 0) -> dict:
    user_text = _EXTRACT_USER[doc_type]
    if attempt > 0:
        user_text = _RETRY_PREFIX + user_text
    raw = _call_claude(
        image_bytes, media_type, user_text,
        label=f"extract/{doc_type}/attempt{attempt + 1}",
    )
    return _parse_json(raw, context=f"{doc_type} extraction attempt {attempt + 1}")


def _apply_threshold(raw_fields: dict, doc_type: str) -> tuple[dict, list[ReviewFlag]]:
    """
    Tag every field with confidence < CONFIDENCE_THRESHOLD or null value as
    needs_review=True. Never silently fills or guesses missing data.
    """
    review_flags: list[ReviewFlag] = []
    annotated: dict = {}

    for key in _REQUIRED_FIELDS_BY_TYPE[doc_type]:
        raw = raw_fields.get(key, {"value": None, "confidence": 0.0})
        value = raw.get("value")
        confidence = float(raw.get("confidence", 0.0))
        needs_review = value is None or confidence < CONFIDENCE_THRESHOLD

        annotated[key] = FieldValue(value=value, confidence=confidence, needs_review=needs_review)

        if needs_review:
            reason = (
                "not found in document"
                if value is None
                else f"low confidence ({confidence:.2f})"
            )
            review_flags.append(ReviewFlag(field=key, reason=reason, confidence=confidence))

    if doc_type == "lab_result":
        tests: list[LabTestValue] = []
        for t in raw_fields.get("test_values", []):
            tests.append(
                LabTestValue(
                    test_name=t.get("test_name") or "",
                    value=t.get("value"),
                    unit=t.get("unit"),
                    reference_range=t.get("reference_range"),
                    confidence=float(t.get("confidence", 0.0)),
                )
            )
        annotated["test_values"] = tests

    return annotated, review_flags


def _extraction_poor_quality(review_flags: list[ReviewFlag], doc_type: str) -> bool:
    required = _REQUIRED_FIELDS_BY_TYPE[doc_type]
    flagged = sum(1 for f in review_flags if f.field in required)
    return flagged > len(required) * RETRY_TRIGGER


def _extract_with_retry(
    image_bytes: bytes, media_type: str, doc_type: str
) -> tuple[dict, list[ReviewFlag], int]:
    """
    Run extraction up to 1 + MAX_RETRIES times.
    Uses the attempt with the fewest flagged required fields.
    JSON parse failures count as a failed attempt and trigger a retry.
    """
    best_annotated: Optional[dict] = None
    best_flags: Optional[list] = None
    best_score = float("inf")
    last_error: Optional[Exception] = None
    required_count = len(_REQUIRED_FIELDS_BY_TYPE[doc_type])

    for attempt in range(1 + MAX_RETRIES):
        t0 = time.perf_counter()
        try:
            raw = _extract_raw(image_bytes, media_type, doc_type, attempt)
        except (ExtractionError, ValueError) as e:
            last_error = e
            logger.warning(
                "[OCR] [extract] attempt=%d/%d FAILED — %s  (%.2fs)",
                attempt + 1, 1 + MAX_RETRIES, e, time.perf_counter() - t0,
            )
            if attempt < MAX_RETRIES:
                continue
            raise ExtractionError(
                f"All {1 + MAX_RETRIES} extraction attempts failed. "
                f"Last error: {last_error}"
            ) from last_error

        annotated, flags = _apply_threshold(raw, doc_type)
        required_flagged = sum(1 for f in flags if f.field in _REQUIRED_FIELDS_BY_TYPE[doc_type])

        logger.info(
            "[OCR] [extract] attempt=%d/%d  doc_type=%s  flagged=%d/%d  (%.2fs)",
            attempt + 1, 1 + MAX_RETRIES, doc_type,
            required_flagged, required_count, time.perf_counter() - t0,
        )

        if best_annotated is None or required_flagged < best_score:
            best_annotated = annotated
            best_flags = flags
            best_score = required_flagged

        if not _extraction_poor_quality(flags, doc_type) or attempt >= MAX_RETRIES:
            break

        logger.warning(
            "[OCR] [extract] quality poor — %.0f%% of required fields flagged, retrying",
            (required_flagged / required_count) * 100,
        )

    return best_annotated, best_flags, attempt + 1  # type: ignore[return-value]


# ── Phase 5 — Lab out-of-range check ─────────────────────────────────────────

def _parse_numeric(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def _is_out_of_range(value_str: Optional[str], range_str: Optional[str]) -> bool:
    if value_str is None or range_str is None:
        return False
    val = _parse_numeric(value_str)
    if val is None:
        return False
    r = range_str.strip()
    m = re.match(r"^([\d.]+)\s*[-–]\s*([\d.]+)$", r)
    if m:
        return val < float(m.group(1)) or val > float(m.group(2))
    m = re.match(r"^>=?\s*([\d.]+)$", r)
    if m:
        return val < float(m.group(1))
    m = re.match(r"^<=?\s*([\d.]+)$", r)
    if m:
        return val > float(m.group(1))
    return False


def _check_lab_ranges(annotated_fields: dict) -> list[OutOfRangeFlag]:
    flags: list[OutOfRangeFlag] = []
    for test in annotated_fields.get("test_values", []):
        if _is_out_of_range(test.value, test.reference_range):
            test.out_of_range = True
            flags.append(
                OutOfRangeFlag(
                    test_name=test.test_name,
                    value=test.value or "",
                    reference_range=test.reference_range or "",
                )
            )
    if flags:
        logger.info(
            "[OCR] [lab_ranges] %d out-of-range value(s): %s",
            len(flags), [f.test_name for f in flags],
        )
    else:
        logger.info("[OCR] [lab_ranges] all values within reference range")
    return flags


# ── Phase 6 — Referral gate + deny-back letter ───────────────────────────────

def _generate_deny_back_letter(
    missing: list[ReviewFlag],
    referring_provider: Optional[str],
    patient_name: Optional[str],
) -> str:
    provider = referring_provider or "Referring Provider"
    patient = patient_name or "Unknown Patient"
    lines = [
        f"  • {f.field.replace('_', ' ').title()} — {f.reason}" for f in missing
    ]
    return (
        "REFERRAL INCOMPLETE — ADDITIONAL INFORMATION REQUIRED\n\n"
        f"To: {provider}\n"
        f"Re: Patient {patient}\n\n"
        "This referral cannot be processed as submitted. The following required "
        "items are missing or could not be verified with sufficient confidence:\n\n"
        + "\n".join(lines)
        + "\n\nPlease resubmit the referral with all required fields completed.\n\n"
        "Greenfield Cardiology Referral Office"
    )


def _referral_gate(
    annotated: dict,
    review_flags: list[ReviewFlag],
    db: Session,
) -> tuple[bool, Optional[str], Optional[str]]:
    missing = [f for f in review_flags if f.field in REFERRAL_REQUIRED_FIELDS]

    if missing:
        provider_field = annotated.get("referring_provider_name")
        name_field = annotated.get("patient_name")
        letter = _generate_deny_back_letter(
            missing,
            referring_provider=provider_field.value if provider_field else None,
            patient_name=name_field.value if name_field else None,
        )
        logger.warning(
            "[OCR] [referral_gate] DENIED — %d missing/low-confidence field(s): %s",
            len(missing), [f.field for f in missing],
        )
        return False, letter, None

    def _v(key: str) -> str:
        return annotated[key].value or ""

    try:
        referral = Referral(
            id=str(uuid.uuid4()),
            patient_name=_v("patient_name"),
            dob=_v("dob"),
            phone=_v("phone"),
            insurance_carrier=_v("insurance_carrier"),
            member_id=_v("member_id"),
            group_number=_v("group_number"),
            referring_provider=_v("referring_provider_name"),
            npi=_v("referring_provider_npi"),
            reason=_v("reason_for_referral"),
            urgency=_v("urgency"),
            status="referral_received",
        )
        db.add(referral)
        db.commit()
        db.refresh(referral)
    except SQLAlchemyError as e:
        db.rollback()
        raise StorageError(f"Failed to insert referral into database: {e}") from e

    logger.info(
        "[OCR] [referral_gate] APPROVED — referral_id=%s  patient=%s",
        referral.id, _v("patient_name"),
    )
    return True, None, referral.id


# ── Phase 6b — Type-specific table storage ────────────────────────────────────

def _confident_value(annotated: dict, key: str) -> Optional[str]:
    f = annotated.get(key)
    if not isinstance(f, FieldValue):
        return None
    return f.value if f.confidence >= CONFIDENCE_THRESHOLD else None


def _store_insurance_card(annotated: dict, processed_doc_id: str, db: Session) -> str:
    now = datetime.now(timezone.utc).isoformat()
    try:
        card = InsuranceCard(
            id=str(uuid.uuid4()),
            processed_document_id=processed_doc_id,
            member_name=_confident_value(annotated, "member_name"),
            member_id=_confident_value(annotated, "member_id"),
            group_number=_confident_value(annotated, "group_number"),
            payer_id=_confident_value(annotated, "payer_id"),
            co_pay_specialist=_confident_value(annotated, "co_pay_specialist"),
            effective_date=_confident_value(annotated, "effective_date"),
            created_at=now,
        )
        db.add(card)
        db.commit()
        db.refresh(card)
    except SQLAlchemyError as e:
        db.rollback()
        raise StorageError(f"Failed to store insurance card: {e}") from e

    confident_fields = [
        k for k in INSURANCE_CARD_REQUIRED_FIELDS
        if _confident_value(annotated, k) is not None
    ]
    logger.info(
        "[OCR] [store_insurance_card] id=%s  confident_fields=%s",
        card.id, confident_fields,
    )
    return card.id


def _store_lab_result(
    annotated: dict,
    out_of_range_flags: list[OutOfRangeFlag],
    processed_doc_id: str,
    db: Session,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    oor_names = {f.test_name for f in out_of_range_flags}

    try:
        result = LabResult(
            id=str(uuid.uuid4()),
            processed_document_id=processed_doc_id,
            patient_name=_confident_value(annotated, "patient_name"),
            dob=_confident_value(annotated, "dob"),
            ordering_provider_name=_confident_value(annotated, "ordering_provider_name"),
            report_date=_confident_value(annotated, "report_date"),
            created_at=now,
        )
        db.add(result)
        db.flush()  # obtain result.id before inserting child rows

        stored_tests = 0
        skipped_tests = 0
        for test in annotated.get("test_values", []):
            if not isinstance(test, LabTestValue):
                continue
            if test.confidence < CONFIDENCE_THRESHOLD:
                skipped_tests += 1
                logger.debug(
                    "[OCR] [store_lab_result] skipping test %r — confidence %.2f < %.2f",
                    test.test_name, test.confidence, CONFIDENCE_THRESHOLD,
                )
                continue
            row = LabTestRow(
                id=str(uuid.uuid4()),
                lab_result_id=result.id,
                test_name=test.test_name,
                value=test.value,
                unit=test.unit,
                reference_range=test.reference_range,
                out_of_range="true" if test.test_name in oor_names else "false",
                confidence=str(round(test.confidence, 4)),
                created_at=now,
            )
            db.add(row)
            stored_tests += 1

        db.commit()
        db.refresh(result)
    except SQLAlchemyError as e:
        db.rollback()
        raise StorageError(f"Failed to store lab result: {e}") from e

    logger.info(
        "[OCR] [store_lab_result] id=%s  tests_stored=%d  tests_skipped=%d  out_of_range=%d",
        result.id, stored_tests, skipped_tests, len(out_of_range_flags),
    )
    return result.id


# ── Phase 7 — Audit storage ───────────────────────────────────────────────────

def _store_audit(
    *,
    filename: str,
    file_hash: str,
    doc_type: str,
    classification_confidence: float,
    annotated: dict,
    review_flags: list[ReviewFlag],
    out_of_range_flags: list[OutOfRangeFlag],
    pushed_downstream: Optional[bool],
    deny_back_letter: Optional[str],
    referral_id: Optional[str],
    insurance_card_id: Optional[str],
    lab_result_id: Optional[str],
    is_duplicate: bool,
    duplicate_of: Optional[str],
    extraction_attempts: int,
    db: Session,
) -> str:
    def _serialise(f):
        if isinstance(f, list):
            return [_serialise(x) for x in f]
        if hasattr(f, "model_dump"):
            return f.model_dump()
        return f

    try:
        doc = ProcessedDocument(
            id=str(uuid.uuid4()),
            filename=filename,
            file_hash=file_hash,
            doc_type=doc_type,
            classification_confidence=str(round(classification_confidence, 4)),
            extracted_fields=json.dumps({k: _serialise(v) for k, v in annotated.items()}),
            review_flags=json.dumps([f.model_dump() for f in review_flags]),
            out_of_range_flags=json.dumps([f.model_dump() for f in out_of_range_flags]),
            pushed_downstream=(
                "true" if pushed_downstream
                else ("false" if pushed_downstream is False else None)
            ),
            deny_back_letter=deny_back_letter,
            referral_id=referral_id,
            insurance_card_id=insurance_card_id,
            lab_result_id=lab_result_id,
            is_duplicate="true" if is_duplicate else "false",
            duplicate_of=duplicate_of,
            extraction_attempts=extraction_attempts,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
    except SQLAlchemyError as e:
        db.rollback()
        raise StorageError(f"Failed to write audit record: {e}") from e

    return doc.id


# ── Main orchestrator ─────────────────────────────────────────────────────────

def process_document_bytes(
    file_bytes: bytes, filename: str, db: Session
) -> ProcessingResult:
    """Run all pipeline phases and return a ProcessingResult."""
    pipeline_start = time.perf_counter()
    logger.info("[OCR] ── START  filename=%s  size=%.1fKB ──", filename, len(file_bytes) / 1024)

    # Phase 1 — Ingest
    image_bytes, media_type = _ingest(file_bytes, filename)

    # Phase 2 — Classify
    doc_type, class_confidence = _classify(image_bytes, media_type)

    # Phase 2b — Duplicate detection
    file_hash = _compute_file_hash(file_bytes)
    is_duplicate, duplicate_of = _check_duplicate(file_hash, db)

    # Phase 3 — Extract (with automatic retry on poor quality)
    annotated, review_flags, extraction_attempts = _extract_with_retry(
        image_bytes, media_type, doc_type
    )

    required_count = len(_REQUIRED_FIELDS_BY_TYPE[doc_type])
    required_flagged = sum(1 for f in review_flags if f.field in _REQUIRED_FIELDS_BY_TYPE[doc_type])
    logger.info(
        "[OCR] [threshold] %d/%d required fields need review  (threshold=%.0f%%)",
        required_flagged, required_count, CONFIDENCE_THRESHOLD * 100,
    )

    # Phase 5 — Lab rules
    out_of_range_flags: list[OutOfRangeFlag] = []
    if doc_type == "lab_result":
        out_of_range_flags = _check_lab_ranges(annotated)

    # Phase 6 / 6b — Type-specific downstream action
    pushed_downstream: Optional[bool] = None
    deny_back_letter: Optional[str] = None
    referral_id: Optional[str] = None
    insurance_card_id: Optional[str] = None
    lab_result_id: Optional[str] = None

    # Write the audit row first so we have a doc_id for type-table FKs,
    # then patch it after type-table IDs are known.
    doc_id = _store_audit(
        filename=filename,
        file_hash=file_hash,
        doc_type=doc_type,
        classification_confidence=class_confidence,
        annotated=annotated,
        review_flags=review_flags,
        out_of_range_flags=out_of_range_flags,
        pushed_downstream=None,
        deny_back_letter=None,
        referral_id=None,
        insurance_card_id=None,
        lab_result_id=None,
        is_duplicate=is_duplicate,
        duplicate_of=duplicate_of,
        extraction_attempts=extraction_attempts,
        db=db,
    )
    logger.info("[OCR] [audit] document_id=%s written", doc_id)

    if is_duplicate and duplicate_of:
        # Same file bytes seen before — reuse the original type-table records.
        # Do NOT insert new rows into insurance_cards / lab_results / referrals.
        orig = (
            db.query(ProcessedDocument)
            .filter(ProcessedDocument.id == duplicate_of)
            .first()
        )
        if orig:
            referral_id = orig.referral_id
            insurance_card_id = orig.insurance_card_id
            lab_result_id = orig.lab_result_id
            pushed_downstream = (
                orig.pushed_downstream == "true" if orig.pushed_downstream else None
            )
            deny_back_letter = orig.deny_back_letter
            logger.info(
                "[OCR] [duplicate] reusing type-table records from original document_id=%s  "
                "insurance_card_id=%s  lab_result_id=%s  referral_id=%s",
                duplicate_of, insurance_card_id, lab_result_id, referral_id,
            )
        else:
            # Original audit row missing — treat as a fresh document
            logger.warning(
                "[OCR] [duplicate] original document_id=%s not found in DB "
                "— processing as new",
                duplicate_of,
            )
            is_duplicate = False

    if not is_duplicate:
        if doc_type == "referral":
            pushed_downstream, deny_back_letter, referral_id = _referral_gate(
                annotated, review_flags, db
            )
        elif doc_type == "insurance_card":
            insurance_card_id = _store_insurance_card(annotated, doc_id, db)
        elif doc_type == "lab_result":
            lab_result_id = _store_lab_result(annotated, out_of_range_flags, doc_id, db)

    # Patch the audit row with type-table IDs and referral gate outcome
    try:
        db.query(ProcessedDocument).filter(ProcessedDocument.id == doc_id).update(
            {
                "pushed_downstream": (
                    "true" if pushed_downstream
                    else ("false" if pushed_downstream is False else None)
                ),
                "deny_back_letter": deny_back_letter,
                "referral_id": referral_id,
                "insurance_card_id": insurance_card_id,
                "lab_result_id": lab_result_id,
            },
            synchronize_session=False,
        )
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        logger.error("[OCR] [audit] failed to patch type-table IDs: %s", e)
        # Non-fatal — audit row exists, just missing some FK links

    total = time.perf_counter() - pipeline_start
    logger.info(
        "[OCR] ── DONE  document_id=%s  type=%s  is_duplicate=%s  "
        "review_flags=%d  out_of_range=%d  attempts=%d  total=%.2fs ──",
        doc_id, doc_type, is_duplicate,
        len(review_flags), len(out_of_range_flags),
        extraction_attempts, total,
    )

    # Phase 8 — Return structured result
    def _serialise(f):
        if isinstance(f, (FieldValue, LabTestValue)):
            return f.model_dump()
        if isinstance(f, list):
            return [_serialise(x) for x in f]
        return f

    return ProcessingResult(
        document_id=doc_id,
        filename=filename,
        document_type=doc_type,
        classification_confidence=class_confidence,
        fields={k: _serialise(v) for k, v in annotated.items()},
        review_flags=review_flags,
        out_of_range_flags=out_of_range_flags,
        pushed_downstream=pushed_downstream,
        referral_id=referral_id,
        deny_back_letter=deny_back_letter,
        insurance_card_id=insurance_card_id,
        lab_result_id=lab_result_id,
        is_duplicate=is_duplicate,
        duplicate_of=duplicate_of,
        extraction_attempts=extraction_attempts,
    )

"""
Async OCR document processing endpoints.

POST /process
    Accepts a file upload, stores it, enqueues background processing,
    and returns a job_id immediately (HTTP 202).  Processing typically
    takes 5–15 s (two Claude Vision calls + optional retries).

GET /process/{job_id}
    Poll job status.  When status == "done" the full ProcessingResult
    is embedded in the response so no second request is needed.

Job lifecycle:
    queued → processing → done
                        → failed
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import ProcessingJob
from ..ocr.exceptions import OCRError
from ..ocr.pipeline import process_document_bytes
from ..ocr.schemas import JobStatusResponse, JobSubmitResponse, ProcessingResult

logger = logging.getLogger(__name__)
router = APIRouter()

_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


# ── Background worker ─────────────────────────────────────────────────────────

def _run_pipeline_job(job_id: str) -> None:
    """
    Runs in a FastAPI background thread after the HTTP response is sent.
    Opens its own DB session — the request session is already closed by
    the time this function executes.
    """
    db = SessionLocal()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
        if not job:
            logger.error("[job/%s] not found in DB — aborting", job_id)
            return

        # ── Mark processing ───────────────────────────────────────────────────
        db.query(ProcessingJob).filter(ProcessingJob.id == job_id).update(
            {
                "status": "processing",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            synchronize_session=False,
        )
        db.commit()
        logger.info("[job/%s] status=processing  filename=%s", job_id, job.filename)

        file_bytes: bytes = job.file_bytes
        filename: str = job.filename

        # ── Run pipeline ──────────────────────────────────────────────────────
        result: ProcessingResult = process_document_bytes(file_bytes, filename, db)

        # ── Mark done ─────────────────────────────────────────────────────────
        db.query(ProcessingJob).filter(ProcessingJob.id == job_id).update(
            {
                "status": "done",
                "document_id": result.document_id,
                "result_json": result.model_dump_json(),
                "file_bytes": None,          # free storage now that processing is done
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
            synchronize_session=False,
        )
        db.commit()
        logger.info(
            "[job/%s] status=done  document_id=%s  type=%s  review_flags=%d",
            job_id, result.document_id, result.document_type, len(result.review_flags),
        )

    except Exception as exc:
        # ── Mark failed ───────────────────────────────────────────────────────
        logger.error("[job/%s] status=failed  error=%s", job_id, exc)
        try:
            db.rollback()
            db.query(ProcessingJob).filter(ProcessingJob.id == job_id).update(
                {
                    "status": "failed",
                    "error_message": str(exc),
                    "file_bytes": None,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
                synchronize_session=False,
            )
            db.commit()
        except Exception as db_exc:
            logger.error("[job/%s] could not write failed status: %s", job_id, db_exc)
    finally:
        db.close()


# ── POST /process ─────────────────────────────────────────────────────────────

@router.post("/process", status_code=202, response_model=JobSubmitResponse)
def submit_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF or image of the medical document"),
    db: Session = Depends(get_db),
):
    """
    Accept a document upload and return a job_id immediately (HTTP 202).
    Poll GET /process/{job_id} for status and result.
    """
    filename = file.filename or "upload"
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{ext}'. "
                f"Accepted: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
            ),
        )

    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    size_kb = len(file_bytes) / 1024
    job_id = str(uuid.uuid4())

    # Persist job + raw bytes so the background task can read them
    job = ProcessingJob(
        id=job_id,
        filename=filename,
        file_bytes=file_bytes,
        status="queued",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(job)
    db.commit()

    # Enqueue — runs after response is sent
    background_tasks.add_task(_run_pipeline_job, job_id)

    logger.info(
        "[/process] job queued  job_id=%s  filename=%s  size=%.1fKB",
        job_id, filename, size_kb,
    )

    return JobSubmitResponse(
        job_id=job_id,
        status="queued",
        filename=filename,
        poll_url=f"/process/{job_id}",
    )


# ── GET /process/{job_id} ─────────────────────────────────────────────────────

@router.get("/process/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    """
    Poll processing status.

    status == "queued"     — job is waiting to start
    status == "processing" — pipeline is running
    status == "done"       — result is in the response body (.result field)
    status == "failed"     — error message is in .error field
    """
    job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    result: ProcessingResult | None = None
    if job.status == "done" and job.result_json:
        try:
            result = ProcessingResult.model_validate_json(job.result_json)
        except Exception as e:
            logger.error("[job/%s] could not deserialize result_json: %s", job_id, e)

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        filename=job.filename,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        result=result,
        error=job.error_message,
    )

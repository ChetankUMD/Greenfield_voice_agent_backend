"""
CLI for the OCR document processing pipeline.

Usage:
    python process.py <file>

Examples:
    python process.py task_files/Fax-Referral.pdf
    python process.py task_files/Fax-InsuranceCard.pdf
    python process.py task_files/Fax-LabResult.pdf
"""
import json
import os
import sys

from app.db import SessionLocal
from app.models import Base
from app.db import engine
from app.ocr.pipeline import process_document_bytes


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    if not os.path.isfile(filepath):
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    with open(filepath, "rb") as fh:
        file_bytes = fh.read()

    filename = os.path.basename(filepath)

    # Ensure tables exist (idempotent)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        result = process_document_bytes(file_bytes, filename, db)
        print(json.dumps(result.model_dump(), indent=2))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

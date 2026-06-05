"""Custom exceptions for the OCR pipeline."""


class OCRError(Exception):
    """Base exception — catch this to handle any pipeline failure."""


class IngestError(OCRError):
    """File could not be read or converted to an image (corrupt, wrong format)."""


class ClassificationError(OCRError):
    """Document type could not be determined from the image."""


class ExtractionError(OCRError):
    """Field extraction failed — Claude API error or unparseable response."""


class StorageError(OCRError):
    """A database write failed during pipeline execution."""

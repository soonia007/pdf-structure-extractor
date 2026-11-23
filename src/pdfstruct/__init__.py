"""pdfstruct - read a PDF and describe its structure as JSON."""

__version__ = "0.1.0"

from .errors import (  # noqa: E402
    EncryptedPDFError,
    ExtractionTimeout,
    NotAPDFError,
    PDFStructError,
    UnreadablePDFError,
)
from .extractor import extract, extract_isolated  # noqa: E402
from .models import SCHEMA_VERSION, Document  # noqa: E402

__all__ = [
    "Document",
    "EncryptedPDFError",
    "ExtractionTimeout",
    "NotAPDFError",
    "PDFStructError",
    "SCHEMA_VERSION",
    "UnreadablePDFError",
    "__version__",
    "extract",
    "extract_isolated",
]

"""pdfstruct - read a PDF and describe its structure as JSON."""

__version__ = "0.1.0"

from .errors import (
    EncryptedPDFError,
    ExtractionTimeout,
    NotAPDFError,
    PDFStructError,
    SpecError,
    UnreadablePDFError,
)
from .extractor import extract, extract_isolated
from .fields import Spec, load_spec
from .models import SCHEMA_VERSION, Document
from .templates import Template, TemplateError, TemplateStore, extract_auto

__all__ = [
    "Document",
    "EncryptedPDFError",
    "ExtractionTimeout",
    "NotAPDFError",
    "PDFStructError",
    "Spec",
    "SpecError",
    "Template",
    "TemplateError",
    "TemplateStore",
    "SCHEMA_VERSION",
    "UnreadablePDFError",
    "__version__",
    "extract",
    "extract_auto",
    "extract_isolated",
    "load_spec",
]

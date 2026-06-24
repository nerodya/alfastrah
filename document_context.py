import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from docx import Document
from fastapi import HTTPException
from pypdf import PdfReader

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b").strip()
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "600"))
PDF_FILE = os.getenv("PDF_FILE", "documents/Онлайн-выписка-помещение.pdf").strip()
DOCX_FILE = os.getenv("DOCX_FILE", "documents/Купля-продажа.pdf").strip()


def read_pdf(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")

    reader = PdfReader(str(file_path))
    text_parts: list[str] = []

    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text()
            if text:
                text_parts.append(text)
        except Exception as exc:
            logger.warning("Failed to read PDF page %s: %s", page_number, exc)

    return "\n".join(text_parts)


def read_docx(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"DOCX file not found: {path}")

    document = Document(str(file_path))
    paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    return "\n".join(paragraphs)


def read_document(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path)
    if suffix in {".docx", ".doc"}:
        return read_docx(path)
    raise ValueError(f"Unsupported document type: {path}")


def build_document_context() -> tuple[str, dict[str, Any]]:
    logger.info("Loading PDF document: %s", PDF_FILE)
    pdf_text = read_document(PDF_FILE)

    logger.info("Loading second document: %s", DOCX_FILE)
    docx_text = read_document(DOCX_FILE)

    context = f"""
=== PDF DOCUMENT ===

{pdf_text}

=== DOCX DOCUMENT ===

{docx_text}
"""

    meta = {
        "pdf_file": PDF_FILE,
        "docx_file": DOCX_FILE,
        "pdf_chars": len(pdf_text),
        "docx_chars": len(docx_text),
        "total_chars": len(context),
    }
    logger.info(
        "Document context loaded: PDF=%s chars, DOCX=%s chars, total=%s chars",
        meta["pdf_chars"],
        meta["docx_chars"],
        meta["total_chars"],
    )
    return context, meta


class DocumentContextStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._context = ""
        self._meta: dict[str, Any] = {}
        self._loaded_at: float | None = None
        self._load_error: str | None = None

    def _build_status(self) -> dict[str, Any]:
        return {
            "loaded": bool(self._context),
            "loaded_at": self._loaded_at,
            "error": self._load_error,
            **self._meta,
        }

    def load(self) -> dict[str, Any]:
        try:
            context, meta = build_document_context()
        except Exception as exc:
            with self._lock:
                self._context = ""
                self._meta = {}
                self._loaded_at = None
                self._load_error = str(exc)
            logger.exception("Failed to load document context")
            raise

        with self._lock:
            self._context = context
            self._meta = meta
            self._loaded_at = time.time()
            self._load_error = None
            return self._build_status()

    def refresh(self) -> dict[str, Any]:
        return self.load()

    def get_context(self) -> str:
        with self._lock:
            if self._load_error and not self._context:
                raise HTTPException(503, f"Document context is not available: {self._load_error}")
            if not self._context:
                raise HTTPException(503, "Document context is not loaded yet")
            return self._context

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._build_status()


def build_ollama_prompt(question: str, context: str) -> str:
    return f"""
Ты помощник по документации.

Отвечай только на основании
предоставленного контекста.

Если ответа нет в документах,
напиши:
"Информация отсутствует в документации".

Контекст:

{context}

Вопрос:

{question}
"""


def ask_ollama(question: str, context: str) -> str:
    prompt = build_ollama_prompt(question, context)
    timeout = httpx.Timeout(
        connect=30.0,
        read=OLLAMA_TIMEOUT,
        write=30.0,
        pool=30.0,
    )

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                },
            )
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Ollama request failed: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Ollama error: {response.text}")

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "Ollama returned invalid JSON") from exc

    answer = data.get("response")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()

    raise HTTPException(502, "Ollama did not return a response")


doc_context = DocumentContextStore()

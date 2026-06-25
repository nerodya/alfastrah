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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "frob/qwen3.5-instruct:27b").strip()
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "600"))
DOCUMENTS_DIR = Path(os.getenv("DOCUMENTS_DIR", "documents"))
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}


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
            logger.warning("Failed to read PDF page %s in %s: %s", page_number, path, exc)

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


def _iter_supported_files(application_dir: Path) -> list[Path]:
    files = [
        file_path
        for file_path in application_dir.iterdir()
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    return sorted(files, key=lambda path: path.name.lower())


def build_application_context(application_id: str, application_dir: Path) -> tuple[str, dict[str, Any]]:
    files = _iter_supported_files(application_dir)
    if not files:
        logger.warning(
            "No supported documents found for application %s in %s",
            application_id,
            application_dir,
        )
        return "", {
            "application_id": application_id,
            "directory": str(application_dir),
            "files": [],
            "total_chars": 0,
        }

    text_parts: list[str] = []
    files_meta: list[dict[str, Any]] = []

    for file_path in files:
        logger.info(
            "Reading document into memory for application %s: %s",
            application_id,
            file_path,
        )
        text = read_document(str(file_path))
        char_count = len(text)
        logger.info(
            "Document loaded into memory for application %s: %s (%s chars)",
            application_id,
            file_path.name,
            char_count,
        )
        files_meta.append(
            {
                "name": file_path.name,
                "extension": file_path.suffix.lower(),
                "chars": char_count,
            }
        )
        text_parts.append(f"=== {file_path.name} ===\n\n{text}")

    context = "\n\n".join(text_parts)
    meta = {
        "application_id": application_id,
        "directory": str(application_dir),
        "files": files_meta,
        "total_chars": len(context),
    }
    logger.info(
        "Application context prepared: id=%s, files=%s, total_chars=%s",
        application_id,
        len(files_meta),
        meta["total_chars"],
    )
    return context, meta


def build_documents_context() -> tuple[dict[str, str], dict[str, Any]]:
    if not DOCUMENTS_DIR.exists():
        raise FileNotFoundError(f"Documents directory not found: {DOCUMENTS_DIR}")

    contexts: dict[str, str] = {}
    applications_meta: dict[str, dict[str, Any]] = {}

    application_dirs = sorted(
        (item for item in DOCUMENTS_DIR.iterdir() if item.is_dir()),
        key=lambda path: path.name,
    )

    if not application_dirs:
        raise FileNotFoundError(
            f"No application folders found in documents directory: {DOCUMENTS_DIR}"
        )

    for application_dir in application_dirs:
        application_id = application_dir.name
        context, meta = build_application_context(application_id, application_dir)
        contexts[application_id] = context
        applications_meta[application_id] = meta

    total_chars = sum(len(context) for context in contexts.values())
    meta = {
        "documents_dir": str(DOCUMENTS_DIR),
        "application_ids": list(contexts.keys()),
        "applications": applications_meta,
        "total_chars": total_chars,
    }
    logger.info(
        "All application contexts loaded: applications=%s, total_chars=%s",
        meta["application_ids"],
        total_chars,
    )
    return contexts, meta


class DocumentContextStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._contexts: dict[str, str] = {}
        self._meta: dict[str, Any] = {}
        self._loaded_at: float | None = None
        self._load_error: str | None = None

    def _build_status(self) -> dict[str, Any]:
        return {
            "loaded": bool(self._contexts),
            "loaded_at": self._loaded_at,
            "error": self._load_error,
            **self._meta,
        }

    def load(self) -> dict[str, Any]:
        try:
            contexts, meta = build_documents_context()
        except Exception as exc:
            with self._lock:
                self._contexts = {}
                self._meta = {}
                self._loaded_at = None
                self._load_error = str(exc)
            logger.exception("Failed to load document contexts")
            raise

        with self._lock:
            self._contexts = contexts
            self._meta = meta
            self._loaded_at = time.time()
            self._load_error = None
            return self._build_status()

    def refresh(self) -> dict[str, Any]:
        return self.load()

    def get_context(self, application_id: str) -> str:
        application_id = str(application_id).strip()
        with self._lock:
            if self._load_error and not self._contexts:
                raise HTTPException(
                    503,
                    f"Document context is not available: {self._load_error}",
                )
            if not self._contexts:
                raise HTTPException(503, "Document context is not loaded yet")
            if application_id not in self._contexts:
                raise HTTPException(
                    404,
                    f"Context for application '{application_id}' was not found",
                )

            context = self._contexts[application_id]
            if not context.strip():
                raise HTTPException(
                    503,
                    f"No documents loaded for application '{application_id}'",
                )
            return context

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._build_status()


def build_ollama_prompt(question: str, context: str) -> str:
    return f"""
Ты помощник по документации в сфере страхования, 
все вопросы которые тебе задают касаются объектов страхования.

Отвечай только на основании
предоставленного контекста.
Если где то информация противоречива, укажи об этом явно, что в тексте присутствует противоречие и укажи какое.
Постарайся ответы давать с markdown разметкой для более визуально красивого результата.

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

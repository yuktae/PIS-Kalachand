"""
Magic-byte validation for uploaded files.

A renamed `.txt` (or any other random bytes) carrying a `.pdf` extension
otherwise reaches Gemini's Files API, which either fails with a confusing
error or — for text-shaped content — processes the garbage as if it were a
real document. Validating the first bytes against a known signature at the
route boundary catches that before anything expensive runs.

Intentionally Flask-free so the helper can be unit-tested without an app
context: callers translate `(False, err)` into whatever response style their
route already speaks (plain 400 text, JSON, NDJSON, etc.) and handle cleanup
of the rejected file.
"""
from __future__ import annotations

import os
from typing import Final


# Each signature is a tuple of (offset, bytes) parts. All parts must match
# for the file to be classified as that kind. A list of single-part entries
# is enough for most formats; WEBP needs two parts because the magic is
# split across the 4-byte size header (RIFF<size>WEBP).
_SignatureParts = tuple[tuple[int, bytes], ...]

_SIGNATURES: Final[dict[str, _SignatureParts]] = {
    "PDF":  ((0, b"%PDF-"),),
    "JPEG": ((0, b"\xff\xd8\xff"),),
    "PNG":  ((0, b"\x89PNG\r\n\x1a\n"),),
    # RIFF<4-byte little-endian size>WEBP
    "WEBP": ((0, b"RIFF"), (8, b"WEBP")),
    # DOCX (and the rest of the Office Open XML family) is a ZIP container.
    # The first four bytes are the standard ZIP local-file-header. We accept
    # the whole family because the deployed extract_raw_text_from_files
    # pipeline reads any Office doc, and we don't want to false-reject a
    # legitimate `.docx` over container-vs-extension nuance.
    "DOCX": ((0, b"PK\x03\x04"),),
}

# How many bytes we need to inspect to cover the deepest signature.
_PEEK_BYTES: Final[int] = max(
    off + len(sig)
    for parts in _SIGNATURES.values()
    for off, sig in parts
)

_SUPPORTED_LABEL: Final[str] = "PDF, JPEG, PNG, WEBP, or DOCX"


def validate_upload(filepath: str) -> tuple[bool, str | None]:
    """Check whether the file at `filepath` starts with a supported
    document/image signature.

    Returns `(True, None)` on success and `(False, message)` otherwise.
    `message` is human-readable and safe to surface in an HTTP error body:
    it embeds the file's basename only, never the full upload path.

    The function is read-only. It does NOT delete the file or call any
    Flask helpers — callers own that cleanup so they can match their own
    route's response style.
    """
    name = os.path.basename(filepath)
    try:
        with open(filepath, "rb") as fh:
            head = fh.read(_PEEK_BYTES)
    except OSError as e:
        return False, f"Could not read uploaded file '{name}': {e}"

    if len(head) < 4:
        return False, f"Uploaded file '{name}' is empty or truncated."

    for parts in _SIGNATURES.values():
        if all(head[off:off + len(sig)] == sig for off, sig in parts):
            return True, None

    return False, (
        f"Uploaded file '{name}' is not a supported {_SUPPORTED_LABEL} "
        "document. The file may be corrupted or renamed from another format."
    )
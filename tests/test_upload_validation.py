"""
Upload-validation tests.

Two layers:

  • Pure helper — `utils.upload_validation.validate_upload` reads the first
    bytes of a saved file and answers "is this really a supported document
    or image?" These tests don't need an app context: write a temp file,
    call the function, assert the (ok, err) shape.

  • One representative route — `/create` is the simplest of the six upload
    sites (plain-text 400 on rejection, no NDJSON early-return shape). We
    POST a renamed `.txt` as `.pdf` and confirm:
        (1) the response is HTTP 400 with the helper's message,
        (2) the file we just uploaded is gone from UPLOAD_FOLDER.

The success path is exercised in the helper tests (real PDF/PNG/JPEG bytes
return (True, None)). Driving a successful end-to-end POST through `/create`
would require mocking Gemini + scraping + image search, which doesn't
belong in a validation regression test.
"""
from __future__ import annotations

import io
import os

from utils.upload_validation import validate_upload


# ── Minimum-viable byte sequences for each accepted format. They satisfy
# the magic-byte check; the rest of each file doesn't need to be valid for
# the validator (which only inspects the first ~12 bytes).
_PDF_BYTES  = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
_PNG_BYTES  = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
_WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 "
_DOCX_BYTES = b"PK\x03\x04" + b"\x14\x00\x06\x00"
_TXT_BYTES  = b"This is just plain text masquerading as a PDF.\n" * 4


def _write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ── helper-level ────────────────────────────────────────────────────────────


def test_validate_accepts_real_pdf_bytes(tmp_path):
    ok, err = validate_upload(_write(tmp_path, "doc.pdf", _PDF_BYTES))
    assert ok is True
    assert err is None


def test_validate_accepts_real_png_bytes(tmp_path):
    ok, err = validate_upload(_write(tmp_path, "img.png", _PNG_BYTES))
    assert ok is True
    assert err is None


def test_validate_accepts_real_jpeg_bytes(tmp_path):
    ok, err = validate_upload(_write(tmp_path, "img.jpg", _JPEG_BYTES))
    assert ok is True
    assert err is None


def test_validate_accepts_webp_bytes(tmp_path):
    ok, err = validate_upload(_write(tmp_path, "img.webp", _WEBP_BYTES))
    assert ok is True
    assert err is None


def test_validate_accepts_docx_bytes(tmp_path):
    ok, err = validate_upload(_write(tmp_path, "doc.docx", _DOCX_BYTES))
    assert ok is True
    assert err is None


def test_validate_rejects_text_renamed_as_pdf(tmp_path):
    path = _write(tmp_path, "fake.pdf", _TXT_BYTES)
    ok, err = validate_upload(path)
    assert ok is False
    assert err is not None
    # Surfaceable to the user — basename, no full path leak.
    assert "fake.pdf" in err
    assert tmp_path.name not in err  # full path components must not appear


def test_validate_rejects_empty_file(tmp_path):
    ok, err = validate_upload(_write(tmp_path, "empty.pdf", b""))
    assert ok is False
    assert err is not None
    assert "empty" in err.lower() or "truncated" in err.lower()


def test_validate_handles_missing_file(tmp_path):
    ok, err = validate_upload(str(tmp_path / "does-not-exist.pdf"))
    assert ok is False
    assert err is not None


# ── one representative route: /create ──────────────────────────────────────


def test_create_route_rejects_renamed_text_and_cleans_up(marketing_client, app):
    """POST a `.pdf`-named file whose body is plain text. The validator
    must reject it BEFORE the streaming generator starts, return 400 with
    the helper's message, and remove the file from UPLOAD_FOLDER so we
    don't leak rejected uploads on disk."""
    upload_folder = app.config["UPLOAD_FOLDER"]
    if not os.path.isabs(upload_folder):
        upload_folder = os.path.join(app.root_path, upload_folder)
    os.makedirs(upload_folder, exist_ok=True)

    bad_name = "renamed_text_should_fail.pdf"
    target_path = os.path.join(upload_folder, bad_name)
    # Pre-clean in case a previous failing run left a file behind.
    if os.path.exists(target_path):
        os.remove(target_path)

    resp = marketing_client.post(
        "/create",
        data={
            "model_name":   "Test Model",
            "supplier_url": "",
            "ai_document":  (io.BytesIO(_TXT_BYTES), bad_name),
        },
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400, (
        f"Expected 400 from validation; got {resp.status_code}. "
        f"Body: {resp.data[:300]!r}"
    )
    # Plain-text response (matches the route's existing early-bail style).
    body = resp.data.decode("utf-8", errors="replace")
    assert bad_name in body, f"Error body should name the rejected file. Got: {body!r}"

    # The rejected file must have been deleted by the route's cleanup.
    assert not os.path.exists(target_path), (
        f"Rejected upload was not cleaned up at {target_path}"
    )


def test_create_route_with_no_file_does_not_400_for_validation(marketing_client):
    """Sanity check: posting with no file should NOT trigger the validation
    error. The save loop simply skips when `f.filename` is empty. (The
    route may still error downstream — that's not what we're testing here;
    we just want to prove the new validation doesn't false-positive on
    the empty-file case.)"""
    resp = marketing_client.post(
        "/create",
        data={"model_name": "Test", "supplier_url": ""},
        content_type="multipart/form-data",
    )
    # We don't pin the status code — the streaming route may return 200
    # with an NDJSON error payload, or it may run further. What matters is
    # that we did NOT get a 400 with our validator's "not a supported
    # ..." message.
    if resp.status_code == 400:
        body = resp.data.decode("utf-8", errors="replace")
        assert "not a supported" not in body, (
            "Validation false-positive on empty-file submission: " + body
        )
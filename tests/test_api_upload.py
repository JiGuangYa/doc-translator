"""Upload endpoint regression: after async->sync refactor, the real docx
upload->parse->summary path still works."""
import io
import uuid

from app import config


def _tiny_docx() -> bytes:
    import docx
    buf = io.BytesIO()
    d = docx.Document()
    d.add_paragraph("hello world")
    d.save(buf)
    return buf.getvalue()


def test_upload_sync_endpoint_roundtrip(client):
    resp = client.post("/api/upload", files={"file": ("demo.docx", _tiny_docx(),
                                                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ext"] == ".docx"
    assert uuid.UUID(hex=body["task_id"])
    # The tasks directory should already contain parse artifacts
    assert (config.TASKS_DIR / body["task_id"] / f"original{body['ext']}").exists()


def test_upload_rejects_bad_ext(client):
    resp = client.post("/api/upload", files={"file": ("x.exe", b"MZ...", "application/octet-stream")})
    assert resp.status_code == 400

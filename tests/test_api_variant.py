"""Variant path-traversal protection regression: download / file / preview
endpoints must apply whitelist validation."""
import json
import uuid

import pytest

TASK_ID = uuid.uuid4().hex  # 32 hex chars, consistent with utils.valid_task_id


@pytest.fixture()
def sample_task(tmp_path, client):
    """Place a minimal task (job.json + original.docx) under TASKS_DIR of the
    signed-in client. The tmp_path of the test is the same directory the
    conftest client points to."""
    from app import config
    task_dir = config.TASKS_DIR / TASK_ID
    task_dir.mkdir(parents=True)
    job = {
        "task_id": TASK_ID,
        "filename": "demo.docx",
        "ext": ".docx",
        "status": "done",
    }
    (task_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    (task_dir / "original.docx").write_bytes(b"PK\x03\x04 fake")
    (task_dir / "translated.docx").write_bytes(b"PK\x03\x04 fake")
    return client


BAD_VARIANTS = [
    "../../etc/passwd",
    "..\\..\\Users\\x\\secret",
    "original/../../other",
    "%2e%2e%2fescape",
]


def test_download_accepts_valid_variants(client, sample_task):
    assert client.get(f"/api/tasks/{TASK_ID}/download?variant=translated").status_code == 200
    assert client.get(f"/api/tasks/{TASK_ID}/download?variant=original").status_code == 200


@pytest.mark.parametrize("variant", BAD_VARIANTS)
def test_download_rejects_traversal(client, sample_task, variant):
    resp = client.get(f"/api/tasks/{TASK_ID}/download", params={"variant": variant})
    assert resp.status_code == 400


def test_raw_file_rejects_traversal(client, sample_task):
    assert client.get(f"/api/tasks/{TASK_ID}/file?variant=original").status_code == 200
    for variant in BAD_VARIANTS:
        assert client.get(f"/api/tasks/{TASK_ID}/file", params={"variant": variant}).status_code == 400


def test_preview_file_rejects_traversal(client, sample_task):
    """Preview series endpoints share _file(); whitelist must take effect
    before opening the file."""
    for variant in BAD_VARIANTS:
        resp = client.get(f"/api/tasks/{TASK_ID}/preview/pdf/1", params={"variant": variant})
        assert resp.status_code == 400


def test_pdf_pages_endpoint_not_shadowed(client, tmp_path):
    """Regression: /preview/pdf/pages must be declared before /{page},
    otherwise it gets swallowed by the int path param and always returns 422."""
    import fitz
    pdf_id = uuid.uuid4().hex
    d = tmp_path / pdf_id
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({
        "task_id": pdf_id, "filename": "b.pdf", "ext": ".pdf", "status": "done",
    }), encoding="utf-8")
    doc = fitz.open()
    doc.new_page()
    doc.save(d / "original.pdf")
    doc.close()
    resp = client.get(f"/api/tasks/{pdf_id}/preview/pdf/pages")
    assert resp.status_code == 200
    assert resp.json() == {"pages": 1}

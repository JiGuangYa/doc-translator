"""Scanned PDFs remain available for local OCR and bilingual export."""

import io
import time

import pymupdf as fitz
from PIL import Image, ImageDraw

from app import config, store


def _scan() -> bytes:
    image = Image.new("RGB", (400, 150), "white")
    ImageDraw.Draw(image).text((15, 40), "Hello scanned document", fill="black")
    png = io.BytesIO()
    image.save(png, format="PNG")
    document = fitz.open()
    page = document.new_page(width=400, height=150)
    page.insert_image(page.rect, stream=png.getvalue())
    data = document.tobytes()
    document.close()
    return data


def test_scanned_pdf_ocr_review_and_export(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    response = client.post("/api/upload", files={
        "file": ("scan.pdf", _scan(), "application/pdf")})
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "ocr_pending"
    assert task["ocr_pages"] == 1
    task_id = task["task_id"]

    received = client.post(f"/api/tasks/{task_id}/ocr", json={"lines": [{
        "page": 1, "bbox": [0.04, 0.2, 0.6, 0.5],
        "text": "Hello scanned document", "confidence": 0.65,
    }]})
    assert received.status_code == 200
    assert received.json()["status"] == "pending_confirm"
    assert any("low-confidence" in warning for warning in received.json()["warnings"])
    blocked = client.post(f"/api/tasks/{task_id}/start", json={
        "provider_id": "mock", "source_lang": "en", "target_lang": "zh-CN"})
    assert blocked.status_code == 400
    edit = client.patch(f"/api/tasks/{task_id}/ocr/s000000", json={
        "text": "Hello reviewed document"})
    assert edit.status_code == 200

    started = client.post(f"/api/tasks/{task_id}/start", json={
        "provider_id": "mock", "source_lang": "en", "target_lang": "zh-CN"})
    assert started.status_code == 200
    for _ in range(50):
        job = store.load_job(task_id)
        if job["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "done", job.get("error")
    output = store.task_dir(task_id) / "translated.pdf"
    with fitz.open(output) as translated:
        assert translated.page_count == 1
        assert translated[0].get_images()
        assert translated[0].get_text().strip()

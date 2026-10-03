import hashlib
import time

import pymupdf as fitz
import pytest

from app import store
from app.formats import pdf_fmt, pdf_layout
from app.formats.common import FormatAdapterError, Segment
from app.services import pipeline, task_manager
from app.services import ocr
from tests.pdf_fixtures import mixed_pdf


def _native_pdf(path):
    with fitz.open() as document:
        page = document.new_page(width=500, height=300)
        page.draw_rect(fitz.Rect(30, 30, 430, 105), color=None, fill=(0.85, 0.95, 0.85))
        page.insert_text((50, 70), "Translate this linked heading", fontsize=16)
        page.insert_text((50, 160), "Unchanged footer", fontsize=12)
        rectangle = page.search_for("Translate this linked heading")[0]
        page.insert_link({"kind": fitz.LINK_URI, "from": rectangle, "uri": "https://example.com/link"})
        document.save(path)
    return path


def test_layout_writer_preserves_links_background_and_other_text(tmp_path):
    source = _native_pdf(tmp_path / "source.pdf")
    target = tmp_path / "translated.pdf"
    extracted = pdf_fmt.extract(source, {})
    first = next(segment for segment in extracted.segments if segment.text.startswith("Translate"))
    report = pdf_fmt.write_back(source, target, {first.seg_id: "这是保留链接的标题"}, {"target_lang": "zh-CN"})
    assert report.written == 1 and not report.overflow
    with fitz.open(source) as original, fitz.open(target) as translated:
        assert "这是保留链接的标题" in translated[0].get_text()
        assert "Translate this linked heading" not in translated[0].get_text()
        assert "Unchanged footer" in translated[0].get_text()
        old_link, new_link = original[0].get_links()[0], translated[0].get_links()[0]
        assert old_link["uri"] == new_link["uri"]
        assert original.xref_object(old_link["xref"]) == translated.xref_object(new_link["xref"])
        clip = fitz.Rect(35, 35, 45, 45)
        assert original[0].get_pixmap(clip=clip).samples == translated[0].get_pixmap(clip=clip).samples


@pytest.mark.parametrize("text", ["你好，世界", "Привет мир", "ภาษาไทย", "مرحبا بالعالم", '<img src="https://example.invalid/pixel">'])
def test_unicode_and_html_literals_remain_searchable(tmp_path, text):
    source = _native_pdf(tmp_path / "source.pdf")
    target = tmp_path / "unicode.pdf"
    first = pdf_fmt.extract(source, {}).segments[0]
    report = pdf_fmt.write_back(source, target, {first.seg_id: text}, {})
    assert report.written == 1
    with fitz.open(target) as document:
        assert text in document[0].get_text()
        assert not document[0].get_images(), "Literal HTML must not fetch or insert an image"


def test_commit_failure_never_replaces_valid_pdf_with_blank_output(tmp_path, monkeypatch):
    source = _native_pdf(tmp_path / "source.pdf")
    task_id = "f" * 32
    job = pipeline.parse_upload(task_id, source.name, source, {})
    segment_id = job["segments"][0]["seg_id"]
    pipeline._apply_translations(task_id, ".pdf", {segment_id: "First valid translation"})
    output = store.task_dir(task_id) / "translated.pdf"
    previous = output.read_bytes()
    revision = store.load_job(task_id)["revision"]
    def fail(*args, **kwargs):
        raise RuntimeError("simulated insertion failure")
    monkeypatch.setattr(fitz.Page, "show_pdf_page", fail)
    with pytest.raises(FormatAdapterError, match="export stopped"):
        pipeline.revise_segment(task_id, segment_id, "Replacement that cannot be stamped", revision)
    assert output.read_bytes() == previous
    assert store.load_job(task_id)["revision"] == revision


def test_overflow_keeps_original_text(tmp_path):
    source = _native_pdf(tmp_path / "source.pdf")
    first = pdf_fmt.extract(source, {}).segments[0]
    target = tmp_path / "overflow.pdf"
    report = pdf_fmt.write_back(source, target, {first.seg_id: "Long translation " * 1000}, {})
    assert first.seg_id in report.overflow
    with fitz.open(target) as document:
        assert first.text in document[0].get_text()


def test_existing_redaction_is_rejected_before_translation(client, tmp_path):
    source = _native_pdf(tmp_path / "source.pdf")
    marked = tmp_path / "marked.pdf"
    with fitz.open(source) as document:
        document[0].add_redact_annot(fitz.Rect(40, 145, 200, 175))
        document.save(marked)
    task = client.post("/api/upload", files={"file": (marked.name, marked.read_bytes(), "application/pdf")}).json()
    response = client.post(f"/api/tasks/{task['task_id']}/start", json={"provider_id": "mock"})
    assert response.status_code == 400 and "redaction" in response.text
    assert task_manager.count_in_flight() == 0
    assert not (store.task_dir(task["task_id"]) / "translated.pdf").exists()


def test_form_fields_are_not_silently_flattened(client, tmp_path):
    source = _native_pdf(tmp_path / "source.pdf")
    form = tmp_path / "form.pdf"
    with fitz.open(source) as document:
        widget = fitz.Widget()
        widget.field_name = "Editable field"
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.rect = fitz.Rect(50, 200, 250, 230)
        document[0].add_widget(widget)
        document.save(form)
    task = client.post("/api/upload", files={"file": (form.name, form.read_bytes(), "application/pdf")}).json()
    response = client.post(f"/api/tasks/{task['task_id']}/start", json={"provider_id": "mock"})
    assert response.status_code == 400 and "form fields" in response.text
    assert task_manager.count_in_flight() == 0


def test_bilingual_export_rejects_annotations_it_cannot_preserve(client, tmp_path):
    source = mixed_pdf(tmp_path / "mixed.pdf")
    annotated = tmp_path / "annotated.pdf"
    with fitz.open(source) as document:
        document[1].add_text_annot(fitz.Point(30, 30), "A note that must not disappear")
        document.save(annotated)
    task = client.post("/api/upload", files={"file": (annotated.name, annotated.read_bytes(), "application/pdf")}).json()
    for page in task["ocr_required_pages"]:
        client.post(f"/api/tasks/{task['task_id']}/ocr/page/{page}", json={"lines": [{
            "page": page, "bbox": [0.1, 0.6, 0.8, 0.9], "text": "Recognized text", "confidence": 0.9}]})
    response = client.post(f"/api/tasks/{task['task_id']}/start", json={"provider_id": "mock"})
    assert response.status_code == 400 and "annotations" in response.text
    assert task_manager.count_in_flight() == 0


def _image_hashes(document, page):
    return {hashlib.sha256(fitz.Pixmap(document, image[0]).samples).hexdigest() for image in page.get_images()}


def test_mixed_pdf_page_checkpoints_review_and_bilingual_output(client, tmp_path):
    source = mixed_pdf(tmp_path / "mixed.pdf")
    original_bytes = source.read_bytes()
    response = client.post("/api/upload", files={"file": (source.name, original_bytes, "application/pdf")})
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "ocr_pending" and task["ocr_required_pages"] == [2, 4]
    task_id = task["task_id"]
    initial = store.load_job(task_id)
    footer = next(segment for segment in initial["segments"] if segment["text"] == "Page 2")
    native = {segment["seg_id"]: segment["text"] for segment in initial["segments"]}
    lines = [{"page": 2, "text": "SCANNED PAGE TWO", "bbox": [0.05, 0.65, 0.8, 0.93], "confidence": 0.9},
             {"page": 2, "text": "Page 2", "bbox": footer["meta"]["normalized_bbox"], "confidence": 0.9}]
    first = client.post(f"/api/tasks/{task_id}/ocr/page/2", json={"lines": lines})
    assert first.status_code == 200 and first.json()["status"] == "ocr_pending"
    assert first.json()["ocr_completed_pages"] == [2]
    job = store.load_job(task_id)
    assert sum(segment["text"] == "Page 2" for segment in job["segments"]) == 1
    assert all(next(segment for segment in job["segments"] if segment["seg_id"] == key)["text"] == text
               for key, text in native.items())
    revision = first.json()["revision"]
    assert client.post(f"/api/tasks/{task_id}/ocr/page/2", json={"lines": lines}).json()["revision"] == revision
    task_manager.recover_on_startup()
    assert store.load_job(task_id)["ocr_completed_pages"] == [2]
    empty = client.post(f"/api/tasks/{task_id}/ocr/page/4", json={"lines": []})
    assert empty.json()["status"] == "pending_confirm" and empty.json()["ocr_empty_pages"] == [4]
    assert client.post(f"/api/tasks/{task_id}/start", json={"provider_id": "mock"}).status_code == 400
    manual = client.post(f"/api/tasks/{task_id}/ocr/page/4/text", json={"text": "Manually reviewed page four"})
    assert manual.status_code == 200 and manual.json()["ocr_review_count"] == 0
    again = client.post(f"/api/tasks/{task_id}/ocr/page/4/text", json={"text": "Manually reviewed page four"})
    assert again.json()["revision"] == manual.json()["revision"]
    assert client.post(f"/api/tasks/{task_id}/start", json={"provider_id": "mock"}).status_code == 200
    for _ in range(100):
        job = store.load_job(task_id)
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job.get("error")
    output = store.task_dir(task_id) / "translated.pdf"
    with fitz.open(source) as before, fitz.open(output) as after:
        assert after.page_count == before.page_count == 4
        assert "Native page one" in after[0].get_text()
        assert "SCANNED PAGE TWO" in after[1].get_text()
        assert "Native page three" in after[2].get_text()
        assert "Manually reviewed page four" in after[3].get_text()
        for number in (1, 3):
            assert _image_hashes(before, before[number]) <= _image_hashes(after, after[number])
        for number in range(before.page_count):
            original_pixels = before[number].get_pixmap(alpha=False)
            copied_pixels = after[number].get_pixmap(clip=before[number].rect, alpha=False)
            assert (original_pixels.width, original_pixels.height) == (copied_pixels.width, copied_pixels.height)
            assert original_pixels.samples == copied_pixels.samples, f"Original page {number + 1} changed visually"
        for number in (0, 3):
            original_link = before[number].get_links()[0]
            copied_link = after[number].get_links()[0]
            assert original_link["uri"] == copied_link["uri"]
            assert tuple(original_link["from"]) == pytest.approx(tuple(copied_link["from"]), abs=0.01)
    assert source.read_bytes() == original_bytes


def test_empty_page_requires_review_then_can_copy_without_a_model(client, tmp_path):
    source = tmp_path / "blank.pdf"
    with fitz.open() as document:
        document.new_page()
        document.save(source)
    task = client.post("/api/upload", files={"file": (source.name, source.read_bytes(), "application/pdf")}).json()
    task_id = task["task_id"]
    assert client.post(f"/api/tasks/{task_id}/ocr/page/1", json={"lines": []}).json()["ocr_review_count"] == 1
    assert client.post(f"/api/tasks/{task_id}/start", json={"provider_id": ""}).status_code == 400
    assert client.post(f"/api/tasks/{task_id}/ocr/page/1/confirm-empty").json()["ocr_review_count"] == 0
    assert client.post(f"/api/tasks/{task_id}/start", json={"provider_id": ""}).status_code == 200
    for _ in range(50):
        job = store.load_job(task_id)
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job.get("error")
    assert (store.task_dir(task_id) / "translated.pdf").read_bytes() == source.read_bytes()


def test_low_confidence_correction_updates_counts_and_checks_revision(client, tmp_path):
    source = mixed_pdf(tmp_path / "mixed.pdf")
    task_id = client.post("/api/upload", files={"file": (source.name, source.read_bytes(), "application/pdf")}).json()["task_id"]
    client.post(f"/api/tasks/{task_id}/ocr/page/2", json={"lines": [
        {"page": 2, "bbox": [0.1, 0.6, 0.5, 0.8], "text": "123", "confidence": 0.6}]})
    job = store.load_job(task_id)
    segment = next(segment for segment in job["segments"] if segment["text"] == "123")
    assert not segment["translatable"]
    revision = job["revision"]
    route = f"/api/tasks/{task_id}/ocr/{segment['seg_id']}"
    assert client.patch(route, json={"text": "Reviewed words", "expected_revision": revision - 1}).status_code == 409
    assert client.patch(route, json={"text": "Reviewed words", "expected_revision": revision}).status_code == 200
    updated = store.load_job(task_id)
    assert next(value for value in updated["segments"] if value["seg_id"] == segment["seg_id"])["translatable"]
    assert updated["segment_count"] == job["segment_count"] + 1


def test_invalid_ocr_box_is_rejected_without_marking_page_complete(client, tmp_path):
    source = mixed_pdf(tmp_path / "mixed.pdf")
    task_id = client.post("/api/upload", files={"file": (source.name, source.read_bytes(), "application/pdf")}).json()["task_id"]
    response = client.post(f"/api/tasks/{task_id}/ocr/page/2", json={"lines": [
        {"page": 2, "bbox": [0.9, 0.1, 0.2, 0.8], "text": "Invalid region", "confidence": 1}]})
    assert response.status_code == 400
    assert store.load_job(task_id)["ocr_completed_pages"] == []


def test_ocr_does_not_drop_image_text_between_native_lines():
    native = {"text": "First native line Second native line", "meta": {
        "normalized_bbox": [0.1, 0.2, 0.9, 0.9],
        "normalized_span_bboxes": [[0.1, 0.8, 0.9, 0.9], [0.1, 0.2, 0.9, 0.3]]}}
    line = {"text": "Text inside an image in the gap", "bbox": [0.1, 0.45, 0.9, 0.55]}
    assert not ocr._already_in_text_layer(line, [native])


def test_cjk_paragraph_embeds_only_used_glyphs():
    fragment = pdf_layout.render_text("这是包含英文 ABC 的中文译文。", 300, 40, 12)
    data = fragment.tobytes(deflate=True)
    fragment.close()
    assert len(data) < 100000, "Each paragraph must not embed a full CJK font"
    with fitz.open(stream=data, filetype="pdf") as reopened:
        assert "中文译文" in reopened[0].get_text()
        assert reopened[0].get_pixmap().samples


def test_legacy_bullet_stays_outside_translation_placement():
    segment = Segment(seg_id="s000001", text="Criteria:•", meta={"bbox": [10, 10, 100, 60],
        "lines": [{"text": "Criteria:", "bbox": [10, 10, 100, 30], "span_bboxes": [[10, 10, 100, 30]]},
                  {"text": "•", "bbox": [10, 45, 20, 60], "span_bboxes": [[10, 45, 20, 60]]}]})
    text, rectangle, strips = pdf_fmt._placement(segment, "标准：•")
    assert text == "标准：" and rectangle.y1 == 30
    assert all(not strip.intersects(fitz.Rect(10, 45, 20, 60)) for strip in strips)


def test_short_centered_heading_keeps_alignment(tmp_path):
    from reportlab.pdfgen import canvas
    source = tmp_path / "centered.pdf"
    drawing = canvas.Canvas(str(source), pagesize=(500, 300))
    drawing.setFont("Helvetica", 24)
    drawing.drawCentredString(250, 240, "Course title")
    drawing.save()
    first = pdf_fmt.extract(source, {}).segments[0]
    target = tmp_path / "centered-translation.pdf"
    pdf_fmt.write_back(source, target, {first.seg_id: "课程标题"}, {})
    with fitz.open(target) as document:
        rectangle = document[0].search_for("课程标题")[0]
        assert abs((rectangle.x0 + rectangle.x1) / 2 - 250) < 3


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_bilingual_original_pixels_preserved_for_all_page_rotations(tmp_path, rotation):
    source = _native_pdf(tmp_path / "source.pdf")
    rotated = tmp_path / "rotated.pdf"
    with fitz.open(source) as document:
        document[0].set_cropbox(fitz.Rect(10, 10, 490, 290))
        document[0].set_rotation(rotation)
        document.save(rotated)
    output = tmp_path / "bilingual.pdf"
    pdf_fmt.write_scanned_bilingual(rotated, output, {"s000000": "Translated example"}, [{
        "seg_id": "s000000", "text": "Example", "translatable": True, "meta": {"page": 1}}], {})
    with fitz.open(rotated) as before, fitz.open(output) as after:
        assert before[0].get_pixmap().samples == after[0].get_pixmap(clip=before[0].rect).samples

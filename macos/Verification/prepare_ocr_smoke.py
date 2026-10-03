"""Create review and long-scan fixtures in an explicitly isolated native data folder."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    data = Path(args.data_dir).resolve()
    if data.name == "DocTranslatorNative":
        raise SystemExit("Use an isolated test data directory")
    root = Path(__file__).resolve().parents[2]
    os.environ["DOC_TRANSLATOR_DATA_DIR"] = str(data)
    os.environ["DOC_TRANSLATOR_DESKTOP"] = "1"
    sys.path.insert(0, str(root))
    from app import store
    from app.services import ocr, pipeline
    from tests.pdf_fixtures import long_scan_pdf, mixed_pdf
    directory = root / "build/pdf-qa/ui"
    directory.mkdir(parents=True, exist_ok=True)
    mixed = mixed_pdf(directory / "mixed-ocr.pdf")
    low_id = uuid.uuid4().hex
    job = pipeline.parse_upload(low_id, "OCR confidence review.pdf", mixed, {})
    job.update(provider_id="mock", source_lang="en", target_lang="zh-CN")
    store.save_job(low_id, job)
    ocr.accept_page(low_id, 2, [
        {"page": 2, "text": "SCANNED PAGE TWO", "bbox": [0.05, 0.82, 0.8, 0.91], "confidence": 0.65},
        {"page": 2, "text": "Review this source before translation.", "bbox": [0.05, 0.70, 0.9, 0.80], "confidence": 0.95}])
    ocr.accept_page(low_id, 4, [
        {"page": 4, "text": "SCANNED PAGE FOUR", "bbox": [0.05, 0.90, 0.95, 0.97], "confidence": 0.95},
        {"page": 4, "text": "Review this source before translation.", "bbox": [0.05, 0.80, 0.95, 0.87], "confidence": 0.95}])
    empty = directory / "empty-ocr-review.pdf"
    picture = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(picture).rectangle((100, 100, 700, 500), fill=(200, 230, 240))
    drawing = canvas.Canvas(str(empty), pagesize=(600, 450))
    drawing.drawImage(ImageReader(picture), 0, 0, width=600, height=450)
    drawing.save()
    empty_id = uuid.uuid4().hex
    job = pipeline.register_scanned_pdf(empty_id, "OCR empty page review.pdf", empty)
    job.update(provider_id="mock", source_lang="en", target_lang="zh-CN")
    store.save_job(empty_id, job)
    ocr.accept_page(empty_id, 1, [])
    long_path = long_scan_pdf(directory / "long-ocr-memory-026.pdf")
    report = {"low_confidence_task": low_id, "empty_page_task": empty_id, "long_scan": str(long_path)}
    (directory / "fixtures.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()

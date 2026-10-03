"""Verify real Apple Vision -> packaged API -> mock translation -> PDF export."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pymupdf as fitz
from packaged_api import PackagedAPI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app")
    parser.add_argument("ocr_tool")
    parser.add_argument("--output")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from tests.pdf_fixtures import mixed_pdf
    with tempfile.TemporaryDirectory(prefix="doc-translator-packaged-pdf-") as temporary:
        directory = Path(temporary)
        source = mixed_pdf(directory / "mixed.pdf")
        with PackagedAPI(args.app, directory) as api:
            task = api.upload(source)
            assert task["status"] == "ocr_pending" and task["ocr_required_pages"] == [2, 4]
            task_id = task["task_id"]
            result = subprocess.run([args.ocr_tool, "--ocr-json", str(source), "2", "4"],
                                     capture_output=True, text=True, timeout=90, check=True)
            lines = json.loads(result.stdout)
            assert any("SCANNED PAGE TWO" in line["text"].upper() for line in lines if line["page"] == 2)
            assert any("SCANNED PAGE FOUR" in line["text"].upper() for line in lines if line["page"] == 4)
            for page in (2, 4):
                task = api.json(f"/api/tasks/{task_id}/ocr/page/{page}",
                                {"lines": [line for line in lines if line["page"] == page]})
            assert task["ocr_completed_pages"] == [2, 4]
            assert task["status"] == "pending_confirm"
            segments = api.json(f"/api/tasks/{task_id}/segments")["segments"]
            assert sum(segment["text"] == "Page 2" for segment in segments) == 1
            assert not task["ocr_empty_pages"]
            for page, title in ((2, "SCANNED PAGE TWO"), (4, "SCANNED PAGE FOUR")):
                recognized = " ".join(segment["text"] for segment in segments
                                      if segment["meta"].get("ocr") and segment["meta"]["page"] == page)
                expected = title + " Review this source before translation."
                def normalize(value):
                    return re.sub(r"\W+", "", value.casefold())
                assert normalize(recognized) == normalize(expected), (page, recognized)
            # The fixture provides ground truth; test the same explicit review
            # action used by the native editor if Vision flags a line for review.
            for segment in segments:
                meta = segment.get("meta", {})
                if (meta.get("confidence", 1) < 0.75 or meta.get("needs_review")) and not meta.get("reviewed"):
                    api.json(f"/api/tasks/{task_id}/ocr/{segment['seg_id']}", {"text": segment["text"]}, "PATCH")
            api.json(f"/api/tasks/{task_id}/start", {"provider_id": "mock", "source_lang": "en", "target_lang": "zh-CN"})
            deadline = time.monotonic() + 20
            while True:
                task = api.json(f"/api/tasks/{task_id}")
                if task["status"] in ("done", "failed") or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            assert task["status"] == "done", task.get("error")
            heading = next(segment for segment in segments if segment["meta"].get("ocr") and
                           "SCANNED PAGE TWO" in segment["text"].upper())
            api.json(f"/api/tasks/{task_id}/segments/{heading['seg_id']}",
                     {"text": "扫描第二页", "expected_revision": task["revision"]}, "PATCH")
            output = api.bytes(f"/api/tasks/{task_id}/download")
        with fitz.open(source) as original, fitz.open(stream=output, filetype="pdf") as translated:
            assert translated.page_count == original.page_count == 4
            assert "扫描第二页" in translated[1].get_text()
            for page in range(4):
                assert original[page].get_pixmap().samples == translated[page].get_pixmap(clip=original[page].rect).samples
            assert translated[0].get_links()[0]["uri"] == original[0].get_links()[0]["uri"]
        if args.output:
            destination = Path(args.output)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "mixed-original.pdf").write_bytes(source.read_bytes())
            (destination / "mixed-bilingual.pdf").write_bytes(output)
            (destination / "ocr-lines.json").write_text(json.dumps(lines, ensure_ascii=False, indent=2))
        print("PASS: packaged mixed-PDF import, real local Vision, page checkpoints, review, revision and bilingual export")
        print("PASS: original page pixels and links preserved; corrected Chinese text is searchable")


if __name__ == "__main__":
    main()

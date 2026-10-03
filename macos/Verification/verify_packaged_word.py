"""Check versioned Word import, mock translation, link revision and page preview in the frozen engine."""

import argparse
import io
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from lxml import etree
from packaged_api import PackagedAPI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from tests.docx_fixtures import complex_docx

    args.output.mkdir(parents=True, exist_ok=True)
    source = complex_docx(args.output / "Word fidelity source.docx", compatibility_copy=True)
    with tempfile.TemporaryDirectory(prefix="doc-translator-packaged-word-") as data, PackagedAPI(args.app, data) as api:
        task = api.upload(source)
        task_id = task["task_id"]
        stored = json.loads((Path(data) / "tasks" / task_id / "job.json").read_text())
        assert stored["format_options"] == {"docx_version": 2}
        segments = api.json(f"/api/tasks/{task_id}/segments")["segments"]
        linked = next(s for s in segments if s["text"] == "read the manual")
        assert linked["translatable"]
        api.json(f"/api/tasks/{task_id}/start", {"provider_id": "mock", "source_lang": "en", "target_lang": "zh-CN"})
        deadline = time.monotonic() + 15
        while True:
            task = api.json(f"/api/tasks/{task_id}")
            if task["status"] not in ("queued", "translating"):
                break
            assert time.monotonic() < deadline, "Word translation did not finish"
            time.sleep(0.1)
        assert task["status"] == "done", task.get("error")
        api.json(f"/api/tasks/{task_id}/segments/{linked['seg_id']}",
                 {"text": "阅读手册", "expected_revision": task["revision"]}, method="PATCH")
        output = api.bytes(f"/api/tasks/{task_id}/download")
        (args.output / "Word fidelity output.docx").write_bytes(output)
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(io.BytesIO(output)) as translated:
            assert original.namelist() == translated.namelist()
            for name in original.namelist():
                if name not in ("word/document.xml", "word/header1.xml"):
                    assert original.read(name) == translated.read(name), name
            xml = etree.fromstring(translated.read("word/document.xml"))
            assert "".join(xml.find(".//w:hyperlink", namespace).itertext()) == "阅读手册"
            boxes = ["".join(node.itertext()) for node in xml.findall(".//w:txbxContent", namespace)]
            assert len(boxes) == 2 and boxes[0] == boxes[1] and boxes[0].startswith("【T】")
        deadline = time.monotonic() + 30
        while True:
            status = api.json(f"/api/tasks/{task_id}/preview/render")
            if status["status"] == "ready":
                break
            if status["status"] in ("none", "failed"):
                api.json(f"/api/tasks/{task_id}/preview/render", {}, method="POST")
            assert status["status"] != "unavailable", "GenOffice is required for packaged Word acceptance"
            assert time.monotonic() < deadline, status
            time.sleep(0.2)
        page = api.bytes(f"/api/tasks/{task_id}/preview/render/page/1?variant=translated")
        assert page.startswith(b"\x89PNG")
        (args.output / "translated-page1.png").write_bytes(page)
        (args.output / "report.json").write_text(json.dumps({"segments": len(segments), "render": status}, indent=2))
    print("PASS: frozen Word engine preserves styles/links/media and text-box fallback copies after translation and revision")
    print("PASS: GenOffice renders the revised structured Word output through the packaged API")


if __name__ == "__main__":
    main()

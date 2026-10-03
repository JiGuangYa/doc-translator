"""Render portable mixed-style/link/text-box Word originals and fixed translations."""

import json
import os
import sys
import tempfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2]
    folder = root / "build/word-qa"
    folder.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="doc-translator-word-render-") as data:
        os.environ["DOC_TRANSLATOR_DATA_DIR"] = data
        os.environ["DOC_TRANSLATOR_DESKTOP"] = "1"
        from app.formats import docx_fmt
        from app.services import process_runner, renderer
        from tests.docx_fixtures import complex_docx

        source = complex_docx(folder / "Word fidelity original.docx", compatibility_copy=True)
        target = folder / "Word fidelity translated.docx"
        translations = {
            "Document fidelity check": "文档保真检查", "Important ": "重要 ",
            "instructions: ": "说明： ", "read the manual": "阅读手册", " before starting.": " 后开始操作。",
            "Component": "部件", "Status": "状态", "Control module": "控制模块", "Ready for review": "等待校对",
            "A local picture is preserved below.": "下方保留本机图片。",
            "Text inside a nested text box": "嵌套文本框内的文字",
            "A final paragraph follows the text box.": "这是文本框之后的最后一段。",
            "Engineering review / Local test": "工程校对 / 本机测试",
        }
        result = docx_fmt.extract(source, {"docx_version": 2})
        values = {f"s{i:06d}": translations[segment.text] for i, segment in enumerate(result.segments)}
        report = docx_fmt.write_back(source, target, values, {"docx_version": 2})
        app = renderer.genoffice_path()
        if not app:
            raise RuntimeError("Install GenOffice to run visual acceptance")
        rendered = {}
        for name, path in (("original", source), ("translated", target)):
            output = process_runner.run([app, "render", str(path), "--out", str(folder / name), "--json"],
                                        timeout=60, label="Word visual verification")
            value = json.loads(output.stdout.strip().splitlines()[-1])
            if output.returncode or value.get("status") != "ok":
                raise RuntimeError(f"GenOffice could not render the Word fixture: {value}")
            rendered[name] = value["detail"]
        report = {"written": report.written, "warnings": report.warnings, "renders": rendered}
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

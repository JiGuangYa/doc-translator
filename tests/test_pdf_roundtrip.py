"""PDF module round-trip tests: dynamically build small PDFs to verify
extract / write_back / scanned-document detection."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf as fitz  # noqa: E402

from app.formats import pdf_fmt  # noqa: E402

INTEGRATION_PDF = Path(r"C:\fcc\office\i2Active Oracle User Guide.pdf")


def _make_pdf(path: Path, lines: list[str]) -> Path:
    """Create a single-page PDF with the given English lines, return the path."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    y = 72.0
    for line in lines:
        page.insert_textbox(fitz.Rect(72, y, 500, y + 24), line,
                            fontname="helv", fontsize=12)
        y += 28.0
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="module")
def sample_pdf(tmp_path_factory) -> Path:
    return _make_pdf(tmp_path_factory.mktemp("pdf") / "sample.pdf", [
        "Hello world",
        "This is a sample document for translation testing.",
        "The quick brown fox jumps over the lazy dog.",
    ])


def test_extract(sample_pdf):
    result = pdf_fmt.extract(sample_pdf, {})
    assert len(result.segments) > 0
    for seg in result.segments:
        assert seg.text.strip()
        bbox = seg.meta["bbox"]
        assert bbox[2] > bbox[0] and bbox[3] > bbox[1]  # valid bbox
        assert seg.meta["page"] == 1
        assert seg.meta["size"] > 0


def test_write_back_identity(sample_pdf, tmp_path):
    result = pdf_fmt.extract(sample_pdf, {})
    translations = {seg.seg_id: seg.text for seg in result.segments}
    dst = tmp_path / "identity.pdf"
    report = pdf_fmt.write_back(sample_pdf, dst, translations, {})

    assert report.written == len(translations)
    assert not report.overflow
    out = fitz.open(dst)
    try:
        assert out.page_count == 1
        text = out[0].get_text()
        assert "Hello world" in text
        assert "quick brown fox" in text
    finally:
        out.close()


def test_write_back_translate(sample_pdf, tmp_path):
    result = pdf_fmt.extract(sample_pdf, {})
    translations = {}
    for seg in result.segments:
        if seg.text.strip() == "Hello world":
            translations[seg.seg_id] = "Hello world"
    assert translations, "the sample should contain a 'Hello world' segment"

    dst = tmp_path / "translated.pdf"
    options = {"target_lang": "zh"}
    report = pdf_fmt.write_back(sample_pdf, dst, translations, options)

    assert report.written >= 1
    out = fitz.open(dst)
    try:
        text = out[0].get_text()
        # writeback re-fits into a tighter bbox and may wrap the translation;
        # verify each word is present and nothing else is missing
        assert "Hello" in text
        assert "world" in text
        # untranslated segments keep the original
        assert "quick brown fox" in text
    finally:
        out.close()


def test_scanned_pdf(tmp_path):
    doc = fitz.open()
    doc.new_page()  # blank page, no text layer
    path = tmp_path / "blank.pdf"
    doc.save(path)
    doc.close()

    with pytest.raises(ValueError, match="(?i)scan"):
        pdf_fmt.extract(path, {})


@pytest.mark.skipif(not INTEGRATION_PDF.exists(), reason="integration sample not found")
def test_integration_extract_real_pdf():
    result = pdf_fmt.extract(INTEGRATION_PDF, {})
    assert len(result.segments) > 50

"""Create portable scan/crop fixtures for the actual Apple Vision test path."""

import argparse
import sys
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--long")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from tests.pdf_fixtures import long_scan_pdf, mixed_pdf
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    mixed_pdf(path)
    with fitz.open(path) as document:
        for page in (2, 4):
            document[page - 1].get_pixmap(dpi=110).save(path.with_name(f"ocr-page-{page}.png"))
    font = Path("/System/Library/Fonts/STHeiti Light.ttc")
    if font.exists():
        image = Image.new("RGB", (1200, 600), "white")
        ImageDraw.Draw(image).text((70, 150), "扫描文字校对", fill="black", font=ImageFont.truetype(str(font), 72))
        chinese = canvas.Canvas(str(path.with_name("ocr-chinese.pdf")), pagesize=(600, 300))
        chinese.drawImage(ImageReader(image), 0, 0, width=600, height=300)
        chinese.save()
    if args.long:
        long_scan_pdf(Path(args.long))
    print(path)


if __name__ == "__main__":
    main()

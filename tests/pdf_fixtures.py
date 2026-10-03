"""Portable PDF inputs generated independently of the translation writer."""

from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def scan_image(title, *, size=(1200, 800), dense=False):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=42)
    draw.text((70, 80), title, fill="black", font=font)
    if dense:
        for number in range(24):
            draw.text((60, 150 + number * 50), f"Local recognition body row {number:02d}. Preserve text.",
                      fill="black", font=ImageFont.load_default(size=36))
    else:
        draw.text((70, 190), "Review this source before translation.", fill="black",
                  font=ImageFont.load_default(size=32))
    return image


def mixed_pdf(path: Path) -> Path:
    drawing = canvas.Canvas(str(path), pagesize=(600, 400))
    drawing.setFillColorRGB(0.88, 0.94, 0.98)
    drawing.rect(30, 300, 540, 70, fill=1, stroke=0)
    drawing.setFillColorRGB(0.1, 0.15, 0.3)
    drawing.setFont("Helvetica-Bold", 18)
    drawing.drawString(50, 340, "Native page one")
    drawing.linkURL("https://example.com/source", (50, 334, 240, 362), relative=0)
    drawing.setFont("Helvetica", 12)
    drawing.drawString(50, 280, "This paragraph already has a native PDF text layer.")
    drawing.showPage()
    drawing.drawImage(ImageReader(scan_image("SCANNED PAGE TWO")), 0, 0, width=600, height=400)
    drawing.setFont("Helvetica", 8)
    drawing.drawString(16, 10, "Page 2")
    drawing.showPage()
    drawing.setFont("Helvetica", 16)
    drawing.drawString(50, 330, "Native page three")
    drawing.setStrokeColorRGB(0.2, 0.5, 0.3)
    drawing.line(50, 100, 400, 100)
    drawing.line(50, 100, 50, 260)
    drawing.setFillColorRGB(0.2, 0.5, 0.3)
    drawing.rect(90, 100, 50, 110, fill=1, stroke=0)
    drawing.rect(180, 100, 50, 150, fill=1, stroke=0)
    drawing.showPage()
    image = scan_image("SCANNED PAGE FOUR", size=(800, 1200)).rotate(90, expand=True)
    drawing.drawImage(ImageReader(image), 0, 0, width=600, height=400)
    drawing.linkURL("https://example.com/rotated", (75, 30, 96, 370), relative=0)
    drawing.save()
    temporary = path.with_name(path.stem + "-rotated.pdf")
    with fitz.open(path) as document:
        document[3].set_cropbox(fitz.Rect(10, 10, 590, 390))
        document[3].set_rotation(90)
        document.save(temporary)
    temporary.replace(path)
    return path


def long_scan_pdf(path: Path, pages=16) -> Path:
    drawing = canvas.Canvas(str(path), pagesize=(600, 800))
    image = ImageReader(scan_image("SCANNED MEMORY CHECK", size=(1200, 1600), dense=True))
    for number in range(1, pages + 1):
        drawing.drawImage(image, 0, 0, width=600, height=800)
        drawing.setFont("Helvetica", 8)
        drawing.drawString(12, 8, f"Page {number}")
        drawing.showPage()
    drawing.save()
    return path

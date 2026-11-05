import asyncio
import io
import shutil
from pathlib import Path

import pytest

from app.services.document_extraction import ExtractionError, extract_file, validate_text
from app.workers.extract_document import check_pixels, extract, ocr


def native_pdf():
    stream = b"BT /F1 20 Tf 40 150 Td (REFUND POLICY 7 DAYS) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 250] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result, offsets = b"%PDF-1.4\n", [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{i} 0 obj\n".encode() + value + b"\nendobj\n"
    start = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return result + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()


@pytest.fixture
def korean_image():
    from PIL import Image, ImageDraw, ImageFont
    if shutil.which("tesseract") is None:
        pytest.skip("실제 OCR 통합 검증에는 Tesseract와 kor+eng가 필요합니다.")
    fonts = ["/System/Library/Fonts/AppleSDGothicNeo.ttc", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
    font_path = next((p for p in fonts if Path(p).is_file()), None)
    if font_path is None:
        pytest.skip("한글 OCR 검증용 글꼴이 필요합니다.")
    import pytesseract
    if not {"kor", "eng"}.issubset(pytesseract.get_languages()):
        pytest.skip("kor+eng 언어 데이터가 필요합니다.")
    image = Image.new("RGB", (1600, 320), "white")
    ImageDraw.Draw(image).text((60, 80), "환불 신청은 결제 후 7일 이내 가능합니다", fill="black", font=ImageFont.truetype(font_path, 58))
    return image


@pytest.mark.anyio
async def test_real_korean_png_and_scanned_pdf(korean_image):
    for format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("PDF", "application/pdf")):
        buffer = io.BytesIO()
        korean_image.save(buffer, format=format)
        text, digest = await extract_file(buffer.getvalue(), mime)
        assert "신청" in text and "7" in text and len(digest) == 64


@pytest.mark.anyio
async def test_native_pdf_and_mixed_pages(korean_image):
    import pypdfium2 as pdfium
    text, _ = await extract_file(native_pdf(), "application/pdf")
    assert "REFUND POLICY 7 DAYS" in text
    scan, output = io.BytesIO(), io.BytesIO()
    korean_image.save(scan, format="PDF")
    with pdfium.PdfDocument.new() as document, pdfium.PdfDocument(native_pdf()) as native, pdfium.PdfDocument(scan.getvalue()) as raster:
        document.import_pages(native)
        document.import_pages(raster)
        document.save(output)
    text, _ = await extract_file(output.getvalue(), "application/pdf")
    assert "REFUND POLICY 7 DAYS" in text and "신청" in text


@pytest.mark.anyio
async def test_corrupt_blank_and_page_limit():
    import pypdfium2 as pdfium
    from PIL import Image
    with pytest.raises(ExtractionError, match="INVALID_OR_ENCRYPTED_DOCUMENT"):
        await extract_file(b"%PDF-corrupt", "application/pdf")
    buffer = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(buffer, format="PNG")
    if shutil.which("tesseract"):
        with pytest.raises(ExtractionError, match="EMPTY_DOCUMENT"):
            await extract_file(buffer.getvalue(), "image/png")
    output = io.BytesIO()
    with pdfium.PdfDocument.new() as document:
        for _ in range(21):
            document.new_page(100, 100).close()
        document.save(output)
    with pytest.raises(ExtractionError, match="DOCUMENT_PAGE_LIMIT"):
        await extract_file(output.getvalue(), "application/pdf")


def test_encryption_pixel_and_ocr_timeout(monkeypatch):
    import pypdfium2 as pdfium
    import pytesseract
    from PIL import Image
    monkeypatch.setattr(pdfium.raw, "FPDF_GetSecurityHandlerRevision", lambda _: 4)
    with pytest.raises(ExtractionError, match="ENCRYPTED_PDF"):
        extract(native_pdf(), "application/pdf")
    with pytest.raises(ExtractionError, match="DOCUMENT_PIXEL_LIMIT"):
        check_pixels(5000, 5000)
    def timeout(*args, **kwargs):
        assert kwargs["lang"] == "kor+eng" and kwargs["timeout"] == 30
        raise RuntimeError("timeout")
    monkeypatch.setattr(pytesseract, "image_to_string", timeout)
    with pytest.raises(ExtractionError, match="OCR_PAGE_TIMEOUT"):
        ocr(Image.new("RGB", (10, 10)))


@pytest.mark.anyio
async def test_document_timeout_kills_process_group(monkeypatch):
    import app.services.document_extraction as module
    class Child:
        pid = 123456789
        waited = False
        async def communicate(self, data):
            await asyncio.sleep(1)
        async def wait(self):
            self.waited = True
    child, killed = Child(), []
    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"]
        return child
    async def timeout(awaitable, timeout):
        assert timeout == 300
        awaitable.close()
        raise TimeoutError
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.asyncio, "wait_for", timeout)
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: killed.append(pid))
    with pytest.raises(ExtractionError, match="DOCUMENT_EXTRACTION_TIMEOUT"):
        await extract_file(b"%PDF-test", "application/pdf")
    assert child.waited and killed == [child.pid]


def test_exact_ai_chunk_boundary_and_hash():
    text = "가" * 54550
    assert validate_text(text)[0] == text
    with pytest.raises(ExtractionError, match="DOCUMENT_CHUNK_LIMIT"):
        validate_text(text + "나")
    with pytest.raises(ExtractionError, match="DOCUMENT_TEXT_LIMIT"):
        validate_text("가" * 200001)
    with pytest.raises(ExtractionError, match="EMPTY_DOCUMENT"):
        validate_text(" \n")

"""PDFium/Tesseract 전용 자식 프로세스. stdout은 결과 JSON에만 사용한다."""
import io
import json
import math
import sys

from app.services.document_extraction import ExtractionError

MAX_PIXELS = 20_000_000


def check_pixels(width: int, height: int):
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise ExtractionError("DOCUMENT_PIXEL_LIMIT")


def ocr(image) -> str:
    import pytesseract

    check_pixels(*image.size)
    try:
        return pytesseract.image_to_string(image, lang="kor+eng", timeout=30)
    except pytesseract.TesseractNotFoundError:
        raise ExtractionError("OCR_NOT_CONFIGURED") from None
    except pytesseract.TesseractError:
        raise ExtractionError("OCR_FAILED_OR_LANGUAGE_MISSING") from None
    except RuntimeError:
        raise ExtractionError("OCR_PAGE_TIMEOUT") from None


def extract(data: bytes, mime: str) -> str:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    if mime in ("image/png", "image/jpeg"):
        with Image.open(io.BytesIO(data)) as image:
            check_pixels(*image.size)
            if getattr(image, "n_frames", 1) != 1:
                raise ExtractionError("MULTIFRAME_IMAGE_UNSUPPORTED")
            image.load()
            with ImageOps.exif_transpose(image).convert("RGB") as normalized:
                return ocr(normalized)
    if mime != "application/pdf":
        raise ExtractionError("UNSUPPORTED_FILE_TYPE")
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(data) as document:
        if pdfium.raw.FPDF_GetSecurityHandlerRevision(document) >= 0:
            raise ExtractionError("ENCRYPTED_PDF")
        if not 1 <= len(document) <= 20:
            raise ExtractionError("DOCUMENT_PAGE_LIMIT")
        pages = []
        for index in range(len(document)):
            with document[index] as page:
                with page.get_textpage() as textpage:
                    text = textpage.get_text_range()
                if not text.strip():
                    check_pixels(math.ceil(page.get_width() * 2), math.ceil(page.get_height() * 2))
                    with page.render(scale=2) as bitmap:
                        with bitmap.to_pil() as image:
                            text = ocr(image)
                # 빈 페이지는 보존하되 문서 전체가 비었는지는 부모에서 검증한다.
                pages.append(text)
                if sum(map(len, pages)) > 200_000:
                    raise ExtractionError("DOCUMENT_TEXT_LIMIT")
        return "\n\n".join(pages)


def main():
    try:
        data = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ExtractionError("DOCUMENT_FILE_LIMIT")
        result = {"text": extract(data, sys.argv[1])}
    except ExtractionError as exc:
        result = {"error": exc.code}
    except ImportError:
        result = {"error": "EXTRACTION_DEPENDENCY_MISSING"}
    except Exception:
        # PDF/OCR 예외에는 원문·경로가 들어갈 수 있으므로 공개 오류는 코드로 제한한다.
        result = {"error": "INVALID_OR_ENCRYPTED_DOCUMENT"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Extraction in a bounded worker; never executes document content."""
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import subprocess
import sys

SUPPORTED = {".md", ".txt", ".pdf", ".html", ".htm"}
MAX_TEXT = 2_000_000


@dataclass
class Section:
    text: str
    page: int | None = None
    reliable_lines: bool = True


def extract_local(path):
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Unsupported file type: {suffix}")
    if suffix == ".pdf":
        from pypdf import PdfReader  # noqa: PLC0415 - pypdf and Pillow load only to read a PDF
        reader = PdfReader(str(path), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported")
        if len(reader.pages) > 2000:
            raise ValueError("PDF exceeds 2000-page limit")
        sections = []
        size = 0
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            size += len(text)
            if size > MAX_TEXT:
                raise ValueError("Extracted text exceeds size limit")
            sections.append(Section(text, i + 1, False))
    else:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        if "\x00" in text:
            raise ValueError("NUL bytes in text input")
        if suffix in (".html", ".htm"):
            from bs4 import BeautifulSoup  # noqa: PLC0415 - BeautifulSoup loads only for HTML sources
            soup = BeautifulSoup(text, "html.parser")
            for item in soup.select("script,style,nav,header,footer,aside,form"):
                item.decompose()
            root = soup.find("main") or soup.find("article") or soup
            for heading in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
                heading.replace_with("\n" + "#" * int(heading.name[1]) + " " + heading.get_text(" ", strip=True) + "\n")
            text = root.get_text("\n", strip=True)
        sections = [Section(text, reliable_lines=suffix in (".md", ".txt"))]
    if sum(len(s.text) for s in sections) > MAX_TEXT:
        raise ValueError("Extracted text exceeds size limit")
    if not any(s.text.strip() for s in sections):
        raise ValueError("No extractable text (empty or image-only/scanned document; OCR is not supported)")
    return sections


def extract(path):
    result = subprocess.run([sys.executable, "-m", "local_knowledge.extraction", str(path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        # Worker emits a short controlled error; never expose parser tracebacks or bodies.
        message = result.stdout.strip()[:400] or "Extraction worker failed or exceeded resource limits"
        raise ValueError(message)
    return [Section(**item) for item in json.loads(result.stdout)]


if __name__ == "__main__":
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    try:
        print(json.dumps([asdict(s) for s in extract_local(Path(sys.argv[1]))]))
    except Exception as exc:
        detail = str(exc)[:250] if type(exc) is ValueError else type(exc).__name__
        print(f"Extraction failed: {detail}")
        sys.exit(1)

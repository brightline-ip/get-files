from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit
from defusedxml import ElementTree
from .extraction_errors import InputError


SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".html", ".htm", ".pdf", ".docx",
    ".xlsx", ".xlsm", ".pptx", ".epub",
}


@dataclass
class Section:
    locator: str
    text: str
    heading: str = ""


@dataclass
class Extraction:
    sections: list[Section]
    metadata: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def extract_pdf_page(page) -> tuple[str, str]:
    """Select layout extraction for pages with repeated table leaders."""
    plain = page.extract_text(orientations=(0, 90, 180, 270)) or ""
    leader_lines = sum(bool(re.search(r"\.{5,}", line)) for line in plain.splitlines())
    if leader_lines < 2:
        return plain, "plain"
    try:
        layout = page.extract_text(extraction_mode="layout") or ""
    except Exception:
        return plain, "layout_rejected"
    plain_chars = [character for character in plain if not character.isspace()]
    layout_chars = [character for character in layout if not character.isspace()]
    if not layout_chars or Counter(layout_chars) != Counter(plain_chars):
        return plain, "layout_rejected"
    return layout, "layout"


class _HTMLText(HTMLParser):
    block_tags = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    suppressed_tags = {"script", "style", "template", "noscript", "head", "title", "svg", "canvas", "object"}

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.ambiguous_reading_order = False
        self._suppressed: list[str] = []
        self._anchors: list[dict[str, object]] = []
        self._title_depth = 0
        parsed_base = urlsplit(base_url) if base_url else None
        self._base_url = base_url if parsed_base and parsed_base.scheme.lower() in {"http", "https"} else None

    def _append(self, value: str) -> None:
        target = self._anchors[-1]["parts"] if self._anchors else self.parts
        assert isinstance(target, list)
        target.append(value)

    def _safe_href(self, value: str | None) -> str | None:
        if not value:
            return None
        href = value.strip()
        if not href or any(ord(character) < 32 for character in href):
            return None
        scheme = urlsplit(href).scheme.lower()
        if scheme and scheme not in {"http", "https", "mailto"}:
            return None
        return urljoin(self._base_url, href) if self._base_url else href

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "title":
            self._title_depth += 1
        if self._suppressed:
            if tag in self.suppressed_tags:
                self._suppressed.append(tag)
            return
        if tag in self.suppressed_tags:
            self._suppressed.append(tag)
            return
        if tag == "a":
            if self._anchors:
                self.ambiguous_reading_order = True
            href = next((value for name, value in attrs if name.lower() == "href"), None)
            self._anchors.append({"href": self._safe_href(href), "parts": []})
            return
        if tag in self.block_tags:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if self._suppressed:
            if tag in self._suppressed:
                while self._suppressed:
                    opened = self._suppressed.pop()
                    if opened == tag:
                        break
            elif tag in self.suppressed_tags:
                self.ambiguous_reading_order = True
            return
        if tag == "a":
            if not self._anchors:
                self.ambiguous_reading_order = True
                return
            anchor = self._anchors.pop()
            raw_text = "".join(anchor["parts"])
            label = raw_text.strip()
            href = anchor["href"]
            rendered = raw_text
            if isinstance(href, str):
                self.links.append({"text": label, "href": href})
                if label and label != href:
                    leading = raw_text[: len(raw_text) - len(raw_text.lstrip())]
                    trailing = raw_text[len(raw_text.rstrip()) :]
                    rendered = f"{leading}[{label}]({href}){trailing}"
            self._append(rendered)
            return
        if tag in self.block_tags:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title_parts.append(data)
        if not self._suppressed:
            self._append(data)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def text(self) -> str:
        if self._suppressed or self._anchors:
            self.ambiguous_reading_order = True
        while self._anchors:
            anchor = self._anchors.pop()
            self._append("".join(anchor["parts"]))
        return "".join(self.parts)

    def title(self) -> str | None:
        title = " ".join("".join(self.title_parts).split())
        return title or None

    def metadata(self) -> dict:
        metadata: dict = {}
        if self.title():
            metadata["html_title"] = self.title()
        if self.links:
            metadata["html_links"] = list(self.links)
        if self.ambiguous_reading_order:
            metadata["html_ambiguous_reading_order"] = True
        return metadata


def _decode(raw: bytes) -> tuple[str, str]:
    encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1252")
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace"), "utf-8-replacement"


def _plain(path: Path) -> Extraction:
    text, encoding = _decode(path.read_bytes())
    metadata = {"encoding": encoding}
    warnings = []
    if path.suffix.lower() in {".html", ".htm"}:
        parser = _HTMLText()
        parser.feed(text)
        parser.close()
        text = parser.text()
        metadata.update(parser.metadata())
        if parser.links:
            warnings.append("HTML link targets are preserved inline; verify reading order for complex HTML")
        if parser.ambiguous_reading_order:
            warnings.append("Malformed or nested HTML may have ambiguous reading order")
    elif path.suffix.lower() == ".xml":
        try:
            root = ElementTree.fromstring(text)
            text = "\n".join(value.strip() for value in root.itertext() if value.strip())
        except ElementTree.ParseError:
            pass
    return Extraction([Section("document", text)], metadata, warnings)


def _delimited(path: Path) -> Extraction:
    text, encoding = _decode(path.read_bytes())
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rendered = []
    for index, row in enumerate(rows, 1):
        rendered.append(f"Row {index}: " + " | ".join(f"{i + 1}={value}" for i, value in enumerate(row)))
    return Extraction([Section("table", "\n".join(rendered), "Tabular data")], {"encoding": encoding, "rows": len(rows)})


def _json(path: Path) -> Extraction:
    text, encoding = _decode(path.read_bytes())
    try:
        if path.suffix.lower() == ".jsonl":
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
        else:
            json.loads(text)
        # Validation must not erase duplicate keys, reorder data, or round numbers.
    except json.JSONDecodeError:
        return Extraction([Section("document", text)], {"encoding": encoding}, ["Invalid JSON; original text preserved"])
    return Extraction([Section("document", text)], {"encoding": encoding})


def _pdf(path: Path) -> Extraction:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if len(reader.pages) > 2000:
        raise InputError("PDF exceeds 2000 pages")
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("PDF is encrypted and cannot be opened without a password") from exc
    sections, warnings, layout_pages, layout_risk_pages = [], [], [], []
    for number, page in enumerate(reader.pages, 1):
        text, mode = extract_pdf_page(page) if "/Contents" in page else ("", "plain")
        if mode == "layout":
            layout_pages.append(number)
        elif mode == "layout_rejected":
            layout_risk_pages.append(number)
        if not text.strip():
            warnings.append(f"Page {number} contains no extractable text; OCR may be required")
        sections.append(Section(f"page:{number}", text, f"Page {number}"))
    meta = {"pages": len(reader.pages)}
    if layout_pages:
        meta["pdf_layout_pages"] = layout_pages
        warnings.append("Layout-aware native extraction was used on pages with repeated table leaders; verify table reading order against the original.")
    if layout_risk_pages:
        meta["pdf_layout_risk_pages"] = layout_risk_pages
        warnings.append("Layout-sensitive native pages could not safely use layout extraction without changing content; plain reading order requires review.")
    if reader.metadata:
        for key in ("title", "author", "subject", "creator"):
            value = getattr(reader.metadata, key, None)
            if value:
                meta[key] = str(value)
    return Extraction(sections, meta, warnings)


def _docx(path: Path) -> Extraction:
    from docx import Document

    document = Document(str(path))
    sections: list[Section] = []
    current_heading = "Document"
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            sections.append(Section(f"section:{len(sections) + 1}", "\n\n".join(buffer), current_heading))
            buffer.clear()

    for block in document.iter_inner_content():
        if not hasattr(block, "text"):
            flush()
            rows = [" | ".join(cell.text for cell in row.cells) for row in block.rows]
            sections.append(Section(f"table:{len(sections) + 1}", "\n".join(rows), "Table"))
            continue
        paragraph = block
        text = paragraph.text
        if not text:
            continue
        style = paragraph.style.name.lower() if paragraph.style else ""
        if style.startswith("heading"):
            flush()
            current_heading = text
            buffer.append(text)
        else:
            buffer.append(text)
    flush()
    props = document.core_properties
    meta = {key: str(value) for key in ("title", "author", "subject", "keywords") if (value := getattr(props, key, None))}
    return Extraction(sections or [Section("document", "")], meta,
                      ["DOCX extraction covers body paragraphs and tables; headers, footnotes, and embedded objects may be omitted"])


def _xlsx(path: Path) -> Extraction:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=False, keep_links=False)
    sections: list[Section] = []
    try:
        cells = 0
        for sheet in workbook.worksheets:
            lines = []
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                cells += len(row)
                if cells > 1_000_000:
                    raise InputError("Workbook exceeds one million cells")
                values = ["" if value is None else str(value) for value in row]
                if any(values):
                    lines.append(f"Row {row_number}: " + " | ".join(f"C{i + 1}={value}" for i, value in enumerate(values)))
            sections.append(Section(f"sheet:{sheet.title}", "\n".join(lines), f"Sheet: {sheet.title}"))
        return Extraction(sections, {"sheets": len(workbook.sheetnames), "sheet_names": workbook.sheetnames},
                          ["Formulas are preserved as expressions, not calculated; charts, images and formatting are not extracted"])
    finally:
        workbook.close()


def _pptx(path: Path) -> Extraction:
    from pptx import Presentation

    presentation = Presentation(str(path))
    sections = []
    for number, slide in enumerate(presentation.slides, 1):
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                texts.append(shape.text.strip())
            if getattr(shape, "has_table", False):
                texts.extend(" | ".join(cell.text.strip() for cell in row.cells) for row in shape.table.rows)
        heading = texts[0].splitlines()[0][:160] if texts else f"Slide {number}"
        sections.append(Section(f"slide:{number}", "\n\n".join(texts), heading))
    return Extraction(sections, {"slides": len(presentation.slides)}, ["Slide images, some grouped objects and speaker notes may be omitted"])


def _epub(path: Path) -> Extraction:
    sections = []
    html_titles = []
    html_links = []
    ambiguous = False
    with zipfile.ZipFile(path) as archive:
        names = sorted(name for name in archive.namelist() if name.lower().endswith((".xhtml", ".html", ".htm")))
        for number, name in enumerate(names, 1):
            text, _ = _decode(archive.read(name))
            parser = _HTMLText()
            parser.feed(text)
            parser.close()
            sections.append(Section(f"item:{number}", parser.text(), Path(name).stem))
            if parser.title():
                html_titles.append({"locator": f"item:{number}", "title": parser.title()})
            html_links.extend({"locator": f"item:{number}", **link} for link in parser.links)
            ambiguous = ambiguous or parser.ambiguous_reading_order
    metadata = {"content_items": len(sections)}
    if html_titles:
        metadata["html_titles"] = html_titles
    if html_links:
        metadata["html_links"] = html_links
    if ambiguous:
        metadata["html_ambiguous_reading_order"] = True
    warnings = ["EPUB items use filename order; reading order and images are not preserved"]
    if html_links:
        warnings.append("HTML link targets are preserved inline")
    return Extraction(sections, metadata, warnings)


def extract(path: Path) -> Extraction:
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {extension or '[no extension]'}")
    if extension == ".pdf":
        return _pdf(path)
    if extension == ".docx":
        return _docx(path)
    if extension in {".xlsx", ".xlsm"}:
        return _xlsx(path)
    if extension == ".pptx":
        return _pptx(path)
    if extension == ".epub":
        return _epub(path)
    if extension in {".csv", ".tsv"}:
        return _delimited(path)
    if extension in {".json", ".jsonl"}:
        return _json(path)
    return _plain(path)


def discover(path: Path, recursive: bool = True) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path
        return
    scanned = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {".git", ".hermes", ".codex", ".venv", "node_modules"}
                   and not (Path(directory) / name).is_symlink() and not (Path(directory) / name).is_junction()]
        scanned += len(dirs) + len(files)
        if scanned > 10000:
            raise InputError("Folder scan exceeds 10000 entries; select a smaller folder")
        for name in files:
            candidate = Path(directory) / name
            if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield candidate
        if not recursive:
            break

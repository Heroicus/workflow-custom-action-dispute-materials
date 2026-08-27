#!/usr/bin/env python3
"""Extract complete, source-only text from dispute-material attachments."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence
import xml.etree.ElementTree as ET


TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
OOXML_SUFFIXES = {".docx", ".xlsx", ".pptx"}
IGNORED_NAMES = {".ds_store", "thumbs.db"}
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MIN_TEXT_CHARS_PER_PDF_PAGE = 24


class MaterialError(Exception):
    """A stable material extraction failure."""


@dataclass
class ExtractionResult:
    file_name: str
    sha256: str
    size: int
    status: str
    text_chars: int
    page_count: int | None = None
    methods: list[str] = field(default_factory=list)
    failed_units: list[str] = field(default_factory=list)
    children: list[dict[str, object]] = field(default_factory=list)
    text: str = field(default="", repr=False)


def normalized_char_count(value: str) -> int:
    return len(re.sub(r"\s+", "", value or ""))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def xml_text(payload: bytes) -> str:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return ""
    values: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"t", "v"} and element.text:
            values.append(element.text)
    return "\n".join(values)


def extract_ooxml(path: Path) -> tuple[str, list[str]]:
    suffix = path.suffix.lower()
    prefixes = {
        ".docx": ("word/document.xml", "word/header", "word/footer", "word/footnotes.xml", "word/endnotes.xml"),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/", "xl/comments"),
        ".pptx": ("ppt/slides/", "ppt/notesSlides/"),
    }[suffix]
    parts: list[str] = []
    methods = [suffix.lstrip(".") + "_xml"]
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith(".xml") and any(name.startswith(prefix) for prefix in prefixes):
                text = xml_text(archive.read(name))
                if text.strip():
                    parts.append(f"[{name}]\n{text}")
                continue
            if Path(name).suffix.lower() not in IMAGE_SUFFIXES or "/media/" not in name:
                continue
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(suffix=Path(name).suffix, delete=False) as handle:
                    temporary_name = handle.name
                    handle.write(archive.read(name))
                text = run_tesseract(Path(temporary_name))
                if text:
                    parts.append(f"[{name}:OCR]\n{text}")
                    if "embedded_ocr" not in methods:
                        methods.append("embedded_ocr")
            except MaterialError:
                pass
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
    return "\n\n".join(parts), methods


def is_ole_file(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(8) == bytes.fromhex("d0cf11e0a1b11ae1")


def run_tesseract(image: Path, timeout: int = 120) -> str:
    executable = shutil.which("tesseract")
    if not executable:
        raise MaterialError("tesseract unavailable")
    languages = "chi_sim+eng"
    completed = subprocess.run(
        [executable, str(image), "stdout", "-l", languages, "--psm", "6"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise MaterialError(completed.stderr.strip() or f"tesseract exited {completed.returncode}")
    return completed.stdout.strip()


def extract_image(path: Path) -> tuple[str, list[str]]:
    return run_tesseract(path), ["ocr"]


def extract_pdf(path: Path) -> tuple[str, int, list[str], list[str]]:
    try:
        import fitz  # type: ignore
    except ImportError:
        return extract_pdf_cli(path)

    document = fitz.open(str(path))
    pages: list[str] = []
    methods: set[str] = set()
    failures: list[str] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            text = page.get_text("text").strip()
            if normalized_char_count(text) >= MIN_TEXT_CHARS_PER_PDF_PAGE:
                methods.add("pdf_text")
            else:
                temporary_name = ""
                try:
                    pixmap = page.get_pixmap(dpi=170, alpha=False)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                        temporary_name = handle.name
                    pixmap.save(temporary_name)
                    ocr_text = run_tesseract(Path(temporary_name))
                    if ocr_text:
                        text = ocr_text
                    methods.add("ocr")
                except Exception as exc:  # page-level failure remains auditable
                    failures.append(f"page:{page_index + 1}:{type(exc).__name__}")
                finally:
                    if temporary_name:
                        Path(temporary_name).unlink(missing_ok=True)
            pages.append(f"[第{page_index + 1}页]\n{text}" if text else f"[第{page_index + 1}页]")
    finally:
        document.close()
    return "\n\n".join(pages), len(pages), sorted(methods), failures


def extract_pdf_cli(path: Path) -> tuple[str, int, list[str], list[str]]:
    pdftotext = shutil.which("pdftotext")
    pdfinfo = shutil.which("pdfinfo")
    pdftoppm = shutil.which("pdftoppm")
    if not pdftotext or not pdfinfo:
        raise MaterialError("PDF extraction unavailable")
    info = subprocess.run(
        [pdfinfo, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=60, check=False,
    )
    match = re.search(r"^Pages:\s+(\d+)", info.stdout, flags=re.MULTILINE)
    if info.returncode != 0 or not match:
        raise MaterialError(info.stderr.strip() or "pdfinfo failed")
    page_count = int(match.group(1))
    pages: list[str] = []
    methods: set[str] = set()
    failures: list[str] = []
    for page_number in range(1, page_count + 1):
        converted = subprocess.run(
            [pdftotext, "-f", str(page_number), "-l", str(page_number), "-layout", str(path), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90, check=False,
        )
        text = converted.stdout.strip() if converted.returncode == 0 else ""
        if normalized_char_count(text) >= MIN_TEXT_CHARS_PER_PDF_PAGE:
            methods.add("pdf_text")
        elif pdftoppm:
            with tempfile.TemporaryDirectory(prefix="odm-pdf-page-") as directory:
                prefix = Path(directory) / "page"
                rendered = subprocess.run(
                    [pdftoppm, "-f", str(page_number), "-l", str(page_number), "-r", "170", "-png", "-singlefile", str(path), str(prefix)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False,
                )
                image = prefix.with_suffix(".png")
                try:
                    if rendered.returncode != 0 or not image.is_file():
                        raise MaterialError(rendered.stderr.strip() or "pdftoppm failed")
                    ocr_text = run_tesseract(image)
                    if ocr_text:
                        text = ocr_text
                    methods.add("ocr")
                except MaterialError as exc:
                    failures.append(f"page:{page_number}:{type(exc).__name__}")
        else:
            failures.append(f"page:{page_number}:OCRUnavailable")
        pages.append(f"[第{page_number}页]\n{text}" if text else f"[第{page_number}页]")
    return "\n\n".join(pages), page_count, sorted(methods), failures


def extract_legacy_office(path: Path) -> tuple[str, list[str]]:
    commands: list[tuple[str, list[str]]] = []
    if path.suffix.lower() == ".doc":
        commands.extend((name, [binary, str(path)]) for name, binary in (("antiword", shutil.which("antiword")), ("catdoc", shutil.which("catdoc"))) if binary)
    elif path.suffix.lower() in {".xls", ".xlsx"}:
        binary = shutil.which("xls2csv")
        if binary:
            commands.append(("xls2csv", [binary, str(path)]))
    for method, command in commands:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False)
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout, [method]

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        with tempfile.TemporaryDirectory(prefix="odm-office-") as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            if path.suffix.lower() == ".doc":
                conversion = "txt:Text"
                expected_suffix = ".txt"
                conversion_source = path
            else:
                conversion = "xlsx"
                expected_suffix = ".xlsx"
                input_dir = root / "input"
                input_dir.mkdir()
                conversion_source = input_dir / f"{path.stem}.xls"
                shutil.copyfile(path, conversion_source)
            completed = subprocess.run(
                [soffice, "--headless", "--convert-to", conversion, "--outdir", str(output_dir), str(conversion_source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                check=False,
            )
            converted = next(output_dir.glob(f"*{expected_suffix}"), None)
            if completed.returncode == 0 and converted and converted.stat().st_size:
                if expected_suffix == ".txt":
                    return read_text_file(converted), ["libreoffice_doc"]
                text, _ = extract_ooxml(converted)
                return text, ["libreoffice_xls"]

    textutil = shutil.which("textutil")
    if textutil and path.suffix.lower() == ".doc":
        completed = subprocess.run(
            [textutil, "-convert", "txt", "-stdout", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout, ["textutil"]
    raise MaterialError(f"unsupported legacy Office file: {path.suffix.lower()}")


def decoded_member_name(member: zipfile.ZipInfo) -> str:
    """Repair archives created with UTF-8 names but without the UTF-8 flag."""

    name = member.filename
    if member.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def safe_archive_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    selected: list[tuple[zipfile.ZipInfo, str]] = []
    total_size = 0
    for member in archive.infolist():
        decoded_name = decoded_member_name(member)
        parts = Path(decoded_name).parts
        if member.is_dir() or not parts or "__MACOSX" in parts or Path(decoded_name).name.lower() in IGNORED_NAMES:
            continue
        if Path(decoded_name).is_absolute() or ".." in parts:
            raise MaterialError("unsafe archive path")
        total_size += member.file_size
        selected.append((member, decoded_name))
        if len(selected) > MAX_ARCHIVE_FILES or total_size > MAX_ARCHIVE_BYTES:
            raise MaterialError("archive limit exceeded")
    return selected


def extract_archive(path: Path, depth: int) -> tuple[str, list[str], list[str], list[dict[str, object]]]:
    if depth >= 2:
        raise MaterialError("nested archive depth exceeded")
    parts: list[str] = []
    failures: list[str] = []
    children: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="odm-archive-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(path) as archive:
            members = safe_archive_members(archive)
            for member, decoded_name in members:
                target = root / decoded_name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                result = extract_material(target, depth + 1, display_name=decoded_name)
                children.append({key: value for key, value in asdict(result).items() if key != "text"})
                if result.status not in {"complete", "ignored"}:
                    failures.append(decoded_name)
                if result.text.strip():
                    parts.append(f"--- {decoded_name} ---\n{result.text}")
    return "\n\n".join(parts), ["zip"], failures, children


def extract_material(path: Path, depth: int = 0, display_name: str | None = None) -> ExtractionResult:
    name = display_name or path.name
    suffix = path.suffix.lower()
    methods: list[str] = []
    failures: list[str] = []
    children: list[dict[str, object]] = []
    pages: int | None = None
    text = ""
    ignored = False
    try:
        if path.stat().st_size == 0 and suffix == ".larkcache":
            ignored = True
            methods = ["empty_cache_ignored"]
        elif suffix in TEXT_SUFFIXES:
            text, methods = read_text_file(path), ["text"]
        elif suffix in OOXML_SUFFIXES:
            if is_ole_file(path):
                text, methods = extract_legacy_office(path)
            else:
                text, methods = extract_ooxml(path)
        elif suffix == ".pdf":
            text, pages, methods, failures = extract_pdf(path)
        elif suffix in IMAGE_SUFFIXES:
            text, methods = extract_image(path)
        elif suffix in {".doc", ".xls"}:
            text, methods = extract_legacy_office(path)
        elif suffix == ".larkcache" and ".m4a.larkcache" in path.name.lower():
            raise MaterialError("audio transcription unavailable")
        elif suffix == ".zip":
            text, methods, failures, children = extract_archive(path, depth)
        else:
            raise MaterialError(f"unsupported file type: {suffix or '<none>'}")
    except Exception as exc:
        failures.append(type(exc).__name__ + ":" + str(exc)[:160])

    status = "ignored" if ignored else ("complete" if not failures else ("partial" if text.strip() else "failed"))
    return ExtractionResult(
        file_name=name,
        sha256=sha256_file(path),
        size=path.stat().st_size,
        status=status,
        text_chars=normalized_char_count(text),
        page_count=pages,
        methods=methods,
        failed_units=failures,
        children=children,
        text=text,
    )


def iter_material_files(directory: Path) -> Iterable[Path]:
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and not path.name.startswith(".") and path.name.lower() not in IGNORED_NAMES:
            yield path


def write_outputs(results: list[ExtractionResult], output_dir: Path, manifest_path: Path, corpus_path: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    corpus_parts: list[str] = []
    for index, result in enumerate(results, 1):
        text_path = output_dir / f"{index:03d}.txt"
        text_path.write_text(result.text, encoding="utf-8")
        corpus_parts.append(f"=== {result.file_name} ===\n{result.text}")

    items = [{key: value for key, value in asdict(result).items() if key != "text"} for result in results]
    manifest = {
        "schema_version": "1.0",
        "items": items,
        "summary": {
            "total": len(results),
            "complete": sum(item.status == "complete" for item in results),
            "partial": sum(item.status == "partial" for item in results),
            "failed": sum(item.status == "failed" for item in results),
            "ignored": sum(item.status == "ignored" for item in results),
            "text_chars": sum(item.text_chars for item in results),
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    corpus_path.write_text("\n\n".join(corpus_parts), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract all downloaded dispute materials.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--input-dir", type=Path, required=True)
    extract.add_argument("--output-dir", type=Path, required=True)
    extract.add_argument("--manifest", type=Path, required=True)
    extract.add_argument("--corpus", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = args.input_dir.resolve(strict=True)
        if not source.is_dir():
            raise MaterialError("input directory is not a directory")
        results = [extract_material(path) for path in iter_material_files(source)]
        if not results:
            raise MaterialError("no attachment files found")
        manifest = write_outputs(results, args.output_dir, args.manifest, args.corpus)
        print(json.dumps(manifest["summary"], ensure_ascii=False, separators=(",", ":")))
        return 0
    except (MaterialError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "failed", "error_code": "MATERIAL_EXTRACTION_FAILED", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

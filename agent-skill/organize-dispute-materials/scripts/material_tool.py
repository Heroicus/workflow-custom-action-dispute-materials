#!/usr/bin/env python3
"""Extract complete, source-only text from dispute-material attachments."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence
import xml.etree.ElementTree as ET


TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".wma", ".amr"}
OOXML_SUFFIXES = {".docx", ".xlsx", ".pptx"}
IGNORED_NAMES = {".ds_store", "thumbs.db"}
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MIN_TEXT_CHARS_PER_PDF_PAGE = 24
PDF_OCR_DPI = 300
VISION_TASK_SCHEMA = "vision-task/v1"
AUDIO_TASK_SCHEMA = "audio-task/v1"


class MaterialError(Exception):
    """A stable material extraction failure."""


@dataclass(frozen=True)
class OCRResult:
    text: str
    mean_confidence: float | None


class VisionCollector:
    """Persist every image that needs independent multimodal verification."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.tasks: list[dict[str, object]] = []

    def add(
        self,
        image: Path,
        *,
        source_file: str,
        source_sha256: str,
        unit: str,
        page: int | None,
        reason: str,
        ocr: OCRResult,
    ) -> str:
        image_hash = sha256_file(image)
        identity = f"{source_file}\0{source_sha256}\0{unit}\0{page or 0}\0{image_hash}"
        task_id = "vis_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        suffix = image.suffix.lower() if image.suffix.lower() in IMAGE_SUFFIXES else ".png"
        destination = self.directory / f"{task_id}{suffix}"
        if not destination.exists():
            shutil.copyfile(image, destination)
        self.tasks.append({
            "schema_version": VISION_TASK_SCHEMA,
            "task_id": task_id,
            "source_file": source_file,
            "source_sha256": source_sha256,
            "unit": unit,
            "page": page,
            "reason": reason,
            "image_path": str(destination),
            "image_sha256": image_hash,
            "ocr_text": ocr.text,
            "ocr_mean_confidence": ocr.mean_confidence,
        })
        return task_id

    def write(self, path: Path) -> dict[str, object]:
        task_ids = [str(task["task_id"]) for task in self.tasks]
        duplicates = sorted(task_id for task_id, count in Counter(task_ids).items() if count > 1)
        if duplicates:
            raise MaterialError(f"duplicate vision task id: {duplicates[0]}")
        tasks = sorted(self.tasks, key=lambda task: str(task["task_id"]))
        manifest = {
            "schema_version": VISION_TASK_SCHEMA,
            "policy": {
                "worker": "纠纷材料视觉核验员",
                "required_model": "Doubao-Seed-2.1-turbo",
                "write_scope": "read_only_evidence",
                "all_visual_units_required": True,
            },
            "tasks": tasks,
            "summary": {"total": len(tasks), "pending": len(tasks)},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest


def material_suffix(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".m4a.larkcache"):
        return ".m4a"
    return path.suffix.lower()


class AudioCollector:
    """Persist every audio attachment for Feishu Minutes transcription."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.tasks: list[dict[str, object]] = []

    def add(self, media: Path, *, source_file: str, source_sha256: str) -> str:
        media_hash = sha256_file(media)
        suffix = material_suffix(media)
        identity = f"{source_file}\0{source_sha256}\0{media_hash}\0{media.stat().st_size}"
        task_id = "aud_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        destination = self.directory / f"{task_id}{suffix}"
        if not destination.exists():
            shutil.copyfile(media, destination)
        self.tasks.append({
            "schema_version": AUDIO_TASK_SCHEMA,
            "task_id": task_id,
            "source_file": source_file,
            "source_sha256": source_sha256,
            "media_path": str(destination),
            "media_sha256": media_hash,
            "media_suffix": suffix,
            "size_bytes": media.stat().st_size,
        })
        return task_id

    def write(self, path: Path) -> dict[str, object]:
        task_ids = [str(task["task_id"]) for task in self.tasks]
        duplicates = sorted(task_id for task_id, count in Counter(task_ids).items() if count > 1)
        if duplicates:
            raise MaterialError(f"duplicate audio task id: {duplicates[0]}")
        tasks = sorted(self.tasks, key=lambda task: str(task["task_id"]))
        manifest = {
            "schema_version": AUDIO_TASK_SCHEMA,
            "policy": {
                "transcriber": "Feishu Minutes",
                "identity": "user",
                "result_schema": "audio-evidence/v1",
                "write_scope": "transcript_only",
                "all_audio_units_required": True,
            },
            "tasks": tasks,
            "summary": {"total": len(tasks), "pending": len(tasks)},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest


@dataclass
class ExtractionResult:
    attachment_id: str
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


def extract_ooxml(
    path: Path,
    collector: VisionCollector | None = None,
    display_name: str | None = None,
    source_sha256: str | None = None,
) -> tuple[str, list[str]]:
    suffix = path.suffix.lower()
    prefixes = {
        ".docx": ("word/document.xml", "word/header", "word/footer", "word/footnotes.xml", "word/endnotes.xml"),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/", "xl/comments"),
        ".pptx": ("ppt/slides/", "ppt/notesSlides/"),
    }[suffix]
    parts: list[str] = []
    methods = [suffix.lstrip(".") + "_xml"]
    with zipfile.ZipFile(path) as archive:
        for member, name in sorted(safe_archive_members(archive), key=lambda item: item[1]):
            if name.endswith(".xml") and any(name.startswith(prefix) for prefix in prefixes):
                text = xml_text(archive.read(member))
                if text.strip():
                    parts.append(f"[{name}]\n{text}")
                continue
            if Path(name).suffix.lower() not in IMAGE_SUFFIXES or "/media/" not in name:
                continue
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(suffix=Path(name).suffix, delete=False) as handle:
                    temporary_name = handle.name
                    handle.write(archive.read(member))
                try:
                    ocr = run_tesseract(Path(temporary_name))
                except MaterialError:
                    ocr = OCRResult(text="", mean_confidence=None)
                    if "embedded_ocr_unavailable" not in methods:
                        methods.append("embedded_ocr_unavailable")
                if ocr.text and "embedded_ocr_hint" not in methods:
                    methods.append("embedded_ocr_hint")
                if collector:
                    collector.add(
                        Path(temporary_name),
                        source_file=f"{display_name or path.name}::{name}",
                        source_sha256=source_sha256 or sha256_file(path),
                        unit=f"embedded:{name}",
                        page=None,
                        reason="embedded_image",
                        ocr=ocr,
                    )
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
    return "\n\n".join(parts), methods


def is_ole_file(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(8) == bytes.fromhex("d0cf11e0a1b11ae1")


def run_tesseract(image: Path, timeout: int = 120) -> OCRResult:
    executable = shutil.which("tesseract")
    if not executable:
        raise MaterialError("tesseract unavailable")
    languages = "chi_sim+eng"
    completed = subprocess.run(
        [executable, str(image), "stdout", "-l", languages, "--psm", "6", "tsv"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise MaterialError(completed.stderr.strip() or f"tesseract exited {completed.returncode}")
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    weighted_confidence = 0.0
    confidence_weight = 0
    try:
        rows = csv.DictReader(io.StringIO(completed.stdout), delimiter="\t")
        for row in rows:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            key = tuple(str(row.get(name) or "0") for name in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append(text)
            try:
                confidence = float(str(row.get("conf") or "-1"))
            except ValueError:
                confidence = -1
            weight = max(1, normalized_char_count(text))
            if confidence >= 0:
                weighted_confidence += confidence * weight
                confidence_weight += weight
    except csv.Error as exc:
        raise MaterialError(f"invalid tesseract TSV: {exc}") from exc
    text = "\n".join(" ".join(words) for words in lines.values()).strip()
    mean_confidence = round(weighted_confidence / confidence_weight, 2) if confidence_weight else None
    return OCRResult(text=text, mean_confidence=mean_confidence)


def extract_image(
    path: Path,
    collector: VisionCollector | None = None,
    display_name: str | None = None,
    source_sha256: str | None = None,
) -> tuple[str, list[str]]:
    try:
        ocr = run_tesseract(path)
        methods = ["ocr", "vision_required"]
    except MaterialError:
        ocr = OCRResult(text="", mean_confidence=None)
        methods = ["ocr_unavailable", "vision_required"]
    if collector:
        collector.add(
            path,
            source_file=display_name or path.name,
            source_sha256=source_sha256 or sha256_file(path),
            unit="image",
            page=None,
            reason="image_attachment",
            ocr=ocr,
        )
    # OCR only helps the visual worker locate text.  It is not evidentiary
    # source text and is therefore excluded from the corpus.
    return "", methods


def extract_audio(
    path: Path,
    collector: AudioCollector | None = None,
    display_name: str | None = None,
    source_sha256: str | None = None,
) -> tuple[str, list[str]]:
    if collector:
        collector.add(
            path,
            source_file=display_name or path.name,
            source_sha256=source_sha256 or sha256_file(path),
        )
    return "", ["feishu_minutes_transcription_required"]


def has_page_image(page: object) -> bool:
    try:
        return bool(page.get_images(full=True))  # type: ignore[attr-defined]
    except Exception:
        return False


def extract_pdf(
    path: Path,
    collector: VisionCollector | None = None,
    display_name: str | None = None,
    source_sha256: str | None = None,
) -> tuple[str, int, list[str], list[str]]:
    try:
        import fitz  # type: ignore
    except ImportError:
        return extract_pdf_cli(path, collector, display_name, source_sha256)

    document = fitz.open(str(path))
    pages: list[str] = []
    methods: set[str] = set()
    failures: list[str] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            text = page.get_text("text").strip()
            has_native_text = normalized_char_count(text) >= MIN_TEXT_CHARS_PER_PDF_PAGE
            page_image = has_page_image(page)
            if has_native_text and not page_image:
                methods.add("pdf_text")
            else:
                temporary_name = ""
                try:
                    pixmap = page.get_pixmap(dpi=PDF_OCR_DPI, alpha=False)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                        temporary_name = handle.name
                    pixmap.save(temporary_name)
                    try:
                        ocr = run_tesseract(Path(temporary_name))
                        methods.add("ocr")
                    except MaterialError:
                        ocr = OCRResult(text="", mean_confidence=None)
                        methods.add("ocr_unavailable")
                    if has_native_text:
                        methods.add("pdf_text")
                    elif ocr.text:
                        methods.add("ocr_hint_only")
                    if collector:
                        collector.add(
                            Path(temporary_name),
                            source_file=display_name or path.name,
                            source_sha256=source_sha256 or sha256_file(path),
                            unit=f"page:{page_index + 1}",
                            page=page_index + 1,
                            reason="pdf_page_visual_content" if page_image else "pdf_page_without_text_layer",
                            ocr=ocr,
                        )
                    methods.add("vision_required")
                except Exception as exc:  # page-level failure remains auditable
                    failures.append(f"page:{page_index + 1}:{type(exc).__name__}")
                finally:
                    if temporary_name:
                        Path(temporary_name).unlink(missing_ok=True)
            pages.append(f"[第{page_index + 1}页]\n{text}" if text else f"[第{page_index + 1}页]")
    finally:
        document.close()
    return "\n\n".join(pages), len(pages), sorted(methods), failures


def extract_pdf_cli(
    path: Path,
    collector: VisionCollector | None = None,
    display_name: str | None = None,
    source_sha256: str | None = None,
) -> tuple[str, int, list[str], list[str]]:
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
                    [pdftoppm, "-f", str(page_number), "-l", str(page_number), "-r", str(PDF_OCR_DPI), "-png", "-singlefile", str(path), str(prefix)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False,
                )
                image = prefix.with_suffix(".png")
                try:
                    if rendered.returncode != 0 or not image.is_file():
                        raise MaterialError(rendered.stderr.strip() or "pdftoppm failed")
                    try:
                        ocr = run_tesseract(image)
                        methods.add("ocr")
                    except MaterialError:
                        ocr = OCRResult(text="", mean_confidence=None)
                        methods.add("ocr_unavailable")
                    if ocr.text:
                        methods.add("ocr_hint_only")
                    if collector:
                        collector.add(
                            image,
                            source_file=display_name or path.name,
                            source_sha256=source_sha256 or sha256_file(path),
                            unit=f"page:{page_number}",
                            page=page_number,
                            reason="pdf_page_without_text_layer",
                            ocr=ocr,
                        )
                    methods.add("vision_required")
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
        if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise MaterialError(f"archive member too large: {decoded_name}")
        compressed = max(1, member.compress_size)
        if member.file_size > 1024 * 1024 and member.file_size / compressed > MAX_COMPRESSION_RATIO:
            raise MaterialError(f"archive compression ratio too high: {decoded_name}")
        total_size += member.file_size
        selected.append((member, decoded_name))
        if len(selected) > MAX_ARCHIVE_FILES or total_size > MAX_ARCHIVE_BYTES:
            raise MaterialError("archive limit exceeded")
    return selected


def extract_archive(
    path: Path,
    depth: int,
    collector: VisionCollector | None = None,
    audio_collector: AudioCollector | None = None,
    display_name: str | None = None,
) -> tuple[str, list[str], list[str], list[dict[str, object]]]:
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
                child_name = f"{display_name or path.name}::{decoded_name}"
                result = extract_material(
                    target, depth=depth + 1, display_name=child_name,
                    collector=collector, audio_collector=audio_collector,
                )
                children.append({key: value for key, value in asdict(result).items() if key != "text"})
                if result.status not in {"complete", "ignored"}:
                    failures.append(child_name)
                if result.text.strip():
                    parts.append(f"--- {child_name} ---\n{result.text}")
    return "\n\n".join(parts), ["zip"], failures, children


def extract_material(
    path: Path,
    attachment_id: str = "",
    depth: int = 0,
    display_name: str | None = None,
    collector: VisionCollector | None = None,
    audio_collector: AudioCollector | None = None,
) -> ExtractionResult:
    name = display_name or path.name
    suffix = material_suffix(path)
    methods: list[str] = []
    failures: list[str] = []
    children: list[dict[str, object]] = []
    pages: int | None = None
    text = ""
    ignored = False
    source_sha256 = sha256_file(path)
    try:
        if path.stat().st_size == 0 and path.suffix.lower() == ".larkcache":
            ignored = True
            methods = ["empty_cache_ignored"]
        elif suffix in TEXT_SUFFIXES:
            text, methods = read_text_file(path), ["text"]
        elif suffix in OOXML_SUFFIXES:
            if is_ole_file(path):
                text, methods = extract_legacy_office(path)
            else:
                text, methods = extract_ooxml(path, collector, name, source_sha256)
        elif suffix == ".pdf":
            text, pages, methods, failures = extract_pdf(path, collector, name, source_sha256)
        elif suffix in IMAGE_SUFFIXES:
            text, methods = extract_image(path, collector, name, source_sha256)
        elif suffix in AUDIO_SUFFIXES:
            text, methods = extract_audio(path, audio_collector, name, source_sha256)
        elif suffix in {".doc", ".xls"}:
            text, methods = extract_legacy_office(path)
        elif suffix == ".zip":
            text, methods, failures, children = extract_archive(path, depth, collector, audio_collector, name)
        else:
            raise MaterialError(f"unsupported file type: {suffix or '<none>'}")
    except Exception as exc:
        failures.append(type(exc).__name__ + ":" + str(exc)[:160])

    status = "ignored" if ignored else ("complete" if not failures else ("partial" if text.strip() else "failed"))
    return ExtractionResult(
        attachment_id=attachment_id,
        file_name=name,
        sha256=source_sha256,
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


def write_outputs(
    results: list[ExtractionResult], output_dir: Path, manifest_path: Path, corpus_path: Path,
    vision_tasks: dict[str, object], audio_tasks: dict[str, object],
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    corpus_parts: list[str] = []
    for index, result in enumerate(results, 1):
        text_path = output_dir / f"{index:03d}.txt"
        text_path.write_text(result.text, encoding="utf-8")
        corpus_parts.append(f"=== 材料[sha256={result.sha256}] {result.file_name} ===\n{result.text}")

    corpus = "\n\n".join(corpus_parts)
    items = [{key: value for key, value in asdict(result).items() if key != "text"} for result in results]
    manifest = {
        "schema_version": "2.0",
        "items": items,
        "artifacts": {
            "source_corpus_sha256": hashlib.sha256(corpus.encode("utf-8")).hexdigest(),
            "text_outputs": [
                {"path": f"{index:03d}.txt", "sha256": sha256_file(output_dir / f"{index:03d}.txt")}
                for index in range(1, len(results) + 1)
            ],
            "vision_task_ids": sorted(
                str(item.get("task_id")) for item in vision_tasks.get("tasks", []) if isinstance(item, dict)
            ),
            "audio_task_ids": sorted(
                str(item.get("task_id")) for item in audio_tasks.get("tasks", []) if isinstance(item, dict)
            ),
        },
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
    corpus_path.write_text(corpus, encoding="utf-8")
    return manifest


def read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterialError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise MaterialError(f"invalid {label}: root must be object")
    return value


def verified_downloads(runtime_path: Path, receipt_path: Path, input_dir: Path) -> dict[Path, str]:
    """Bind every local input file to the exact Base attachment token."""

    runtime = read_object(runtime_path, "runtime")
    record_id = str(runtime.get("record_id") or "").strip()
    expected = runtime.get("attachment_ids")
    if not re.fullmatch(r"rec[A-Za-z0-9_-]{1,125}", record_id) or not isinstance(expected, list):
        raise MaterialError("runtime record_id or attachment_ids is invalid")
    expected_ids = sorted({str(item).strip() for item in expected if str(item).strip()})
    if len(expected_ids) != len(expected) or not expected_ids:
        raise MaterialError("runtime attachment_ids must be unique and non-empty")

    receipt = read_object(receipt_path, "attachment download receipt")
    data = receipt.get("data")
    downloaded = data.get("downloaded") if isinstance(data, dict) else None
    if receipt.get("ok") is not True or receipt.get("identity") != "user" or not isinstance(downloaded, list):
        raise MaterialError("attachment download receipt is not a successful user readback")
    root = input_dir.resolve(strict=True)
    mapping: dict[Path, str] = {}
    received_ids: list[str] = []
    for item in downloaded:
        if not isinstance(item, dict):
            raise MaterialError("attachment download item is not an object")
        token = str(item.get("file_token") or "").strip()
        saved = Path(str(item.get("saved_path") or "")).resolve(strict=True)
        try:
            saved.relative_to(root)
        except ValueError as exc:
            raise MaterialError("downloaded attachment escaped the job input directory") from exc
        if str(item.get("record_id") or "") != record_id or str(item.get("field_id") or "") != "fldOz2CYX4":
            raise MaterialError("downloaded attachment is not bound to the target record field")
        try:
            receipt_size = int(item.get("size_bytes") or -1)
        except (TypeError, ValueError) as exc:
            raise MaterialError("downloaded attachment size is invalid") from exc
        if not saved.is_file() or receipt_size != saved.stat().st_size:
            raise MaterialError("downloaded attachment size/readback mismatch")
        if saved in mapping:
            raise MaterialError("duplicate downloaded attachment path")
        mapping[saved] = token
        received_ids.append(token)
    actual_files = {path.resolve() for path in iter_material_files(root)}
    if sorted(received_ids) != expected_ids or set(mapping) != actual_files:
        raise MaterialError("downloaded attachment tokens or files do not match the runtime envelope")
    return mapping


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract all downloaded dispute materials.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--input-dir", type=Path, required=True)
    extract.add_argument("--output-dir", type=Path, required=True)
    extract.add_argument("--manifest", type=Path, required=True)
    extract.add_argument("--corpus", type=Path, required=True)
    extract.add_argument("--vision-dir", type=Path, required=True)
    extract.add_argument("--vision-tasks", type=Path, required=True)
    extract.add_argument("--audio-dir", type=Path, required=True)
    extract.add_argument("--audio-tasks", type=Path, required=True)
    extract.add_argument("--runtime", type=Path, required=True)
    extract.add_argument("--download-receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = args.input_dir.resolve(strict=True)
        if not source.is_dir():
            raise MaterialError("input directory is not a directory")
        collector = VisionCollector(args.vision_dir)
        audio_collector = AudioCollector(args.audio_dir)
        downloads = verified_downloads(args.runtime, args.download_receipt, source)
        results = [
            extract_material(
                path, attachment_id=downloads[path.resolve()],
                collector=collector, audio_collector=audio_collector,
            )
            for path in iter_material_files(source)
        ]
        if not results:
            raise MaterialError("no attachment files found")
        vision = collector.write(args.vision_tasks)
        audio = audio_collector.write(args.audio_tasks)
        manifest = write_outputs(results, args.output_dir, args.manifest, args.corpus, vision, audio)
        output = dict(manifest["summary"])
        output["vision_tasks"] = vision["summary"]["total"]
        output["audio_tasks"] = audio["summary"]["total"]
        output["pdf_ocr_dpi"] = PDF_OCR_DPI
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (MaterialError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "failed", "error_code": "MATERIAL_EXTRACTION_FAILED", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

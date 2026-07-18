#!/usr/bin/env python3
"""PGM değişiklik raporundaki ilan eklerinden sınav konu listesi taslağı çıkarır.

Çıktı yalnızca inceleme amacıyla ``review/candidate-scope.json`` dosyasına
yazılır. Yeterli güven oluşmazsa mevcut aday dosyası değiştirilmez.
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import zipfile


ALLOWED_HOST_SUFFIXES = ("adalet.gov.tr",)
DOCUMENT_SUFFIXES = (".pdf", ".docx")
COMMON_MARKER = re.compile(r"\bortak\s+(?:sinav\s+)?konular", re.IGNORECASE)
DUTY_MARKER = re.compile(r"\b(?:gorev|görev)\s+(?:alani|alanı|konular)", re.IGNORECASE)
ROW_NUMBER = re.compile(r"^\s*(?:\d{1,2}[.)-]|[a-zçğıöşü][.)])\s+", re.IGNORECASE)
NOISE = re.compile(r"^(?:sayfa\s+\d+|ek[- ]?\d+|sira\s*no|sıra\s*no|konu\s+basligi|konu\s+başlığı|soru\s+sayisi|soru\s+sayısı)$", re.IGNORECASE)


def fold(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().translate(str.maketrans("çğıöşü", "cgiosu"))).strip()


def allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def fetch_bytes(url: str, maximum: int = 30 * 1024 * 1024) -> tuple[bytes, str]:
    if not allowed_url(url):
        raise ValueError(f"İzin verilmeyen kaynak adresi: {url}")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 YIMAkademiScopeExtractor/1.0", "Accept-Language": "tr-TR,tr;q=0.9"})
    with urlopen(request, timeout=45) as response:
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("İndirilen resmî ek izin verilen boyutu aştı.")
        return data, response.headers.get_content_type()


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, str]] = []
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self.href is not None:
            self.items.append((" ".join(self.text).strip(), self.href))
            self.href, self.text = None, []


def report_urls(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls = re.findall(r"https://[^\s)>]+", path.read_text(encoding="utf-8"))
    return list(dict.fromkeys(url.rstrip(".,") for url in urls if allowed_url(url.rstrip(".,"))))


def document_links(announcement_url: str) -> list[tuple[str, str]]:
    data, content_type = fetch_bytes(announcement_url)
    suffix = Path(urlparse(announcement_url).path).suffix.casefold()
    if suffix in DOCUMENT_SUFFIXES:
        return [(announcement_url, announcement_url)]
    if "html" not in content_type:
        return []
    parser = Links()
    parser.feed(data.decode("utf-8", errors="replace"))
    ranked: list[tuple[int, str, str]] = []
    for title, href in parser.items:
        absolute = urljoin(announcement_url, href)
        suffix = Path(urlparse(absolute).path).suffix.casefold()
        if suffix not in DOCUMENT_SUFFIXES or not allowed_url(absolute):
            continue
        searchable = fold(title + " " + absolute)
        score = sum(word in searchable for word in ("konu", "yazi isleri", "mudur", "ek 2", "gorevde yukselme"))
        ranked.append((score, title or Path(urlparse(absolute).path).name, absolute))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [(title, url) for _, title, url in ranked]


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError("PDF okumak için pypdf kurulmalıdır.") from error
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as error:
            raise ValueError("Şifreli PDF okunamadı.") from error
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def extract_text(url: str) -> str:
    data, _ = fetch_bytes(url)
    suffix = Path(urlparse(url).path).suffix.casefold()
    return pdf_text(data) if suffix == ".pdf" else docx_text(data)


def clean_lines(text: str) -> list[str]:
    result: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip(" \t|•·")
        if not line or NOISE.fullmatch(line):
            continue
        result.append(line)
    return result


def title_from_line(line: str) -> str | None:
    numbered = bool(ROW_NUMBER.match(line))
    title = ROW_NUMBER.sub("", line).strip(" :-–—")
    if not numbered or len(title) < 5 or len(title) > 240:
        return None
    if re.fullmatch(r"\d+", title) or NOISE.fullmatch(title):
        return None
    return title


def parse_scope(text: str) -> tuple[list[str], list[str], list[str]]:
    lines = clean_lines(text)
    common: list[str] = []
    duty: list[str] = []
    notes: list[str] = []
    group: str | None = None
    for line in lines:
        folded = fold(line)
        if COMMON_MARKER.search(folded):
            group = "common"
            continue
        if DUTY_MARKER.search(folded):
            group = "duty"
            continue
        if group is None:
            continue
        title = title_from_line(line)
        if not title:
            continue
        target = common if group == "common" else duty
        if fold(title) not in {fold(existing) for existing in target}:
            target.append(title)
    if not common:
        notes.append("Ortak konular bölümü güvenle çıkarılamadı.")
    if not duty:
        notes.append("Görev konuları bölümü güvenle çıkarılamadı.")
    return common, duty, notes


def write_report(path: Path, attempts: list[str], selected: str, common: list[str], duty: list[str], notes: list[str], ready: bool) -> None:
    lines = ["# PGM konu listesi çıkarma raporu", "", f"- Durum: **{'İNCELEMEYE HAZIR' if ready else 'OTOMATİK ÇIKARIM YAPILAMADI'}**", f"- Seçilen resmî ek: {selected or 'Yok'}", f"- Ortak konu sayısı: {len(common)}", f"- Görev konusu sayısı: {len(duty)}", "", "## Denenen ekler", ""]
    lines.extend(f"- {item}" for item in attempts or ["Belge bağlantısı bulunamadı."])
    lines.extend(["", "## Uyarılar", ""])
    lines.extend(f"- {item}" for item in notes or ["Yok."])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_output(name: str, value: str) -> None:
    output = Path(sys.argv[0]).parent
    github_output = __import__("os").environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--change-report", type=Path, default=Path(".monitor/change-report.md"))
    parser.add_argument("--output", type=Path, default=Path("review/candidate-scope.json"))
    parser.add_argument("--report", type=Path, default=Path(".monitor/scope-extraction-report.md"))
    args = parser.parse_args()

    announcements = report_urls(args.change_report)
    attempts: list[str] = []
    selected = ""
    best_common: list[str] = []
    best_duty: list[str] = []
    notes: list[str] = []
    for announcement in announcements:
        try:
            documents = document_links(announcement)
        except Exception as error:
            notes.append(f"İlan açılamadı: {announcement} — {error}")
            continue
        for title, document_url in documents:
            attempts.append(f"{title}: {document_url}")
            try:
                common, duty, parse_notes = parse_scope(extract_text(document_url))
            except Exception as error:
                notes.append(f"Ek okunamadı: {document_url} — {error}")
                continue
            if len(common) + len(duty) > len(best_common) + len(best_duty):
                selected, best_common, best_duty = document_url, common, duty
                notes.extend(parse_notes)

    ready = len(best_common) >= 5 and len(best_duty) >= 5 and len(best_common) + len(best_duty) >= 15
    write_report(args.report, attempts, selected, best_common, best_duty, notes, ready)
    if ready:
        payload = {
            "schemaVersion": 1,
            "examId": "pgm-aday-sinav-kapsami",
            "announcementUrl": announcements[0] if announcements else "",
            "sourceDocumentUrl": selected,
            "requiresApproval": True,
            "commonTopics": best_common,
            "dutyTopics": best_duty,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    set_output("ready", str(ready).lower())
    set_output("common_count", str(len(best_common)))
    set_output("duty_count", str(len(best_duty)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

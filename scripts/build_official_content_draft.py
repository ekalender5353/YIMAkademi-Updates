#!/usr/bin/env python3
"""Onay bekleyen resmî YİM Akademi içerik paketi taslağı üretir.

Mevcut doğrulanmış konu içeriğini korur. Yeni konu yalnız güvenilir bir resmî
kaynakla kesin eşleşirse resmî PDF/DOCX metninden oluşturulur. Çıktı hiçbir
zaman ``updates`` klasörüne yazılmaz ve otomatik yayımlanmaz.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Yardımcı modül yüklenemedi: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare_module = load_module("yim_compare_scope", SCRIPT_DIR / "compare_exam_scope.py")
extract_module = load_module("yim_extract_scope", SCRIPT_DIR / "extract_pgm_scope.py")

OFFICIAL_HOSTS = ("adalet.gov.tr", "anayasa.gov.tr", "atam.gov.tr", "cbiko.gov.tr", "mevzuat.gov.tr", "tdk.gov.tr")
LAW_NUMBER = re.compile(r"\b(\d{3,5})\s+say[ıi]l[ıi]\b", re.IGNORECASE)
ARTICLE = re.compile(r"(?im)^\s*(?:MADDE|Madde)\s+(\d+[A-Z]?)\s*[-–—]?")
INLINE_MARKER = re.compile(r"(?<!\n)(?<!\w)\s+((?:[a-zçğıöşü]\)|\(\d{1,3}\)))\s+", re.IGNORECASE)


def fold(value: str) -> str:
    return compare_module.fold(value)


def trusted(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    return parsed.scheme == "https" and any(host == suffix or host.endswith("." + suffix) for suffix in OFFICIAL_HOSTS)


def normalize_body(value: str) -> str:
    value = value.replace("\r", "\n").replace("\u00ad", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = INLINE_MARKER.sub(lambda match: "\n" + match.group(1) + " ", value)
    return value.strip()


def split_articles(text: str, source_title: str) -> list[dict]:
    text = normalize_body(text)
    matches = list(ARTICLE.finditer(text))
    sections: list[dict] = []
    if not matches:
        if len(text) < 300:
            raise ValueError("Resmî metin içerik oluşturmak için çok kısa.")
        return [{"id": "resmi-metin", "title": source_title, "scope": "Resmî kaynaktan alınan metin", "body": text, "examNote": "", "sourceIds": []}]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start():end].strip()
        if len(body) < 20:
            continue
        article_number = match.group(1)
        sections.append({"id": f"madde-{article_number.casefold()}", "title": f"{source_title} — Madde {article_number}", "scope": "Yürürlükteki resmî konsolide metin", "body": body, "examNote": "", "sourceIds": []})
    if not sections:
        raise ValueError("Resmî metinde kullanılabilir madde bulunamadı.")
    return sections


def source_score(candidate_title: str, source: dict) -> float:
    title = str(source.get("title", ""))
    candidate_numbers = set(LAW_NUMBER.findall(candidate_title))
    source_numbers = set(LAW_NUMBER.findall(title))
    if candidate_numbers and candidate_numbers & source_numbers:
        return 1.0
    return compare_module.similarity(candidate_title, title)


def find_source(candidate_title: str, sources: list[dict]) -> tuple[dict | None, float]:
    ranked = sorted(((source_score(candidate_title, source), source) for source in sources if trusted(str(source.get("url", "")))), key=lambda item: item[0], reverse=True)
    return (ranked[0][1], ranked[0][0]) if ranked else (None, 0.0)


def source_text(source: dict) -> str:
    url = str(source.get("url", ""))
    suffix = Path(urlparse(url).path).suffix.casefold()
    if suffix not in (".pdf", ".docx"):
        raise ValueError("Kaynak doğrudan PDF veya DOCX resmî metnine bağlı değil.")
    return extract_module.extract_text(url)


def new_topic_id(group: str, title: str, existing: set[str]) -> str:
    prefix = "ortak" if fold(group) == "ortak" else "gorev"
    digest = hashlib.sha256(fold(title).encode("utf-8")).hexdigest()[:10]
    candidate = f"{prefix}-yeni-{digest}"
    if candidate in existing:
        raise ValueError(f"Yeni konu kimliği çakıştı: {candidate}")
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("current_pack", type=Path)
    parser.add_argument("candidate_scope", type=Path)
    parser.add_argument("--output", type=Path, default=Path("review/content-pack.draft.json"))
    parser.add_argument("--report", type=Path, default=Path(".quality/content-draft-report.md"))
    args = parser.parse_args()

    pack, current = compare_module.load_current(args.current_pack)
    candidate_data, candidates = compare_module.load_candidate(args.candidate_scope)
    matches, removed = compare_module.compare(current, candidates)
    topics_by_id = {topic["id"]: topic for topic in pack.get("topics", [])}
    sources = pack.get("sources", [])
    existing_ids = set(topics_by_id)
    draft_topics: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []

    common_ids: list[str] = []
    duty_ids: list[str] = []
    for match in matches:
        target_ids = common_ids if fold(match.candidate.group) == "ortak" else duty_ids
        if match.status in ("unchanged", "renamed") and match.current is not None:
            topic = deepcopy(topics_by_id[match.current.topic_id])
            if match.status == "renamed":
                warnings.append(f"Başlık değişikliği adayı: {topic['title']} -> {match.candidate.title}")
                topic["title"] = match.candidate.title
            draft_topics.append(topic)
            target_ids.append(topic["id"])
            continue
        if match.status == "ambiguous":
            errors.append(f"Belirsiz konu eşleşmesi: {match.candidate.title} / en yakın: {match.current.title if match.current else 'yok'} (%{match.score * 100:.1f})")
            continue

        source, score = find_source(match.candidate.title, sources)
        if source is None or score < 0.78:
            errors.append(f"Yeni konu için güvenilir resmî kaynak eşleşmedi: {match.candidate.title} (güven %{score * 100:.1f})")
            continue
        try:
            sections = split_articles(source_text(source), str(source.get("title", match.candidate.title)))
        except Exception as error:
            errors.append(f"Yeni konu metni hazırlanamadı: {match.candidate.title} — {error}")
            continue
        topic_id = new_topic_id(match.candidate.group, match.candidate.title, existing_ids)
        existing_ids.add(topic_id)
        for section in sections:
            section["sourceIds"] = [source["id"]]
        topic = {"id": topic_id, "group": match.candidate.group, "sortOrder": len(target_ids) + 1, "title": match.candidate.title, "shortTitle": match.candidate.title, "summary": f"{source['title']} resmî metni esas alınarak hazırlanan taslak içerik.", "contentKind": "OfficialTextDraft", "questionCount": 0, "tags": [], "sourceIds": [source["id"]], "sections": sections}
        draft_topics.append(topic)
        target_ids.append(topic_id)

    draft = deepcopy(pack)
    draft["packageVersion"] = str(pack.get("packageVersion", "0.0.0")) + "-draft"
    draft["publishedAtUtc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    draft["displayName"] = f"YİM Akademi — {candidate_data.get('examId', 'Yeni sınav')} İNCELEME TASLAĞI"
    draft["examProfile"]["id"] = candidate_data.get("examId", "pgm-aday-sinav-kapsami")
    draft["examProfile"]["announcementUrl"] = candidate_data.get("announcementUrl", "")
    draft["examProfile"]["commonTopicIds"] = common_ids
    draft["examProfile"]["dutyTopicIds"] = duty_ids
    draft["topics"] = draft_topics

    lines = ["# YİM Akademi resmî içerik taslağı raporu", "", f"- Mevcut paket: `{pack.get('packageVersion', '')}`", f"- Aday sınav: `{candidate_data.get('examId', '')}`", "- Otomatik yayın: **Kapalı**", f"- Taslak üretildi: **{'Hayır' if errors else 'Evet'}**", f"- Korunan/oluşturulan konu: {len(draft_topics)}", f"- Çıkarılmış olabilecek konu: {len(removed)}", "", "## Engelleyici hatalar", ""]
    lines.extend(f"- {item}" for item in errors or ["Yok."])
    lines.extend(["", "## İnceleme uyarıları", ""])
    lines.extend(f"- {item}" for item in warnings or ["Yok."])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if errors:
        print(json.dumps({"draftReady": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"draftReady": True, "topics": len(draft_topics), "warnings": warnings}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

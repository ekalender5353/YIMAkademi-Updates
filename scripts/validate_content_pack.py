#!/usr/bin/env python3
"""YİM Akademi içerik paketini yayımdan önce doğrular.

Bu denetleyici içerik üretmez ve paketi otomatik yayımlamaz. Yapısal veya
kaynak güvenliği açısından sorunlu paketlerde hata kodu döndürerek yayın
işlemini durdurur; editoryal inceleme gerektiren durumları uyarı olarak raporlar.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlparse


TRUSTED_OFFICIAL_HOSTS = {
    "adalet.gov.tr",
    "anayasa.gov.tr",
    "atam.gov.tr",
    "cbiko.gov.tr",
    "mevzuat.gov.tr",
    "tdk.gov.tr",
}
GROUPS = {"ortak", "görev", "gorev"}
LIST_MARKER = re.compile(r"(?<!\w)(?:[a-zçğıöşü]\)|\(\d{1,3}\))\s+", re.IGNORECASE)
SUSPICIOUS_INLINE_MARKER = re.compile(r"\S\s+(?:[a-zçğıöşü]\)|\(\d{1,3}\))\s+\S", re.IGNORECASE)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    statistics: dict[str, int] = field(default_factory=dict)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def normalized(value: object) -> str:
    return str(value or "").strip()


def official_host(host: str) -> bool:
    host = host.casefold().removeprefix("www.")
    return any(host == allowed or host.endswith("." + allowed) for allowed in TRUSTED_OFFICIAL_HOSTS)


def validate(path: Path) -> Report:
    report = Report()
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        report.error(f"Paket JSON olarak okunamadı: {error}")
        return report

    if pack.get("schemaVersion") != 1:
        report.error("schemaVersion değeri 1 olmalıdır.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", normalized(pack.get("packageVersion"))):
        report.error("packageVersion x.y.z biçiminde olmalıdır.")

    sources = pack.get("sources") or []
    topics = pack.get("topics") or []
    profile = pack.get("examProfile") or {}
    if not isinstance(sources, list) or not sources:
        report.error("En az bir resmî kaynak tanımlanmalıdır.")
        sources = []
    if not isinstance(topics, list) or not topics:
        report.error("Konu listesi boş olamaz.")
        topics = []

    source_ids: set[str] = set()
    for index, source in enumerate(sources, 1):
        source_id = normalized(source.get("id"))
        if not source_id or source_id.casefold() in source_ids:
            report.error(f"Kaynak {index}: boş veya yinelenen kaynak kimliği: {source_id!r}")
        source_ids.add(source_id.casefold())
        url = normalized(source.get("url"))
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            report.error(f"Kaynak {source_id}: yalnızca geçerli HTTPS adresi kullanılabilir.")
        elif not official_host(parsed.hostname):
            report.error(f"Kaynak {source_id}: resmî alan adı izin listesinde değil: {parsed.hostname}")
        for field_name in ("title", "authority", "relevantScope"):
            if not normalized(source.get(field_name)):
                report.error(f"Kaynak {source_id}: {field_name} alanı boş olamaz.")

    topic_ids: set[str] = set()
    section_count = 0
    inline_marker_count = 0
    empty_source_topics = 0
    duplicate_bodies: Counter[str] = Counter()
    for index, topic in enumerate(topics, 1):
        topic_id = normalized(topic.get("id"))
        folded_id = topic_id.casefold()
        if not topic_id or folded_id in topic_ids:
            report.error(f"Konu {index}: boş veya yinelenen konu kimliği: {topic_id!r}")
        topic_ids.add(folded_id)
        if normalized(topic.get("group")).casefold() not in GROUPS:
            report.error(f"Konu {topic_id}: grup yalnızca Ortak veya Görev olabilir.")
        if not normalized(topic.get("title")) or not normalized(topic.get("summary")):
            report.error(f"Konu {topic_id}: başlık ve özet boş olamaz.")

        topic_sources = topic.get("sourceIds") or []
        if not topic_sources:
            empty_source_topics += 1
        for source_id in topic_sources:
            if normalized(source_id).casefold() not in source_ids:
                report.error(f"Konu {topic_id}: tanımsız kaynak kullanıyor: {source_id}")

        sections = topic.get("sections") or []
        if not sections:
            report.error(f"Konu {topic_id}: içerik bölümü bulunmuyor.")
            continue
        section_ids: set[str] = set()
        for section in sections:
            section_count += 1
            section_id = normalized(section.get("id"))
            if not section_id or section_id.casefold() in section_ids:
                report.error(f"Konu {topic_id}: boş veya yinelenen bölüm kimliği: {section_id!r}")
            section_ids.add(section_id.casefold())
            title = normalized(section.get("title"))
            body = normalized(section.get("body"))
            if not title or not body:
                report.error(f"Konu {topic_id}/{section_id}: başlık veya gövde boş.")
                continue
            compact_body = re.sub(r"\s+", " ", body).casefold()
            if len(compact_body) >= 80:
                duplicate_bodies[compact_body] += 1
            if SUSPICIOUS_INLINE_MARKER.search(body.replace("\n", " ")):
                inline_marker_count += 1
            for source_id in section.get("sourceIds") or []:
                if normalized(source_id).casefold() not in source_ids:
                    report.error(f"Bölüm {topic_id}/{section_id}: tanımsız kaynak: {source_id}")

    common_ids = [normalized(item).casefold() for item in profile.get("commonTopicIds") or []]
    duty_ids = [normalized(item).casefold() for item in profile.get("dutyTopicIds") or []]
    profile_ids = common_ids + duty_ids
    if len(profile_ids) != len(set(profile_ids)):
        report.error("Sınav profilinde yinelenen konu kimliği var.")
    missing = sorted(set(profile_ids) - topic_ids)
    extra = sorted(topic_ids - set(profile_ids))
    if missing:
        report.error("Sınav profilinde olup katalogda bulunmayan konular: " + ", ".join(missing))
    if extra:
        report.error("Katalogda olup sınav profiline eklenmeyen konular: " + ", ".join(extra))
    if profile.get("commonQuestionCount", 0) + profile.get("dutyQuestionCount", 0) != profile.get("totalQuestionCount", 0):
        report.error("Ortak ve görev soru sayılarının toplamı totalQuestionCount ile eşleşmiyor.")
    if not normalized(profile.get("announcementUrl")):
        report.error("Sınav ilanının resmî adresi boş olamaz.")

    duplicated = sum(count - 1 for count in duplicate_bodies.values() if count > 1)
    if duplicated:
        report.warn(f"{duplicated} bölüm gövdesi başka bir bölümle tamamen aynı; editoryal kontrol önerilir.")
    if inline_marker_count:
        report.warn(
            f"{inline_marker_count} bölümde paragraf içinde a), b) veya (1), (2) benzeri işaret bulundu. "
            "Bunların ayrı satır olup olmadığı önizlemede kontrol edilmelidir."
        )
    if empty_source_topics:
        report.warn(f"{empty_source_topics} konunun doğrudan sourceIds kaydı yok.")

    report.statistics = {
        "sources": len(sources),
        "topics": len(topics),
        "sections": section_count,
        "profileTopics": len(profile_ids),
        "inlineListMarkerCandidates": inline_marker_count,
        "duplicateBodyCandidates": duplicated,
    }
    return report


def write_markdown(report: Report, destination: Path, package: Path) -> None:
    status = "BAŞARISIZ" if report.errors else "BAŞARILI"
    lines = [
        "# YİM Akademi içerik kalite raporu",
        "",
        f"- Durum: **{status}**",
        f"- Paket: `{package.as_posix()}`",
        f"- Denetim zamanı: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "## İstatistikler",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in report.statistics.items())
    lines.extend(["", "## Hatalar", ""])
    lines.extend(f"- {item}" for item in report.errors or ["Yok."])
    lines.extend(["", "## Editoryal uyarılar", ""])
    lines.extend(f"- {item}" for item in report.warnings or ["Yok."])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--report", type=Path, default=Path(".quality/content-report.md"))
    args = parser.parse_args()
    report = validate(args.package)
    write_markdown(report, args.report, args.package)
    print(json.dumps({"errors": report.errors, "warnings": report.warnings, "statistics": report.statistics}, ensure_ascii=False, indent=2))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

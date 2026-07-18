#!/usr/bin/env python3
"""Yeni PGM konu listesini kurulu YİM Akademi sınav profiliyle karşılaştırır.

Girdi olarak doğrulanacak aday konu listesini JSON biçiminde alır. Betik hiçbir
paketi değiştirmez ve yayımlamaz; yalnızca inceleme raporu ve makinece okunabilir
fark dosyası üretir.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import unicodedata


MATCH_THRESHOLD = 0.74
AMBIGUOUS_THRESHOLD = 0.30
STOP_WORDS = {
    "ve", "ile", "ilgili", "hakkinda", "hakkındaki", "sayili", "sayılı",
    "kanun", "kanunu", "mevzuat", "yonetmelik", "yönetmelik", "esaslar",
}


@dataclass
class Topic:
    group: str
    title: str
    topic_id: str = ""
    source_text: str = ""
    aliases: tuple[str, ...] = ()


@dataclass
class Match:
    candidate: Topic
    current: Topic | None
    score: float
    status: str


def fold(value: str) -> str:
    value = value.casefold().translate(str.maketrans("çğıöşü", "cgiosu"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"\b\d+\s+sayili\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokens(value: str) -> set[str]:
    return {token for token in fold(value).split() if token not in STOP_WORDS and len(token) > 1}


def similarity(left: str, right: str) -> float:
    left_folded, right_folded = fold(left), fold(right)
    if left_folded == right_folded:
        return 1.0
    sequence = SequenceMatcher(None, left_folded, right_folded).ratio()
    left_tokens, right_tokens = tokens(left), tokens(right)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    containment = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)) if left_tokens and right_tokens else 0.0
    return round(sequence * 0.35 + jaccard * 0.35 + containment * 0.30, 4)


def load_current(path: Path) -> tuple[dict, list[Topic]]:
    pack = json.loads(path.read_text(encoding="utf-8"))
    topics_by_id = {item["id"]: item for item in pack.get("topics", [])}
    sources_by_id = {item["id"]: item for item in pack.get("sources", [])}
    profile = pack.get("examProfile", {})
    result: list[Topic] = []
    for group, key in (("Ortak", "commonTopicIds"), ("Görev", "dutyTopicIds")):
        for topic_id in profile.get(key, []):
            item = topics_by_id.get(topic_id)
            if item:
                source_titles = [
                    sources_by_id[source_id].get("title", "")
                    for source_id in item.get("sourceIds") or []
                    if source_id in sources_by_id
                ]
                aliases = tuple(
                    value for value in [item.get("shortTitle", ""), item.get("summary", ""), *(item.get("tags") or []), *source_titles]
                    if str(value).strip()
                )
                result.append(Topic(group=group, title=item.get("title", ""), topic_id=topic_id, aliases=aliases))
    return pack, result


def load_candidate(path: Path) -> tuple[dict, list[Topic]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: list[Topic] = []
    for group, key in (("Ortak", "commonTopics"), ("Görev", "dutyTopics")):
        values = data.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"{key} bir liste olmalıdır.")
        for item in values:
            if isinstance(item, str):
                title, source_text = item.strip(), item.strip()
            else:
                title = str(item.get("title", "")).strip()
                source_text = str(item.get("sourceText", title)).strip()
            if not title:
                raise ValueError(f"{key} içinde boş konu başlığı bulundu.")
            result.append(Topic(group=group, title=title, source_text=source_text))
    if not result:
        raise ValueError("Aday konu listesi boş olamaz.")
    return data, result


def compare(current: list[Topic], candidates: list[Topic]) -> tuple[list[Match], list[Topic]]:
    remaining = list(current)
    matches: list[Match] = []
    for candidate in candidates:
        same_group = [item for item in remaining if fold(item.group) == fold(candidate.group)]
        ranked = sorted(
            ((max(similarity(candidate.title, value) for value in (item.title, *item.aliases)), item) for item in same_group),
            key=lambda pair: pair[0], reverse=True,
        )
        best_score, best = ranked[0] if ranked else (0.0, None)
        if best is not None and best_score >= MATCH_THRESHOLD:
            status = "unchanged" if fold(candidate.title) == fold(best.title) else "renamed"
            matches.append(Match(candidate, best, best_score, status))
            remaining.remove(best)
        elif best is not None and best_score >= AMBIGUOUS_THRESHOLD:
            matches.append(Match(candidate, best, best_score, "ambiguous"))
        else:
            matches.append(Match(candidate, None, best_score, "added"))
    return matches, remaining


def write_outputs(pack: dict, candidate_data: dict, matches: list[Match], removed: list[Topic], report_path: Path, diff_path: Path) -> None:
    diff = {
        "schemaVersion": 1,
        "currentPackageVersion": pack.get("packageVersion", ""),
        "candidateExamId": candidate_data.get("examId", ""),
        "announcementUrl": candidate_data.get("announcementUrl", ""),
        "requiresApproval": True,
        "summary": {
            "unchanged": sum(item.status == "unchanged" for item in matches),
            "renamed": sum(item.status == "renamed" for item in matches),
            "added": sum(item.status == "added" for item in matches),
            "removed": len(removed),
            "ambiguous": sum(item.status == "ambiguous" for item in matches),
        },
        "matches": [
            {
                "status": item.status,
                "score": item.score,
                "group": item.candidate.group,
                "candidateTitle": item.candidate.title,
                "currentTopicId": item.current.topic_id if item.current else "",
                "currentTitle": item.current.title if item.current else "",
            }
            for item in matches
        ],
        "removed": [asdict(item) for item in removed],
    }
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(json.dumps(diff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    labels = {"unchanged": "Aynı", "renamed": "Adı değişmiş olabilir", "added": "Yeni", "ambiguous": "Belirsiz"}
    lines = [
        "# YİM Akademi sınav kapsamı karşılaştırma raporu", "",
        f"- Mevcut paket: `{pack.get('packageVersion', '')}`",
        f"- Aday sınav: `{candidate_data.get('examId', '')}`",
        f"- Resmî ilan: {candidate_data.get('announcementUrl', '') or 'Belirtilmedi'}",
        "- Otomatik yayın: **Kapalı — onay gerekli**", "",
        "## Özet", "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in diff["summary"].items())
    lines.extend(["", "## Aday konu eşleşmeleri", "", "| Grup | Durum | Yeni başlık | Mevcut başlık | Güven |", "|---|---|---|---|---:|"])
    for item in matches:
        current_title = item.current.title if item.current else "—"
        lines.append(f"| {item.candidate.group} | {labels[item.status]} | {item.candidate.title} | {current_title} | %{item.score * 100:.1f} |")
    lines.extend(["", "## Çıkarılmış olabilecek mevcut konular", ""])
    lines.extend(f"- **{item.group}:** {item.title} (`{item.topic_id}`)" for item in removed or [Topic("", "Yok")])
    lines.extend(["", "## Yayın kararı", "", "Yeni paket oluşturulmadan önce `Yeni`, `Belirsiz`, `Adı değişmiş olabilir` ve `Çıkarılmış olabilir` kayıtları resmî ilan eki üzerinden tek tek onaylanmalıdır."])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("current_pack", type=Path)
    parser.add_argument("candidate_scope", type=Path)
    parser.add_argument("--report", type=Path, default=Path(".quality/scope-comparison.md"))
    parser.add_argument("--diff", type=Path, default=Path(".quality/scope-diff.json"))
    args = parser.parse_args()
    pack, current = load_current(args.current_pack)
    candidate_data, candidates = load_candidate(args.candidate_scope)
    matches, removed = compare(current, candidates)
    write_outputs(pack, candidate_data, matches, removed, args.report, args.diff)
    summary = json.loads(args.diff.read_text(encoding="utf-8"))["summary"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

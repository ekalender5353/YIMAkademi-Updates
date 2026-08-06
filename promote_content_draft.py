#!/usr/bin/env python3
"""Doğrulanmış içerik taslağını atomik olarak güncelleme kanalına yükseltir."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import base64
import subprocess
import tempfile


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("draft", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--signing-key", type=Path, required=True)
    args = parser.parse_args()

    draft, candidate, current, manifest = map(load, (args.draft, args.candidate, args.current, args.manifest))
    errors: list[str] = []
    profile = draft.get("examProfile") or {}
    for field in ("commonQuestionCount", "dutyQuestionCount", "totalQuestionCount"):
        value = candidate.get(field)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"Aday kapsamda güvenilir {field} değeri yok.")
    if isinstance(candidate.get("commonQuestionCount"), int) and isinstance(candidate.get("dutyQuestionCount"), int) and isinstance(candidate.get("totalQuestionCount"), int):
        if candidate["commonQuestionCount"] + candidate["dutyQuestionCount"] != candidate["totalQuestionCount"]:
            errors.append("Aday kapsamın soru sayıları birbiriyle tutarlı değil.")
    if candidate.get("requiresApproval") is True:
        errors.append("Aday kapsam hâlâ insan onayı gerektiriyor olarak işaretli.")
    if not str(candidate.get("sourceDocumentUrl", "")).startswith("https://"):
        errors.append("Doğrudan resmî kaynak belge adresi yok.")
    if not candidate.get("examId") or candidate.get("examId") == "pgm-aday-sinav-kapsami":
        errors.append("Kalıcı ve benzersiz sınav kimliği üretilemedi.")
    if profile.get("commonQuestionCount", 0) + profile.get("dutyQuestionCount", 0) != profile.get("totalQuestionCount", 0):
        errors.append("Taslak paket soru sayıları tutarsız.")
    if errors:
        raise SystemExit("GÜVENLİ YAYIN ENGELLENDİ:\n- " + "\n- ".join(errors))

    raw = (json.dumps(draft, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.current.write_bytes(raw)
    if not args.signing_key.is_file():
        raise SystemExit("GÜVENLİ YAYIN ENGELLENDİ:\n- RSA imzalama özel anahtarı bulunamadı.")
    with tempfile.NamedTemporaryFile() as signature_file:
        signing = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(args.signing_key),
             "-out", signature_file.name, str(args.current)],
            capture_output=True,
            text=True,
            check=False,
        )
        if signing.returncode != 0:
            raise SystemExit("GÜVENLİ YAYIN ENGELLENDİ:\n- İçerik paketi dijital olarak imzalanamadı.")
        signature_file.seek(0)
        content_signature = base64.b64encode(signature_file.read()).decode("ascii")
    manifest.update({
        "packageVersion": draft["packageVersion"],
        "publishedAtUtc": draft["publishedAtUtc"],
        "contentPackSha256": hashlib.sha256(raw).hexdigest(),
        "contentPackSizeBytes": len(raw),
        "signatureAlgorithm": "RSA-SHA256",
        "contentPackSignature": content_signature,
        "releaseNotes": f"{candidate.get('examId')} resmî sınav kapsamı güvenlik kontrollerinden geçerek otomatik yayımlandı.",
        "officialAnnouncementUrl": candidate.get("announcementUrl", ""),
    })
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Yayın hazır: {draft['packageVersion']} / {len(raw)} bayt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

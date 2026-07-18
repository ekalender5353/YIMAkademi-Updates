#!/usr/bin/env python3
"""YİM Akademi yayın manifesti ile içerik paketinin atomik tutarlılığını doğrular."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


TRUSTED_REPOSITORY_PATH = "/ekalender5353/YIMAkademi-Updates/"


def version(value: object) -> tuple[int, int, int]:
    parts = str(value).split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"Geçersiz sürüm: {value}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("content_pack", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    raw = args.content_pack.read_bytes()
    pack = json.loads(raw.decode("utf-8"))
    errors: list[str] = []

    if manifest.get("schemaVersion") != 1:
        errors.append("Manifest şema sürümü 1 değil.")
    try:
        version(manifest.get("packageVersion"))
        version(manifest.get("minimumAppVersion"))
    except ValueError as error:
        errors.append(str(error))
    if manifest.get("packageVersion") != pack.get("packageVersion"):
        errors.append("Manifest ve içerik paketi sürümleri eşleşmiyor.")
    if manifest.get("contentPackSizeBytes") != len(raw):
        errors.append("Manifestteki paket boyutu gerçek dosya boyutuyla eşleşmiyor.")
    expected_hash = str(manifest.get("contentPackSha256", "")).casefold()
    if expected_hash != hashlib.sha256(raw).hexdigest():
        errors.append("Manifestteki SHA-256 içerik paketini doğrulamıyor.")

    content_url = urlparse(str(manifest.get("contentPackUrl", "")))
    if content_url.scheme != "https" or content_url.hostname != "raw.githubusercontent.com":
        errors.append("İçerik paketi güvenilir HTTPS GitHub ham dosya kanalında değil.")
    elif not content_url.path.startswith(TRUSTED_REPOSITORY_PATH):
        errors.append("İçerik paketi tanımlı YİM Akademi yayın deposunda değil.")
    announcement = urlparse(str(manifest.get("officialAnnouncementUrl", "")))
    if announcement.scheme != "https" or not (announcement.hostname or "").endswith("adalet.gov.tr"):
        errors.append("Resmî ilan adresi Adalet Bakanlığı HTTPS alanında değil.")

    if errors:
        raise SystemExit("YAYIN KANALI DOĞRULANAMADI:\n- " + "\n- ".join(errors))
    print(f"Yayın kanalı doğrulandı: {pack['packageVersion']} / {len(raw)} bayt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

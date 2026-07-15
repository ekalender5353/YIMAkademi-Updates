#!/usr/bin/env python3
"""PGM'deki sınav duyurularını değişiklik açısından izler.

Bu betik resmî içeriği doğrudan son kullanıcılara yayımlamaz. Yeni veya
değişen aday ilanları kaydeder; doğrulanmış content-pack daha sonra yayımlanır.
"""

from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin
from urllib.request import Request, urlopen


PGM_URL = "https://pgm.adalet.gov.tr/"
STATE_PATH = Path(".monitor/state.json")
REPORT_PATH = Path(".monitor/change-report.md")
KEYWORDS = (
    "görevde yükselme",
    "gorevde yukselme",
    "yazı işleri müdür",
    "yazi isleri mudur",
    "unvan değişikliği",
    "unvan degisikligi",
    "yazılı sınav",
    "yazili sinav",
)


def fold(value: str) -> str:
    value = html.unescape(value).casefold()
    translations = str.maketrans("çğıöşü", "cgiosu")
    return re.sub(r"\s+", " ", value.translate(translations)).strip()


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        values = dict(attrs)
        self._href = values.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            title = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.links.append((title, self._href))
            self._href = None
            self._text = []


def fetch(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "YIMAkademi-OfficialMonitor/1.0 (+public update checker)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=45) as response:
        if response.status != 200:
            raise RuntimeError(f"PGM HTTP {response.status} döndürdü.")
        body = response.read(10 * 1024 * 1024 + 1)
        if len(body) > 10 * 1024 * 1024:
            raise RuntimeError("PGM yanıtı izin verilen boyutu aştı.")
        charset = response.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")


def candidates(page: str) -> list[dict[str, str]]:
    parser = LinkParser()
    parser.feed(page)
    found: dict[str, dict[str, str]] = {}
    for title, href in parser.links:
        absolute = urljoin(PGM_URL, href)
        searchable = fold(f"{title} {absolute}")
        if not any(fold(keyword) in searchable for keyword in KEYWORDS):
            continue
        if not absolute.startswith("https://"):
            continue
        found[absolute] = {"title": title or "Başlıksız bağlantı", "url": absolute}
    return sorted(found.values(), key=lambda item: (fold(item["title"]), item["url"]))


def fingerprint(items: list[dict[str, str]]) -> str:
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(items: list[dict[str, str]], digest: str) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {"schemaVersion": 1, "source": PGM_URL, "fingerprint": digest, "candidates": items},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def write_report(previous: dict, current: list[dict[str, str]]) -> None:
    previous_urls = {item["url"] for item in previous.get("candidates", [])}
    new_items = [item for item in current if item["url"] not in previous_urls]
    lines = [
        "# PGM değişiklik adayı",
        "",
        "PGM sınav/duyuru bağlantılarında değişiklik algılandı. Bu kayıt otomatik",
        "olarak son kullanıcılara yayımlanmaz; konu kapsamı doğrulandıktan sonra",
        "yeni YİM Akademi içerik paketi hazırlanmalıdır.",
        "",
        f"Resmî kaynak: {PGM_URL}",
        "",
        "## Yeni bağlantılar",
        "",
    ]
    if new_items:
        lines.extend(f'- [{item["title"]}]({item["url"]})' for item in new_items)
    else:
        lines.append("Bağlantı başlıklarından biri değişti veya daha önceki bir bağlantı kaldırıldı.")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> int:
    page = fetch(PGM_URL)
    current = candidates(page)
    digest = fingerprint(current)
    previous = load_state()
    baseline = not bool(previous.get("fingerprint"))
    changed = not baseline and previous.get("fingerprint") != digest

    if changed:
        write_report(previous, current)
    save_state(current, digest)
    set_output("baseline", str(baseline).lower())
    set_output("changed", str(changed).lower())
    set_output("candidate_count", str(len(current)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Resmî kaynak denetimi başarısız: {error}", file=sys.stderr)
        raise

#!/usr/bin/env python3
"""
Bonus Stack — HTML → PDF build script.

Renders every `source.html` under `bonuses/*/` to a matching PDF in
`bonuses/dist/`, using the shared print stylesheet at
`bonuses/shared/print.css`.

Requires: wkhtmltopdf on PATH.

Usage:
    python bonuses/build_bonuses.py             # build all
    python bonuses/build_bonuses.py bonus-01    # build a single bonus by dir name
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
SHARED = ROOT / "shared"


def find_sources(only: str | None = None) -> list[Path]:
    sources: list[Path] = []
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name in ("shared", "dist", "__pycache__"):
            continue
        if only and d.name != only:
            continue
        src = d / "source.html"
        if src.exists():
            sources.append(src)
    return sources


def trim_trailing_blank_pages(pdf: Path, *, min_chars: int = 30) -> int:
    """
    Strip near-blank trailing pages. wkhtmltopdf sometimes adds a phantom
    final page when content sits close to the bottom margin and the
    footer can't fit. We detect pages whose extracted-text length is
    below `min_chars` and strip them from the tail.

    Returns the number of pages removed.
    """
    # How many pages does the PDF have?
    info = subprocess.run(
        ["pdfinfo", str(pdf)], capture_output=True, text=True
    )
    pages = 0
    for line in info.stdout.splitlines():
        if line.startswith("Pages:"):
            pages = int(line.split(":", 1)[1].strip())
            break
    if pages <= 1:
        return 0

    # Walk from the last page backward looking for the first "real" page.
    last_real = pages
    for p in range(pages, 0, -1):
        r = subprocess.run(
            ["pdftotext", "-f", str(p), "-l", str(p), str(pdf), "-"],
            capture_output=True, text=True,
        )
        if len(r.stdout.strip()) >= min_chars:
            last_real = p
            break
        last_real = p - 1

    if last_real >= pages or last_real < 1:
        return 0

    # Use qpdf to extract pages 1..last_real into a temp file, then replace.
    tmp = pdf.with_suffix(".trimmed.pdf")
    subprocess.run(
        ["qpdf", str(pdf), "--pages", str(pdf), f"1-{last_real}", "--",
         str(tmp)],
        check=True, capture_output=True,
    )
    tmp.replace(pdf)
    return pages - last_real


def render(src: Path) -> Path:
    """Render one HTML source to PDF. Returns the output path."""
    bonus_dir = src.parent
    out = DIST / f"{bonus_dir.name}.pdf"
    DIST.mkdir(parents=True, exist_ok=True)

    cmd = [
        "wkhtmltopdf",
        "--enable-local-file-access",
        "--print-media-type",
        "--page-size", "Letter",
        "--margin-top", "0.6in",
        "--margin-bottom", "0.6in",
        "--margin-left", "0.55in",
        "--margin-right", "0.55in",
        "--encoding", "utf-8",
        "--quiet",
        # Page footer: brand mark on left, page x of y on right
        "--footer-font-size", "7",
        "--footer-font-name", "Helvetica",
        "--footer-spacing", "5",
        "--footer-left", "PAYDAY TRACKER",
        "--footer-right", "[page] / [topage]",
        str(src),
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FAIL {bonus_dir.name}:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)

    trimmed = trim_trailing_blank_pages(out)
    if trimmed:
        print(f"    (trimmed {trimmed} blank trailing page{'s' if trimmed != 1 else ''})")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("only", nargs="?", help="Build only this bonus dir name")
    args = parser.parse_args()

    if shutil.which("wkhtmltopdf") is None:
        print("wkhtmltopdf not found on PATH", file=sys.stderr)
        return 1

    sources = find_sources(args.only)
    if not sources:
        target = f"'{args.only}'" if args.only else "any bonuses"
        print(f"No source.html files found for {target}", file=sys.stderr)
        return 1

    print(f"Rendering {len(sources)} bonus PDF(s)…\n")
    for src in sources:
        out = render(src)
        size_kb = out.stat().st_size / 1024
        print(f"  ✓ {src.parent.name:25s} → {out.relative_to(ROOT)} ({size_kb:,.1f} KB)")
    print(f"\nDone. Outputs in {DIST.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

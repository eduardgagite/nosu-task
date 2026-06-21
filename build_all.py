#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch builder for the Takazov phrasebook files in the current directory."""

import argparse
import csv
import re
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pipeline


def norm_name(s):
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[_.,;:()]+", " ", s)
    s = re.sub(r"\bч\b", " ", s)
    s = re.sub(r"\d+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_page(path):
    m = re.match(r"^(\d+)", path.stem)
    return int(m.group(1)) if m else None


def parse_chapter(path):
    m = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)", path.stem, re.I)
    return int(m.group(1)) if m else None


def pdf_chapters(path):
    stem = path.stem
    out = set()
    m = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)\s*[-_]\s*(\d+)", stem, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        out.update(range(min(a, b), max(a, b) + 1))
    m = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)(?:\s*,\s*|\s+)(\d+)", stem, re.I)
    if m:
        out.update([int(m.group(1)), int(m.group(2))])
    m = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)", stem, re.I)
    if m:
        out.add(int(m.group(1)))
    return out


def topic_title(path):
    s = path.stem
    s = re.sub(r"^\d+[\s._-]*", "", s)
    s = re.sub(r"^(?:Ч\.?\s*|_Ч_)\d+(?:\s*[-_]\s*\d+)?[\s._-]*", "", s, flags=re.I)
    return norm_name(s)


def choose_pdf(mp3, pdfs_by_page):
    exact = mp3.with_suffix(".pdf")
    if exact.exists():
        return exact
    page = parse_page(mp3)
    ch = parse_chapter(mp3)
    candidates = pdfs_by_page.get(page, [])
    if not candidates:
        return None
    if ch is not None:
        by_ch = [p for p in candidates if ch in pdf_chapters(p)]
        if by_ch:
            by_ch.sort(key=lambda p: (len(pdf_chapters(p)), len(p.name)))
            return by_ch[0]
    title = topic_title(mp3)
    if title:
        scored = []
        for p in candidates:
            pt = topic_title(p)
            if title in pt or pt in title:
                scored.append((abs(len(title) - len(pt)), p))
        if scored:
            scored.sort(key=lambda x: x[0])
            return scored[0][1]
    if len(candidates) == 1:
        return candidates[0]
    return None


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def filter_pairs(rows, mp3):
    page = parse_page(mp3)
    ch = parse_chapter(mp3)
    if ch is not None:
        keys = {f"{page}.ch{ch}", f"{page}.sec{ch}"}
        filtered = [r for r in rows if r.get("section_key") in keys]
        if filtered:
            return filtered, f"section {sorted(keys)}"
    title = topic_title(mp3)
    if title:
        filtered = [r for r in rows if title and title in norm_name(r.get("section", ""))]
        if filtered:
            return filtered, f"title {title}"
    return rows, "full pdf"


def run_segment(mp3, wdir, args):
    manifest = wdir / "manifest.csv"
    if manifest.exists() and not args.force:
        return
    pipeline.cmd_segment(Namespace(
        audio=str(mp3),
        out=str(wdir),
        min_silence=args.min_silence,
        silence_thresh=args.silence_thresh,
        pad=args.pad,
    ))


def run_pdf(pdf, mp3, wdir, args):
    all_pairs = wdir / "pairs_all.csv"
    pairs = wdir / "pairs.csv"
    if pairs.exists() and not args.force:
        return read_csv(pairs), "cached"
    pipeline.cmd_pdf(Namespace(pdf=str(pdf), start=1, end=0, out=str(all_pairs)))
    rows = read_csv(all_pairs)
    filtered, reason = filter_pairs(rows, mp3)
    fields = ["section_key", "section", "ossetian", "russian",
              "note", "raw_ossetian", "raw_russian"]
    write_csv(pairs, filtered, fields)
    return filtered, reason


def run_langid_batch(work_items, args):
    from faster_whisper import WhisperModel

    pending = []
    for item in work_items:
        manifest = item["work"] / "manifest.csv"
        if not manifest.exists():
            continue
        rows = read_csv(manifest)
        if rows and "asr_text" in rows[0] and not args.force_langid:
            continue
        pending.append((item, rows))
    if not pending:
        return

    print(f"Гружу модель Whisper ({args.model}) один раз для {len(pending)} тем.")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    for item, rows in pending:
        print(f"\n===== langid: {item['mp3'].stem} =====", flush=True)
        for r in rows:
            path = item["work"] / r["clip"]
            segments, info = model.transcribe(str(path), beam_size=args.beam_size)
            text = " ".join(s.text.strip() for s in segments).strip()
            r["lang"] = info.language
            r["lang_prob"] = f"{info.language_probability:.2f}"
            r["asr_text"] = text
            ru_like = info.language == "ru" and info.language_probability >= args.ru_threshold
            r["flag_russian"] = "RU?" if ru_like else ""
            print(f"{r['clip']}: {info.language} ({info.language_probability:.2f}) "
                  f"{r['flag_russian']} {text[:60]}", flush=True)
        fields = ["clip", "start_ms", "end_ms", "dur_ms",
                  "lang", "lang_prob", "asr_text", "flag_russian"]
        write_csv(item["work"] / "manifest.csv", rows, fields)


def run_align(item, args):
    out = Path(args.out) / item["mp3"].stem
    cmd = [
        sys.executable, "pipeline.py", "align",
        "--dir", str(item["work"]),
        "--pairs", str(item["work"] / "pairs.csv"),
        "--out", str(out),
        "--drop-empty",
    ]
    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=".")
    p.add_argument("--work", default="work")
    p.add_argument("--out", default="dataset")
    p.add_argument("--model", default="base")
    p.add_argument("--device", default="cpu")
    p.add_argument("--compute-type", default="int8")
    p.add_argument("--beam-size", type=int, default=1)
    p.add_argument("--ru-threshold", type=float, default=0.85)
    p.add_argument("--min-silence", type=int, default=450)
    p.add_argument("--silence-thresh", type=int, default=-10)
    p.add_argument("--pad", type=int, default=120)
    p.add_argument("--force", action="store_true")
    p.add_argument("--force-langid", action="store_true")
    p.add_argument("--only", help="regex по имени mp3 для отладочного запуска")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    inp = Path(args.input)
    pdfs = sorted(inp.glob("*.pdf"))
    pdfs_by_page = {}
    for pdf in pdfs:
        pdfs_by_page.setdefault(parse_page(pdf), []).append(pdf)

    items = []
    missing = []
    for mp3 in sorted(inp.glob("*.mp3"), key=lambda x: x.name):
        page = parse_page(mp3)
        if page is None or page < 40:
            continue
        if args.only and not re.search(args.only, mp3.name):
            continue
        pdf = choose_pdf(mp3, pdfs_by_page)
        if pdf is None:
            missing.append(mp3.name)
            continue
        wdir = Path(args.work) / mp3.stem
        items.append({"mp3": mp3, "pdf": pdf, "work": wdir})
        if args.limit and len(items) >= args.limit:
            break

    print(f"Тем к обработке: {len(items)}; без PDF: {len(missing)}")
    for name in missing:
        print(f"[нет PDF] {name}")

    prep_report = []
    for item in items:
        print(f"\n===== prep: {item['mp3'].stem} =====")
        print(f"PDF: {item['pdf'].name}")
        run_segment(item["mp3"], item["work"], args)
        pairs, reason = run_pdf(item["pdf"], item["mp3"], item["work"], args)
        prep_report.append({
            "topic": item["mp3"].stem,
            "pdf": item["pdf"].name,
            "filter": reason,
            "pairs": len(pairs),
        })

    run_langid_batch(items, args)

    for item in items:
        print(f"\n===== align: {item['mp3'].stem} =====")
        run_align(item, args)

    fields = ["topic", "pdf", "filter", "pairs"]
    write_csv(Path(args.out) / "_prep_report.csv", prep_report, fields)
    if missing:
        (Path(args.out) / "_missing_pdf.txt").write_text("\n".join(missing), encoding="utf-8")


if __name__ == "__main__":
    main()

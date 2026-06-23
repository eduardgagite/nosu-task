#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон блочной привязки по ВСЕМ темам -> tools/candidates/<тема>.json.

langid кэшируется в /tmp/seg_<тема>/manifest_lid.csv, поэтому повторные запуски
(после правок алгоритма) не пересчитывают Whisper и идут мгновенно.
"""
import csv
import json
import re
import subprocess
import sys
import urllib.parse
from argparse import Namespace
from pathlib import Path

import pipeline
import block_align as ba


def audio_base():
    remote = subprocess.getoutput("git config --get remote.origin.url")
    mo = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", remote)
    return (f"https://raw.githubusercontent.com/{mo.group(1)}/{mo.group(2)}/main/"
            if mo else "")


def topic_key(name):
    page = re.match(r"^(\d+)", name)
    page = int(page.group(1)) if page else 0
    ch = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)", name, re.I)
    ch = int(ch.group(1)) if ch else 0
    return (page, ch, name)


def get_clips(topic, mp3, model):
    """Сегментация + langid с кэшем."""
    wdir = Path("/tmp") / f"seg_{topic}"
    lid = wdir / "manifest_lid.csv"
    if lid.exists():
        return list(csv.DictReader(open(lid, encoding="utf-8")))
    if not (wdir / "manifest.csv").exists():
        pipeline.cmd_segment(Namespace(audio=str(mp3), out=str(wdir),
                                       min_silence=350, silence_thresh=-10, pad=120))
    clips = list(csv.DictReader(open(wdir / "manifest.csv", encoding="utf-8")))
    for c in clips:
        segs, info = model.transcribe(str(wdir / c["clip"]), beam_size=1)
        c["text"] = " ".join(s.text.strip() for s in segs).strip()
        c["lang"] = info.language
        c["prob"] = f"{info.language_probability:.3f}"
    with open(lid, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["clip", "start_ms", "end_ms", "dur_ms",
                                          "text", "lang", "prob"])
        w.writeheader()
        w.writerows(clips)
    # клипы больше не нужны (langid закэширован) — чистим, чтобы не копить память/диск
    for c in clips:
        try:
            (wdir / c["clip"]).unlink()
        except OSError:
            pass
    return clips


def main():
    from faster_whisper import WhisperModel
    base = audio_base()
    model = WhisperModel("small", device="cpu", compute_type="int8")
    topics = sorted([p.parent.name for p in Path("dataset").glob("*/dataset.xlsx")],
                    key=topic_key)
    Path("tools/candidates").mkdir(parents=True, exist_ok=True)
    print(f"тем к обработке: {len(topics)}", flush=True)

    summary = []
    for ti, topic in enumerate(topics, 1):
        try:
            mp3 = Path("raw") / f"{topic}.mp3"
            if not mp3.exists():
                print(f"[{ti}/{len(topics)}] нет mp3: {topic}", flush=True)
                summary.append((topic, 0, 0, 0, "нет mp3"))
                continue
            phrases = ba.load_phrases(Path("dataset") / topic / "dataset.xlsx")
            if not phrases:
                print(f"[{ti}/{len(topics)}] нет фраз: {topic}", flush=True)
                continue
            clips = get_clips(topic, mp3, model)
            clips, blocks, cand, flags = ba.build_candidates(clips, phrases)
            obj = {
                "topic": topic,
                "audio_url": base + "raw/" + urllib.parse.quote(mp3.name),
                "phrases": [{
                    "i": i + 1, "oset": oset, "rus": rus,
                    "start": round(cand[i][0] / 1000, 2) if cand[i] else 0.0,
                    "end": round(cand[i][1] / 1000, 2) if cand[i] else 0.0,
                    "flag": "; ".join(flags[i]) if flags[i] else "-",
                } for i, (rus, oset) in enumerate(phrases)],
            }
            Path(f"tools/candidates/{topic}.json").write_text(
                json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            ok = sum(1 for i in range(len(phrases)) if not flags[i])
            summary.append((topic, len(phrases), len(blocks), ok, ""))
            print(f"[{ti}/{len(topics)}] {topic}: фраз {len(phrases)}, "
                  f"блоков {len(blocks)}, чисто {ok}", flush=True)
        except Exception as e:
            print(f"[{ti}/{len(topics)}] ОШИБКА {topic}: {e}", flush=True)
            summary.append((topic, 0, 0, 0, f"ошибка: {e}"))

    with open("tools/candidates/_summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["topic", "phrases", "blocks", "clean", "note"])
        w.writerows(summary)
    tot = sum(s[1] for s in summary)
    clean = sum(s[3] for s in summary)
    print(f"\nГОТОВО. фраз всего {tot}, чистых кандидатов {clean}", flush=True)


if __name__ == "__main__":
    main()

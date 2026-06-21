#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Re-run Whisper language-ID on every final clip to catch Russian audio
that leaked into Ossetian rows. Writes results incrementally so the run
is observable and resumable."""
import csv
import glob
import os
from pathlib import Path

from faster_whisper import WhisperModel

OUT = Path("_langid_check.csv")

clips = sorted(glob.glob("dataset/*/sound/*.mp3"))
done = set()
if OUT.exists():
    with open(OUT, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            done.add(r["clip"])

print(f"Клипов всего: {len(clips)}; уже сделано: {len(done)}", flush=True)
model = WhisperModel("base", device="cpu", compute_type="int8")

newfile = not OUT.exists()
with open(OUT, "a", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    if newfile:
        w.writerow(["clip", "lang", "prob", "text"])
    for i, c in enumerate(clips, 1):
        if c in done:
            continue
        try:
            segs, info = model.transcribe(c, beam_size=1)
            text = " ".join(s.text.strip() for s in segs).strip()
            w.writerow([c, info.language, f"{info.language_probability:.3f}", text[:120]])
            f.flush()
            if i % 25 == 0 or info.language == "ru":
                print(f"[{i}/{len(clips)}] {info.language} {info.language_probability:.2f} "
                      f"{os.path.basename(c)} :: {text[:50]}", flush=True)
        except Exception as e:
            w.writerow([c, "ERR", "0", str(e)[:80]])
            f.flush()
print("ГОТОВО langid", flush=True)

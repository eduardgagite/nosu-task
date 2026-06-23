#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Финальная проверка: прогон нарезанных осетинских клипов через Whisper,
чтобы поймать те, что на самом деле РУССКИЕ (главная претензия заказчика).
Пишет _clip_verify.csv (path, lang, prob, text). Возобновляемо."""
import csv
import glob
from pathlib import Path

from faster_whisper import WhisperModel

OUT = Path("_clip_verify.csv")
clips = sorted(glob.glob("sound/*.mp3"))
done = set()
if OUT.exists():
    for r in csv.DictReader(open(OUT, encoding="utf-8")):
        done.add(r["path"])

model = WhisperModel("base", device="cpu", compute_type="int8")
new = not OUT.exists()
with open(OUT, "a", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    if new:
        w.writerow(["path", "lang", "prob", "text"])
    for i, c in enumerate(clips, 1):
        name = Path(c).name
        if name in done:
            continue
        try:
            segs, info = model.transcribe(c, beam_size=1)
            text = " ".join(s.text.strip() for s in segs).strip()
            w.writerow([name, info.language, f"{info.language_probability:.3f}", text[:100]])
            f.flush()
            if info.language == "ru" or i % 50 == 0:
                print(f"[{i}/{len(clips)}] {name} {info.language} {info.language_probability:.2f}", flush=True)
        except Exception as e:
            w.writerow([name, "ERR", "0", str(e)[:60]])
            f.flush()
print("ГОТОВО verify", flush=True)

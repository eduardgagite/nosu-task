#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проход 1 (только faster-whisper, без MMS — экономия памяти):
сегментация + langid всех тем с кэшем в /tmp/seg_<тема>/manifest_lid.csv."""
import re
from pathlib import Path

from faster_whisper import WhisperModel
import block_align_all as baa


def tkey(n):
    pg = re.match(r"^(\d+)", n)
    ch = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)", n, re.I)
    return (int(pg.group(1)) if pg else 0, int(ch.group(1)) if ch else 0, n)


def main():
    topics = sorted([p.parent.name for p in Path("dataset").glob("*/dataset.xlsx")],
                    key=tkey)
    model = WhisperModel("base", device="cpu", compute_type="int8")
    print(f"langid-проход: тем {len(topics)}", flush=True)
    for ti, topic in enumerate(topics, 1):
        mp3 = Path("raw") / f"{topic}.mp3"
        if not mp3.exists():
            print(f"[{ti}/{len(topics)}] нет mp3: {topic}", flush=True)
            continue
        lid = Path("/tmp") / f"seg_{topic}" / "manifest_lid.csv"
        if lid.exists():
            print(f"[{ti}/{len(topics)}] {topic}: кэш есть", flush=True)
            continue
        try:
            baa.get_clips(topic, mp3, model)
            print(f"[{ti}/{len(topics)}] {topic}: langid готов", flush=True)
        except Exception as e:
            print(f"[{ti}/{len(topics)}] ОШИБКА {topic}: {e}", flush=True)
    print("ГОТОВО langid-проход", flush=True)


if __name__ == "__main__":
    main()

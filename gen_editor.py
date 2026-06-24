#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает все темы (tools/candidates/*.json) в данные редактора и встраивает
их ПРЯМО в tools/editor.html (самодостаточный файл, открывается из файла)."""
import json
import re
from pathlib import Path


def tkey(name):
    pg = re.match(r"^(\d+)", name)
    ch = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)", name, re.I)
    return (int(pg.group(1)) if pg else 0, int(ch.group(1)) if ch else 0, name)


def main():
    files = sorted(Path("tools/candidates").glob("*.json"), key=lambda p: tkey(p.stem))
    topics = []
    nph = 0
    for jf in files:
        obj = json.loads(jf.read_text(encoding="utf-8"))
        phrases = [{"i": p["i"], "oset": p["oset"], "rus": p["rus"],
                    "start": p.get("start", 0) or 0, "end": p.get("end", 0) or 0,
                    "flag": p.get("flag", "-")} for p in obj["phrases"]]
        nph += len(phrases)
        topics.append({"topic": obj["topic"], "audio_url": obj["audio_url"],
                       "phrases": phrases})

    data_js = "window.EDITOR_DATA=" + json.dumps(topics, ensure_ascii=False).replace("</", "<\\/") + ";"
    html = Path("tools/editor.html").read_text(encoding="utf-8")
    html = re.sub(r'<script id="editordata">.*?</script>',
                  '<script id="editordata">' + data_js + '</script>', html, flags=re.S)
    Path("tools/editor.html").write_text(html, encoding="utf-8")
    print(f"тем: {len(topics)}, фраз: {nph}")
    print("-> данные встроены в tools/editor.html (просто открой его)")


if __name__ == "__main__":
    main()

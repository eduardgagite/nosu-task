#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовит данные для быстрой ручной проверки пар: tools/review_data.json
из combined_index.csv (только строки с аудио). Сортирует по длительности
к тексту — сначала самые «нормальные» (короткий текст ≈ короткий звук)."""
import csv
import json
from pathlib import Path

rows = list(csv.DictReader(open("combined_index.csv", encoding="utf-8")))
items = []
for r in rows:
    if not r["path"]:
        continue
    items.append({
        "path": r["path"],
        "url": r["url"],
        "oset": r["sentence"],
        "rus": r["sentence_rus"],
        "dur": float(r["duration"] or 0),
        "topic": r["topic"],
        "flag": r["flag"],
    })

# приоритет проверки: сперва без флагов (кандидаты в «качественные»)
items.sort(key=lambda x: (x["flag"] != "-", x["topic"]))
Path("tools").mkdir(exist_ok=True)
# .js (а не .json) — чтобы review.html грузил данные даже открытый как файл
Path("tools/review_data.js").write_text(
    "window.REVIEW_DATA=" + json.dumps(items, ensure_ascii=False) + ";",
    encoding="utf-8")
clean = sum(1 for x in items if x["flag"] == "-")
print(f"в проверку: {len(items)} пар с аудио (из них без флагов: {clean})")
print("-> tools/review_data.js  (открывай tools/review.html)")

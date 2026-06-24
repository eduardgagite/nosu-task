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
import re
Path("tools").mkdir(exist_ok=True)
# встраиваем данные ПРЯМО в review.html — самодостаточный файл, открывается из файла
data_js = "window.REVIEW_DATA=" + json.dumps(items, ensure_ascii=False).replace("</", "<\\/") + ";"
html = Path("tools/review.html").read_text(encoding="utf-8")
html = re.sub(r'<script id="reviewdata">.*?</script>',
              '<script id="reviewdata">' + data_js + '</script>', html, flags=re.S)
Path("tools/review.html").write_text(html, encoding="utf-8")
Path("tools/review_data.js").unlink(missing_ok=True)
clean = sum(1 for x in items if x["flag"] == "-")
print(f"в проверку: {len(items)} пар с аудио (из них без флагов: {clean})")
print("-> данные встроены в tools/review.html (просто открой его)")

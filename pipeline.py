#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IronProekt — пайплайн для сборки датасета из разговорника Таказова.

Что делает:
  1) segment  — режет большой mp3 на отдельные фразы по паузам (тишине)
  2) langid   — прогоняет каждый клип через Whisper: определяет язык + расшифровку,
                помечает клипы, где осталась русская речь
  3) pdf      — вытаскивает из PDF пары «осетинский — русский», чистит скобки
  4) align    — сопоставляет клипы и пары, собирает финальную таблицу (xlsx)
                и раскладывает аудио в папку sound/

Запуск по шагам (рекомендуется), например:
  python pipeline.py segment  --audio tema_05.mp3 --out work/tema_05
  python pipeline.py langid    --dir work/tema_05
  python pipeline.py pdf       --pdf razgovornik.pdf --start 40 --end 0 --out work/pairs.csv
  python pipeline.py align     --dir work/tema_05 --pairs work/pairs.csv --out dataset/

Все параметры резки (длина паузы, порог тишины) настраиваются — см. ниже.
"""

import argparse
import csv
import os
import re
import shutil  # noqa
import sys
import unicodedata
from collections import Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# ШАГ 1. Резка по тишине
# ---------------------------------------------------------------------------
def cmd_segment(args):
    """Режет один mp3 на клипы по паузам. Пишет клипы + manifest.csv."""
    from pydub import AudioSegment
    from pydub.silence import detect_nonsilent

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Загружаю {args.audio} ...")
    audio = AudioSegment.from_file(args.audio)

    # detect_nonsilent возвращает список [начало, конец] (мс) звучащих кусков.
    # min_silence_len — насколько длинной должна быть пауза, чтобы считать её разрезом.
    # silence_thresh  — порог громкости (дБ): тише него = тишина.
    nonsilent = detect_nonsilent(
        audio,
        min_silence_len=args.min_silence,
        silence_thresh=audio.dBFS + args.silence_thresh,
        seek_step=5,
    )

    pad = args.pad  # запас по краям, чтобы не отрезать начало/конец слова
    rows = []
    for i, (start, end) in enumerate(nonsilent, 1):
        s = max(0, start - pad)
        e = min(len(audio), end + pad)
        clip = audio[s:e]
        name = f"{i:04d}.mp3"
        clip.export(out / name, format="mp3")
        rows.append({
            "clip": name,
            "start_ms": s,
            "end_ms": e,
            "dur_ms": e - s,
        })

    manifest = out / "manifest.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["clip", "start_ms", "end_ms", "dur_ms"])
        w.writeheader()
        w.writerows(rows)

    print(f"Готово: {len(rows)} клипов в {out}")
    print(f"Манифест: {manifest}")
    print("\nЕсли клипов СЛИШКОМ МНОГО (режет посреди фразы) — увеличь --min-silence.")
    print("Если СЛИШКОМ МАЛО (склеивает фразы) — уменьши --min-silence "
          "или подними --silence-thresh (например, до -16).")


# ---------------------------------------------------------------------------
# ШАГ 2. Определение языка + расшифровка (Whisper)
# ---------------------------------------------------------------------------
def cmd_langid(args):
    """Прогоняет клипы через Whisper. Дополняет manifest колонками lang/text."""
    from faster_whisper import WhisperModel

    d = Path(args.dir)
    manifest = d / "manifest.csv"
    rows = list(csv.DictReader(open(manifest, encoding="utf-8")))

    print(f"Гружу модель Whisper ({args.model}) — первый раз скачивается, потом из кэша.")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)

    for r in rows:
        path = d / r["clip"]
        segments, info = model.transcribe(str(path), beam_size=args.beam_size)
        text = " ".join(s.text.strip() for s in segments).strip()
        r["lang"] = info.language
        r["lang_prob"] = f"{info.language_probability:.2f}"
        r["asr_text"] = text
        # Осетинского в Whisper нет: осетинскую речь он часто помечает как ru/uk/bg
        # с НИЗКОЙ уверенностью. Чистый русский — обычно ru с ВЫСОКОЙ уверенностью.
        ru_like = (info.language == "ru" and info.language_probability >= args.ru_threshold)
        r["flag_russian"] = "RU?" if ru_like else ""
        print(f"{r['clip']}: {info.language} ({info.language_probability:.2f}) "
              f"{r['flag_russian']}  {text[:50]}")

    fields = ["clip", "start_ms", "end_ms", "dur_ms",
              "lang", "lang_prob", "asr_text", "flag_russian"]
    with open(manifest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    flagged = sum(1 for r in rows if r["flag_russian"])
    print(f"\nГотово. Помечено как возможно русские: {flagged}. "
          f"Проверь их в manifest.csv (колонка flag_russian).")


# ---------------------------------------------------------------------------
# ШАГ 3. Извлечение пар из PDF + чистка скобок
# ---------------------------------------------------------------------------
VARIANT_NOTE = "варианты в исходном PDF — сверить по Whisper/на слух"


def strip_pdf_artifacts(s: str) -> str:
    """Убирает служебные вставки pdfminer вида (cid:3), не трогая реальные скобки."""
    return re.sub(r"\(cid:\d+\)", "", s or "")


def clean_common(s: str) -> str:
    """Нормализация письма: ударения прочь, ў -> у, служебные знаки прочь."""
    s = strip_pdf_artifacts(s)
    s = s.replace("ў", "у").replace("Ў", "У").replace("æ", "ӕ").replace("Æ", "Ӕ")
    s = s.replace("°", "").replace("·", "").replace("\u00a0", " ")
    s = unicodedata.normalize("NFD", s)
    # Убираем только знаки ударения. Не удаляем все Mn: так ломается русское "й".
    s = "".join(ch for ch in s if ch not in {"\u0300", "\u0301"})
    # В PDF осетинская "А/И" иногда распознана как латинская A/I с ударением.
    s = re.sub(r"A(?=[\u0400-\u04FF])", "А", s)
    s = re.sub(r"(?<=[\u0400-\u04FF])A", "А", s)
    s = re.sub(r"I(?=[\u0400-\u04FF])", "И", s)
    s = re.sub(r"(?<=[\u0400-\u04FF])I", "И", s)
    return unicodedata.normalize("NFC", s)


def has_variants(s: str) -> bool:
    s = strip_pdf_artifacts(s)
    return bool(re.search(r"\([^)]*\)|\[[^\]]*\]|\s/\s|\S/\S", s))


def clean_text(s: str) -> str:
    """Приводит текст к 'произносимому' виду: убирает скобки/варианты."""
    if not s:
        return ""
    raw = clean_common(s)
    # убрать содержимое в круглых и квадратных скобках вместе со скобками
    s = re.sub(r"\([^)]*\)", "", raw)
    s = re.sub(r"\[[^\]]*\]", "", s)
    # вариант через слэш «привет/здравствуй» -> берём первый
    s = re.sub(r"\s*/\s*\S+", "", s)
    # склейка переносов с дефисом внутри слова
    s = re.sub(r"(\w)-\s+(\w)", r"\1\2", s)
    # пробелы перед пунктуацией и после открывающих скобок
    s = re.sub(r"\s+([,.;:!?…])", r"\1", s)
    s = re.sub(r"([«(])\s+", r"\1", s)
    # лишние пробелы и знаки
    s = re.sub(r"\s{2,}", " ", s).strip(" \t\u00a0-—–·")
    if "?" in raw and s and not re.search(r"[?!.…]$", s):
        s += "?"
    return s.strip()


def cmd_pdf(args):
    """Тащит текст из PDF (диапазон страниц) и пытается разбить на пары RU/OS."""
    import pdfplumber

    start = args.start
    end = args.end if args.end and args.end > 0 else None
    pairs = []
    raw_dump = []
    file_page = re.match(r"^(\d+)", Path(args.pdf).name)
    state = {
        "page_no": file_page.group(1) if file_page else "",
        "section_no": 0,
        "section": "",
        "section_key": file_page.group(1) if file_page else "",
    }

    with pdfplumber.open(args.pdf) as pdf:
        last = end or len(pdf.pages)
        for pno in range(start - 1, min(last, len(pdf.pages))):
            page = pdf.pages[pno]
            text = page.extract_text() or ""
            raw_dump.append(f"===== страница {pno+1} =====\n{text}\n")

            words = page.extract_words()
            if words:
                rows = page_rows(words, page.width)
                pairs.extend(rows_to_pairs(rows, state))

    # сырой текст — чтобы свериться глазами с реальной разметкой
    Path(args.out).with_suffix(".rawtext.txt").write_text(
        "\n".join(raw_dump), encoding="utf-8")

    fields = ["section_key", "section", "ossetian", "russian",
              "note", "raw_ossetian", "raw_russian"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in pairs:
            if row["ossetian"] or row["russian"]:
                w.writerow(row)

    print(f"Извлечено пар: {len(pairs)} -> {args.out}")
    print(f"Сырой текст для проверки: {Path(args.out).with_suffix('.rawtext.txt')}")
    if not pairs:
        print("\nВНИМАНИЕ: текст не извлёкся. Возможно PDF — это скан (картинки) "
              "без текстового слоя. Тогда нужен OCR или ручной ввод текста. "
              "Аудио при этом всё равно режется штатно.")


def detect_column_cut(words, page_width):
    """Находит левую границу осетинской колонки по повторяющемуся x0."""
    candidates = [
        round(w["x0"], 1) for w in words
        if page_width * 0.42 <= w["x0"] <= page_width * 0.65
    ]
    if not candidates:
        return page_width * 0.47
    mode_x, count = Counter(candidates).most_common(1)[0]
    if count < 2:
        return page_width * 0.47
    return max(page_width * 0.40, mode_x - 5)


def group_lines(words, y_tol=3):
    """Собирает слова в строки по координате y (top)."""
    def add_word(parts, text):
        join = False
        if parts and re.search(r"\(cid:\d+\)$", parts[-1]):
            prev_clean = clean_common(parts[-1]).strip(".,;:!?…»\"")
            cur_clean = clean_common(text).strip(".,;:!?…»\"")
            join = (
                (len(prev_clean) > 2 and len(cur_clean) <= 2)
                or cur_clean.startswith("ъ")
                or cur_clean in {"ины", "скийы", "тӕйы"}
            )
        if join:
            parts[-1] += text
        else:
            parts.append(text)

    lines = []
    cur, cur_top, cur_x0, cur_x1 = [], None, None, None
    for w in sorted(words, key=lambda x: (round(x["top"]), x["x0"])):
        if cur_top is None or abs(w["top"] - cur_top) <= y_tol:
            add_word(cur, w["text"])
            cur_top = w["top"] if cur_top is None else cur_top
            cur_x0 = w["x0"] if cur_x0 is None else min(cur_x0, w["x0"])
            cur_x1 = w["x1"] if cur_x1 is None else max(cur_x1, w["x1"])
        else:
            lines.append({"text": " ".join(cur), "top": cur_top,
                          "x0": cur_x0, "x1": cur_x1})
            cur = []
            add_word(cur, w["text"])
            cur_top = w["top"]; cur_x0 = w["x0"]; cur_x1 = w["x1"]
    if cur:
        lines.append({"text": " ".join(cur), "top": cur_top,
                      "x0": cur_x0, "x1": cur_x1})
    return lines


def page_rows(words, page_width):
    """Возвращает физические строки: левая колонка = RU, правая = OS."""
    cut = detect_column_cut(words, page_width)
    left = [w for w in words if w["x0"] < cut]
    right = [w for w in words if w["x0"] >= cut]
    left_lines = group_lines(left)
    right_lines = group_lines(right)
    used_right = set()
    rows = []
    for li, l in enumerate(left_lines):
        best = None
        best_delta = 999
        for ri, r in enumerate(right_lines):
            if ri in used_right:
                continue
            delta = abs(r["top"] - l["top"])
            if delta <= 9 and delta < best_delta:
                best = ri
                best_delta = delta
        if best is None:
            rows.append({"top": l["top"], "ru": l["text"], "os": ""})
        else:
            used_right.add(best)
            r = right_lines[best]
            rows.append({"top": min(l["top"], r["top"]), "ru": l["text"], "os": r["text"]})
    for ri, r in enumerate(right_lines):
        if ri not in used_right:
            rows.append({"top": r["top"], "ru": "", "os": r["text"]})
    return sorted(rows, key=lambda x: x["top"])


def terminal(s: str) -> bool:
    s = strip_pdf_artifacts(s).strip()
    s = s.rstrip(")]»\"'")
    return bool(re.search(r"[.?!…:]$", s))


def continuation_start(s: str) -> bool:
    s = clean_common(s).strip()
    return bool(s and (s[0].islower() or s[0] in "([,;:«"))


def clean_heading(s: str) -> str:
    s = clean_common(s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" \t.·")


def heading_text(row):
    ru = clean_common(row["ru"]).strip()
    os_ = clean_common(row["os"]).strip()
    if os_ and re.match(r"^\d+\.$", ru):
        return clean_heading(f"{ru} {os_}")
    if os_ and re.match(r"^\d+\.\s*Ч\.?$", ru, re.I):
        return clean_heading(f"{ru} {os_}")
    if os_ and re.match(r"^\d+\.\s*Ч\.\s*\d+\.?$", ru, re.I):
        return clean_heading(f"{ru} {os_}")
    return clean_heading(ru) if not os_ else ""


def is_section_heading(row) -> bool:
    text = heading_text(row)
    if not text:
        return False
    if re.match(r"^\d+\.\s*(?:Ч\.\s*)?\d*", text, re.I):
        return True
    if re.match(r"^Ч\.\s*\d+", text, re.I):
        return True
    if terminal(clean_common(row["ru"]).strip()):
        return False
    words = re.findall(r"\w+", text, re.U)
    return 1 <= len(words) <= 5


def update_section(text: str, state):
    text = clean_heading(text)
    page = state.get("page_no") or ""
    m_page = re.match(r"^(\d+)\.", text)
    if m_page:
        page = m_page.group(1)
        state["page_no"] = page

    m_ch = re.search(r"(?:^|\s)Ч\.\s*(\d+)", text, re.I)
    state["section_no"] = state.get("section_no", 0) + 1
    title = text
    if m_ch:
        title = text[m_ch.end():].strip(" .")
        state["section_key"] = f"{page}.ch{m_ch.group(1)}" if page else f"ch{m_ch.group(1)}"
    elif m_page:
        title = text[m_page.end():].strip(" .")
        # Заголовок страницы без "Ч." не всегда является отдельной темой.
        state["section_key"] = page
        state["section_no"] = 0
    else:
        state["section_key"] = f"{page}.sec{state['section_no']}" if page else f"sec{state['section_no']}"
    state["section"] = title or text


def should_append(cur, row) -> bool:
    if not cur:
        return False
    if is_section_heading(row):
        return False
    next_ru = row["ru"].strip()
    next_os = row["os"].strip()
    if next_ru.startswith(("—", "-")) and terminal(cur["raw_russian"]):
        return False
    if continuation_start(next_ru) or continuation_start(next_os):
        return True
    return False


def make_pair(cur, state):
    raw_ru = re.sub(r"\s{2,}", " ", cur["raw_russian"]).strip()
    raw_os = re.sub(r"\s{2,}", " ", cur["raw_ossetian"]).strip()
    note = []
    if has_variants(raw_ru) or has_variants(raw_os):
        note.append(VARIANT_NOTE)
    return {
        "section_key": state.get("section_key", ""),
        "section": state.get("section", ""),
        "ossetian": clean_text(raw_os),
        "russian": clean_text(raw_ru),
        "note": "; ".join(note),
        "raw_ossetian": clean_common(raw_os),
        "raw_russian": clean_common(raw_ru),
    }


def rows_to_pairs(rows, state):
    pairs = []
    cur = None
    for row in rows:
        if is_section_heading(row):
            if cur:
                pairs.append(make_pair(cur, state))
                cur = None
            update_section(heading_text(row), state)
            continue
        if not row["ru"].strip() and not row["os"].strip():
            continue
        if should_append(cur, row):
            cur["raw_russian"] = f"{cur['raw_russian']} {row['ru']}".strip()
            cur["raw_ossetian"] = f"{cur['raw_ossetian']} {row['os']}".strip()
        else:
            if cur:
                pairs.append(make_pair(cur, state))
            cur = {"raw_russian": row["ru"], "raw_ossetian": row["os"]}
    if cur:
        pairs.append(make_pair(cur, state))
    return pairs


# ---------------------------------------------------------------------------
# ШАГ 4. Сопоставление клипов и текста -> финальный датасет
# ---------------------------------------------------------------------------
def cmd_align(args):
    """Привязка через 'русский якорь'.

    Идея: каждая запись в разговорнике звучит как РУС-фраза, затем ОСЕТ-фраза.
    Русские клипы Whisper распознаёт хорошо -> матчим их с русским текстом из PDF
    (это якоря). Осетинский клип записи = клип(ы) между этим якорем и следующим.
    Их склеиваем и сохраняем как аудио записи. Русские клипы в датасет не идут.
    """
    from openpyxl import Workbook
    from pydub import AudioSegment
    try:
        from rapidfuzz import fuzz
        score = lambda a, b: fuzz.token_set_ratio(a, b)
    except ImportError:
        from difflib import SequenceMatcher
        score = lambda a, b: SequenceMatcher(None, a, b).ratio() * 100

    d = Path(args.dir)
    clips = list(csv.DictReader(open(d / "manifest.csv", encoding="utf-8")))
    pairs = list(csv.DictReader(open(args.pairs, encoding="utf-8")))
    out = Path(args.out); sound = out / "sound"
    if sound.exists():
        shutil.rmtree(sound)
    sound.mkdir(parents=True, exist_ok=True)

    def norm(s):
        s = clean_common(s or "").lower()
        return re.sub(r"[^\w\s]", "", s)

    def is_russian_speech(c):
        try:
            prob = float(c.get("lang_prob") or 0)
        except ValueError:
            prob = 0
        return c.get("flag_russian") == "RU?" or (c.get("lang") == "ru" and prob >= 0.75)

    # 1) находим для каждой пары русский клип-якорь последовательно по аудио.
    anchors = []  # индекс клипа-якоря для каждой пары
    anchor_debug = []
    pos = 0
    for p in pairs:
        best_i, best_s = None, -1
        best_text = ""
        search_start = max(0, pos - 1)
        for i in range(search_start, len(clips)):
            c = clips[i]
            s = score(norm(p["russian"]), norm(c.get("asr_text", "")))
            is_candidate = c.get("lang") == "ru" or is_russian_speech(c) or s >= 85
            if i == pos - 1 and s < 90:
                is_candidate = False
            if not is_candidate:
                continue
            if s > best_s:
                best_s, best_i = s, i
                best_text = c.get("asr_text", "")
        if best_i is not None and best_s >= args.min_match:
            anchors.append(best_i)
            pos = best_i + 1
            anchor_debug.append({
                "row": len(anchors),
                "anchor_clip": clips[best_i]["clip"],
                "score": f"{best_s:.1f}",
                "russian": p.get("russian", ""),
                "asr_text": best_text,
            })
        else:
            anchors.append(None)
            anchor_debug.append({
                "row": len(anchors),
                "anchor_clip": "",
                "score": f"{best_s:.1f}",
                "russian": p.get("russian", ""),
                "asr_text": best_text,
            })

    anchor_set = {a for a in anchors if a is not None}

    def os_clip_indices(start, end):
        return [
            i for i in range(start, end)
            if i not in anchor_set and not is_russian_speech(clips[i])
        ]

    def clip_duration(i):
        try:
            return int(float(clips[i].get("dur_ms") or 0))
        except ValueError:
            return 0

    def partition_clips(indices, weights):
        n = len(weights)
        if n == 0:
            return []
        if not indices:
            return [[] for _ in range(n)]
        if len(indices) <= n:
            groups = [[idx] for idx in indices] + [[] for _ in range(n - len(indices))]
            return groups

        durs = [clip_duration(i) for i in indices]
        total_dur = sum(durs) or 1
        total_weight = sum(weights) or n
        targets = [total_dur * (w or 1) / total_weight for w in weights]

        # DP-разбиение последовательных клипов на n непустых групп.
        m = len(indices)
        pref = [0]
        for d in durs:
            pref.append(pref[-1] + d)
        dp = [[float("inf")] * (m + 1) for _ in range(n + 1)]
        prev = [[None] * (m + 1) for _ in range(n + 1)]
        dp[0][0] = 0
        for g in range(1, n + 1):
            for j in range(g, m + 1):
                for i in range(g - 1, j):
                    dur = pref[j] - pref[i]
                    target = targets[g - 1] or 1
                    cost = ((dur - target) / target) ** 2
                    val = dp[g - 1][i] + cost
                    if val < dp[g][j]:
                        dp[g][j] = val
                        prev[g][j] = i
        groups = []
        g, j = n, m
        while g > 0:
            i = prev[g][j]
            if i is None:
                return [[idx] for idx in indices[:n - 1]] + [indices[n - 1:]]
            groups.append(indices[i:j])
            j = i
            g -= 1
        return list(reversed(groups))

    assignments = [[] for _ in pairs]
    run_notes = ["" for _ in pairs]
    k = 0
    while k < len(pairs):
        if anchors[k] is None:
            k += 1
            continue
        run = [k]
        last_anchor = anchors[k]
        j = k + 1
        while j < len(pairs) and anchors[j] is not None:
            between = os_clip_indices(last_anchor + 1, anchors[j]) if anchors[j] >= last_anchor else []
            if between:
                break
            run.append(j)
            last_anchor = max(last_anchor, anchors[j])
            j += 1

        next_anchor = next(
            (anchors[x] for x in range(j, len(anchors))
             if anchors[x] is not None and anchors[x] > last_anchor),
            len(clips),
        )
        pool = os_clip_indices(last_anchor + 1, next_anchor)
        weights = [max(len(pairs[idx].get("ossetian", "")), 8) for idx in run]
        groups = partition_clips(pool, weights)
        for idx, group in zip(run, groups):
            assignments[idx] = group
            if len(run) > 1 or len(pool) != len(run):
                run_notes[idx] = "групповая привязка осетинских клипов — сверить на слух"
        k = max(j, k + 1)

    wb = Workbook(); ws = wb.active; ws.title = "dataset"
    ws.append(["audio", "ossetian", "russian", "note"])

    for k, p in enumerate(pairs):
        a_i = anchors[k]
        note = p.get("note", "")
        os_clips = [clips[i] for i in assignments[k]]
        anchor_debug[k]["assigned_clips"] = " ".join(c["clip"] for c in os_clips)
        anchor_debug[k]["assigned_asr_text"] = " | ".join(c.get("asr_text", "") for c in os_clips)
        if run_notes[k]:
            note = "; ".join(x for x in [note, run_notes[k]] if x)
        if a_i is None:
            note = "; ".join(x for x in [note, "не найден русский якорь — проверить вручную"] if x)
        if os_clips:
            combined = AudioSegment.empty()
            for c in os_clips:
                combined += AudioSegment.from_file(d / c["clip"])
            name = f"{k+1:04d}.mp3"
            combined.export(sound / name, format="mp3")
            audio_cell = f"sound/{name}"
        else:
            audio_cell = ""
            note = note or "нет осетинского клипа — проверить вручную"
        if getattr(args, "drop_empty", False) and (not audio_cell or not p["ossetian"] or not p["russian"]):
            continue
        ws.append([audio_cell, p["ossetian"], p["russian"], note])

    xlsx = out / "dataset.xlsx"; wb.save(xlsx)
    with open(out / "alignment.csv", "w", newline="", encoding="utf-8") as f:
        fields = ["row", "anchor_clip", "score", "russian", "asr_text",
                  "assigned_clips", "assigned_asr_text"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(anchor_debug)
    matched = sum(1 for a in anchors if a is not None)
    print(f"Готово. Пар: {len(pairs)}, привязано по якорю: {matched}")
    print(f"Таблица: {xlsx}\nАудио: {sound}")
    print("Строки с пометкой в колонке note — проверь вручную (быстро, на слух).")


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="IronProekt dataset pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("segment", help="резать mp3 по тишине")
    s.add_argument("--audio", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--min-silence", type=int, default=500,
                   help="мин. длина паузы в мс для разреза (по умолчанию 500)")
    s.add_argument("--silence-thresh", type=int, default=-10,
                   help="порог тишины относительно средней громкости, дБ (по умолч. -14)")
    s.add_argument("--pad", type=int, default=120,
                   help="запас по краям клипа в мс (по умолчанию 120)")
    s.set_defaults(func=cmd_segment)

    l = sub.add_parser("langid", help="Whisper: язык + расшифровка")
    l.add_argument("--dir", required=True)
    l.add_argument("--model", default="large-v3")
    l.add_argument("--device", default="cpu", help="cpu или cuda")
    l.add_argument("--compute-type", default="int8")
    l.add_argument("--beam-size", type=int, default=1)
    l.add_argument("--ru-threshold", type=float, default=0.85)
    l.set_defaults(func=cmd_langid)

    d = sub.add_parser("pdf", help="вытащить пары RU/OS из PDF")
    d.add_argument("--pdf", required=True)
    d.add_argument("--start", type=int, default=40, help="первая страница (с 1)")
    d.add_argument("--end", type=int, default=0, help="последняя страница (0 = до конца)")
    d.add_argument("--out", default="pairs.csv")
    d.set_defaults(func=cmd_pdf)

    a = sub.add_parser("align", help="собрать финальный датасет (русский якорь)")
    a.add_argument("--dir", required=True, help="папка с клипами + manifest.csv")
    a.add_argument("--pairs", required=True, help="pairs.csv из шага pdf")
    a.add_argument("--out", required=True, help="папка для датасета")
    a.add_argument("--min-match", type=float, default=60,
                   help="мин. совпадение русского текста и расшифровки, 0-100")
    a.add_argument("--drop-empty", action="store_true",
                   help="не записывать строки, для которых не найден осетинский клип")
    a.set_defaults(func=cmd_align)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Блочная привязка. Структура записи: диктор читает БЛОК русских фраз, затем БЛОК
их осетинских переводов (размеры блоков нерегулярны).

Надёжно (автоматически):
  - какие клипы — русские фразы (по совпадению ASR с русским текстом PDF);
  - к каким фразам PDF относится каждый русский блок;
  - значит, следующий осетинский блок содержит ровно эти фразы по порядку.

Ненадёжно (отдаём человеку в плеере):
  - где именно граница между осетинскими фразами внутри блока.

Скрипт строит JSON-кандидат: для каждой фразы — её осетинский кусок (стартовая
догадка по паузам), плюс границы осетинского блока. Человек правит в pleere.
"""
import csv
import json
import re
from pathlib import Path

from rapidfuzz import fuzz

RU_FUZZ = 55       # ASR клипа похож на русскую фразу PDF -> это русский якорь
MERGE_FUZZ = 72    # один клип покрывает и следующую фразу (склейка)


def classify(clips, phrase_rus):
    """Для каждого клипа: лучшая русская фраза и её fuzz; флаг 'русский якорь'."""
    for c in clips:
        scores = [fuzz.token_set_ratio(c["text"].lower(), p.lower()) for p in phrase_rus]
        c["best_rus"] = max(range(len(phrase_rus)), key=lambda j: scores[j]) if phrase_rus else -1
        c["best_score"] = max(scores) if scores else 0
        c["is_ru"] = c["best_score"] >= RU_FUZZ
    return clips


def assign_blocks(clips, phrases):
    """Монотонно раскладываем клипы по фразам блоками.
    Возвращает список блоков: {phrase_idxs, ru_clips, os_clips}."""
    phrase_rus = [p[0] for p in phrases]
    blocks = []
    cur = None
    pj = 0  # следующая неназначенная фраза
    state = "seek"
    for k, c in enumerate(clips):
        if c["is_ru"] and pj < len(phrases):
            if state != "ru":
                # начинается новый русский блок
                if cur:
                    blocks.append(cur)
                cur = {"phrase_idxs": [], "ru_clips": [], "os_clips": []}
                state = "ru"
            cur["ru_clips"].append(k)
            # сколько фраз покрывает этот клип (учёт склейки двух подряд)
            assigned = [pj]
            pj += 1
            while pj < len(phrases) and fuzz.token_set_ratio(
                    c["text"].lower(), phrase_rus[pj].lower()) >= MERGE_FUZZ:
                assigned.append(pj)
                pj += 1
            cur["phrase_idxs"].extend(assigned)
        else:
            if state == "ru":
                state = "os"
            if cur is not None:
                cur["os_clips"].append(k)
    if cur:
        blocks.append(cur)
    return blocks


def split_os_block(os_clips, clips, n):
    """Разбить осетинский блок на n кусков-кандидатов по самым большим паузам."""
    if not os_clips:
        return []
    segs = [(int(clips[k]["start_ms"]), int(clips[k]["end_ms"])) for k in os_clips]
    s0, s1 = segs[0][0], segs[-1][1]
    if n <= 1:
        return [(s0, s1)]
    # паузы между соседними клипами
    gaps = []
    for i in range(len(segs) - 1):
        gaps.append((segs[i + 1][0] - segs[i][1], i))
    gaps.sort(reverse=True)
    cut_after = sorted(i for _, i in gaps[:n - 1])  # n-1 самых больших пауз
    parts = []
    start = s0
    ci = 0
    for i in range(len(segs)):
        if ci < len(cut_after) and i == cut_after[ci]:
            parts.append((start, segs[i][1]))
            start = segs[i + 1][0]
            ci += 1
    parts.append((start, s1))
    # если кусков не хватило (мало клипов), добьём равным делением
    while len(parts) < n:
        # делим самый длинный кусок пополам
        li = max(range(len(parts)), key=lambda x: parts[x][1] - parts[x][0])
        a, b = parts[li]
        mid = (a + b) // 2
        parts[li:li + 1] = [(a, mid), (mid, b)]
    return parts[:n]


def build_candidates(clips, phrases):
    clips = classify(clips, [p[0] for p in phrases])
    blocks = assign_blocks(clips, phrases)
    cand = {i: None for i in range(len(phrases))}
    flags = {i: [] for i in range(len(phrases))}
    for b in blocks:
        idxs = b["phrase_idxs"]
        parts = split_os_block(b["os_clips"], clips, len(idxs))
        if not parts:
            for j in idxs:
                flags[j].append("нет осетинского аудио в блоке")
            continue
        if len(b["ru_clips"]) < len(idxs):
            for j in idxs:
                flags[j].append("склейка русских фраз — проверить границы")
        # если осет-клипов меньше, чем фраз — деление условное
        if len(b["os_clips"]) < len(idxs):
            for j in idxs:
                flags[j].append("осет. блок недосегментирован — проверить")
        for j, span in zip(idxs, parts):
            cand[j] = span
    return clips, blocks, cand, flags


def load_phrases(topic_xlsx):
    import openpyxl
    ws = openpyxl.load_workbook(topic_xlsx).active
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        oset, rus = (r[1] or "").strip(), (r[2] or "").strip()
        if oset or rus:
            out.append((rus, oset))
    return out


def main():
    import argparse
    from faster_whisper import WhisperModel
    import pipeline
    from argparse import Namespace

    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--model", default="small")
    args = ap.parse_args()

    src_map = {}
    with open("dataset/_prep_report.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            src_map[r["topic"]] = r["pdf"]

    topic = args.topic
    mp3 = Path("raw") / f"{topic}.mp3"
    phrases = load_phrases(Path("dataset") / topic / "dataset.xlsx")
    wdir = Path("/tmp") / f"seg_{topic}"

    if not (wdir / "manifest.csv").exists():
        pipeline.cmd_segment(Namespace(audio=str(mp3), out=str(wdir),
                                       min_silence=350, silence_thresh=-10, pad=120))
    clips = list(csv.DictReader(open(wdir / "manifest.csv", encoding="utf-8")))
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    for c in clips:
        segs, info = model.transcribe(str(wdir / c["clip"]), beam_size=1)
        c["text"] = " ".join(s.text.strip() for s in segs).strip()
        c["lang"] = info.language
        c["prob"] = info.language_probability

    clips, blocks, cand, flags = build_candidates(clips, phrases)

    # выгрузка кандидатов в JSON для инструмента доводки tools/correct.html
    import subprocess as _sp
    import urllib.parse as _up
    remote = _sp.getoutput("git config --get remote.origin.url")
    mo = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", remote)
    base = (f"https://raw.githubusercontent.com/{mo.group(1)}/{mo.group(2)}/main/"
            if mo else "")
    cand_json = {
        "topic": topic,
        "audio_url": base + "raw/" + _up.quote(mp3.name),
        "phrases": [{
            "i": i + 1, "oset": oset, "rus": rus,
            "start": round(cand[i][0] / 1000, 2) if cand[i] else 0.0,
            "end": round(cand[i][1] / 1000, 2) if cand[i] else 0.0,
            "flag": "; ".join(flags[i]) if flags[i] else "-",
        } for i, (rus, oset) in enumerate(phrases)],
    }
    Path("tools/candidates").mkdir(parents=True, exist_ok=True)
    Path(f"tools/candidates/{topic}.json").write_text(
        json.dumps(cand_json, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"кандидаты -> tools/candidates/{topic}.json")

    print(f"\nтема: {topic} | фраз: {len(phrases)} | блоков: {len(blocks)}")
    print("\n=== блоки (русский диапазон -> осет. клипы) ===")
    for bi, b in enumerate(blocks, 1):
        rng = b["phrase_idxs"]
        print(f"блок {bi}: фразы {[i+1 for i in rng]} | "
              f"рус.клипов {len(b['ru_clips'])} | осет.клипов {len(b['os_clips'])}")
    print("\n=== кандидаты по фразам ===")
    ok = 0
    for i, (rus, oset) in enumerate(phrases):
        span = cand[i]
        fl = "; ".join(flags[i]) if flags[i] else "-"
        if fl == "-" and span:
            ok += 1
        dur = (span[1] - span[0]) / 1000 if span else 0
        print(f"{i+1:>2} [{dur:4.1f}s] {fl[:38]:38} {oset[:30]} / {rus[:22]}")
    print(f"\nчистых кандидатов: {ok}/{len(phrases)} (остальное правится в плеере)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ПОЛНОСТЬЮ АВТОМАТИЧЕСКАЯ привязка звук↔текст. Без ручной работы.

  1) русские якоря (faster-whisper) -> какие фразы в каком блоке (block_align);
  2) внутри осетинского блока — MMS forced alignment известного осетинского
     текста к аудио -> точные границы каждой фразы;
  3) пишет границы в tools/candidates/<тема>.json (его режет build_from_candidates).

Запуск:
    python3 auto_align.py --topic "40. Утро"      # одна тема
    python3 auto_align.py --all                    # все темы
"""
import argparse
import csv
import json
import re
import subprocess
import urllib.parse
import wave
from pathlib import Path

import numpy as np
import torch
from torchaudio.functional import forced_align, merge_tokens
from torchaudio.pipelines import MMS_FA as bundle

import block_align as ba
import block_align_all as baa

DIGRAPHS = [("гъ", "g"), ("къ", "k"), ("хъ", "h"), ("пъ", "p"), ("тъ", "t"),
            ("цъ", "c"), ("чъ", "ch"), ("дж", "j"), ("дз", "z")]
SINGLE = {"ӕ": "a", "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
          "ё": "o", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l",
          "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
          "у": "u", "ў": "w", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh",
          "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}

_model = None
_dict = None
_whisper = None


def get_model():
    global _model, _dict
    if _model is None:
        _model = bundle.get_model()
        _model.eval()
        _dict = bundle.get_dict()
    return _model, _dict


def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper


def romanize(text):
    t = text.lower()
    for a, b in DIGRAPHS:
        t = t.replace(a, b)
    out = []
    for ch in t:
        if ch in SINGLE:
            out.append(SINGLE[ch])
        elif ch.isspace():
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def wav_span(mp3, a, b):
    tmp = "/tmp/_blk.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", str(mp3),
                    "-ss", f"{a}", "-to", f"{b}", "-ar", "16000", "-ac", "1", tmp],
                   check=True)
    wf = wave.open(tmp)
    pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return torch.tensor(pcm.astype(np.float32) / 32768.0).unsqueeze(0)


def align_block(mp3, a, b, texts):
    """texts: осет. фразы блока по порядку. Возвращает [(start,end)|None] в сек."""
    model, DICT = get_model()
    # романизация в слова и токены
    wlen, flat = [], []
    for t in texts:
        ws = [w for w in romanize(t).split() if any(c in DICT for c in w)]
        wlen.append(len(ws))
        for w in ws:
            flat.append([DICT[c] for c in w if c in DICT])
    if not flat:
        return [None] * len(texts)
    wav = wav_span(mp3, a, b)
    targets = torch.tensor([[t for tk in flat for t in tk]], dtype=torch.int32)
    with torch.inference_mode():
        emission, _ = model(wav)
    if targets.shape[1] >= emission.size(1):
        return [None] * len(texts)   # токенов не меньше кадров — CTC не выровняет
    try:
        aligned, scores = forced_align(emission, targets, blank=0)
    except (RuntimeError, ValueError):
        return [None] * len(texts)   # блок не выровнялся — пометим фразы
    spans = merge_tokens(aligned[0], scores[0].exp())
    wc = [len(tk) for tk in flat]
    idx, wspans = 0, []
    for c in wc:
        wspans.append(spans[idx:idx + c])
        idx += c
    frame_dur = (b - a) / emission.size(1)
    out, wi = [], 0
    for n in wlen:
        if n == 0:
            out.append(None)
            continue
        blk = wspans[wi:wi + n]
        wi += n
        s = blk[0][0].start * frame_dur + a
        e = blk[-1][-1].end * frame_dur + a
        out.append((round(s, 2), round(e, 2)))
    return out


def process_topic(topic, base, pad=0.15):
    mp3 = Path("raw") / f"{topic}.mp3"
    phrases = ba.load_phrases(Path("dataset") / topic / "dataset.xlsx")
    lid = Path("/tmp") / f"seg_{topic}" / "manifest_lid.csv"
    if lid.exists():                                   # кэш есть — whisper НЕ грузим
        clips = list(csv.DictReader(open(lid, encoding="utf-8")))
    else:
        clips = baa.get_clips(topic, mp3, get_whisper())
    clips = ba.classify(clips, [p[0] for p in phrases])
    blocks = ba.assign_blocks(clips, phrases)

    bounds = {i: None for i in range(len(phrases))}
    flags = {i: "-" for i in range(len(phrases))}
    def clip_ru(k):
        return clips[k]["lang"] == "ru" and float(clips[k].get("prob") or 0) >= 0.6

    for blk in blocks:
        idxs = blk["phrase_idxs"]
        # обрезаем русские клипы с краёв осет-блока (их якорь не распознался)
        os_clips = list(blk["os_clips"])
        while os_clips and clip_ru(os_clips[0]):
            os_clips.pop(0)
        while os_clips and clip_ru(os_clips[-1]):
            os_clips.pop()
        if not os_clips:
            for j in idxs:
                flags[j] = "нет осетинского аудио — проверить"
            continue
        a = int(clips[os_clips[0]]["start_ms"]) / 1000 - pad
        b = int(clips[os_clips[-1]]["end_ms"]) / 1000 + pad
        a = max(0, a)
        texts = [phrases[j][1] for j in idxs]   # осетинский
        spans = align_block(mp3, a, b, texts)
        for j, sp in zip(idxs, spans):
            if sp is None:
                flags[j] = "выравнивание не удалось — проверить"
            else:
                bounds[j] = sp

    obj = {
        "topic": topic,
        "audio_url": base + "raw/" + urllib.parse.quote(mp3.name),
        "phrases": [{
            "i": i + 1, "oset": oset, "rus": rus,
            "start": bounds[i][0] if bounds[i] else 0.0,
            "end": bounds[i][1] if bounds[i] else 0.0,
            "flag": flags[i],
        } for i, (rus, oset) in enumerate(phrases)],
    }
    Path("tools/candidates").mkdir(parents=True, exist_ok=True)
    Path(f"tools/candidates/{topic}.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for i in range(len(phrases)) if bounds[i])
    return len(phrases), ok


def base_url():
    remote = subprocess.getoutput("git config --get remote.origin.url")
    mo = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", remote)
    return (f"https://raw.githubusercontent.com/{mo.group(1)}/{mo.group(2)}/main/"
            if mo else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    base = base_url()

    def tkey(n):
        pg = re.match(r"^(\d+)", n)
        ch = re.search(r"(?:Ч\.?\s*|_Ч_)(\d+)", n, re.I)
        return (int(pg.group(1)) if pg else 0, int(ch.group(1)) if ch else 0, n)

    if args.all:
        topics = sorted([p.parent.name for p in Path("dataset").glob("*/dataset.xlsx")],
                        key=tkey)
        for ti, topic in enumerate(topics, 1):
            if Path(f"tools/candidates/{topic}.json").exists():
                print(f"[{ti}/{len(topics)}] {topic}: уже выровнено, пропуск", flush=True)
                continue
            try:
                n, ok = process_topic(topic, base)
                print(f"[{ti}/{len(topics)}] {topic}: {ok}/{n} выровнено", flush=True)
            except Exception as e:
                print(f"[{ti}/{len(topics)}] ПРОПУСК {topic}: {e}", flush=True)
    else:
        n, ok = process_topic(args.topic, base)
        print(f"{args.topic}: выровнено {ok}/{n}")
        print(Path(f"tools/candidates/{args.topic}.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()

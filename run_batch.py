#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пакетная обработка: проходит по папке, находит пары <тема>.mp3 + <тема>.pdf
(одинаковое имя), и для каждой прогоняет весь пайплайн:
    segment -> langid -> pdf -> align

Пример:
    python run_batch.py --input raw/ --work work/ --out dataset/ --model large-v3

Структура папки raw/:
    40__Тема.mp3   40__Тема.pdf
    41__Тема.mp3   41__Тема.pdf
    ...
Результат: dataset/<тема>/dataset.xlsx + dataset/<тема>/sound/
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
PIPE = HERE / "pipeline.py"


def run(*args):
    cmd = [sys.executable, str(PIPE), *map(str, args)]
    print(">>", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="папка с парами .mp3 + .pdf")
    p.add_argument("--work", default="work", help="папка для промежуточных файлов")
    p.add_argument("--out", default="dataset", help="папка для готовых датасетов")
    p.add_argument("--model", default="large-v3", help="модель Whisper")
    p.add_argument("--min-silence", type=int, default=450)
    p.add_argument("--silence-thresh", type=int, default=-10)
    args = p.parse_args()

    inp = Path(args.input)
    mp3s = sorted(inp.glob("*.mp3"))
    if not mp3s:
        sys.exit(f"В {inp} не найдено .mp3")

    for mp3 in mp3s:
        pdf = mp3.with_suffix(".pdf")
        name = mp3.stem
        if not pdf.exists():
            print(f"[пропуск] нет PDF для {mp3.name}")
            continue
        print(f"\n===== {name} =====")
        wdir = Path(args.work) / name
        odir = Path(args.out) / name
        try:
            run("segment", "--audio", mp3, "--out", wdir,
                "--min-silence", args.min_silence,
                "--silence-thresh", args.silence_thresh)
            run("langid", "--dir", wdir, "--model", args.model)
            run("pdf", "--pdf", pdf, "--start", 1, "--end", 0,
                "--out", wdir / "pairs.csv")
            run("align", "--dir", wdir, "--pairs", wdir / "pairs.csv",
                "--out", odir)
        except subprocess.CalledProcessError as e:
            print(f"[ошибка] на теме {name}: {e}. Перехожу к следующей.")

    print("\nГотово. Проверь датасеты в", args.out)


if __name__ == "__main__":
    main()

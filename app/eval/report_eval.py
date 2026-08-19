#!/usr/bin/env python
"""eval 런로그 → 지표 리포트. stdlib only.

    python app/eval/report_eval.py app/runs/<런>-eval.jsonl

내는 것: 정확도 · 혼동행렬 · label_source 별 슬라이스 · confidence 분포 ·
임계값별 ROC 점 · 오판 이미지 목록 (검수용).

label_source 슬라이스가 핵심 정보다: orth(코스 위 pano 의 직교 화각)는
"가장 값싸고 가장 어려운 음성"(22-labels.md §5)이라 여기 정확도가
offroute(그냥 딴 동네 도로)와 얼마나 벌어지는지가 판정 기준의 실질
난이도를 말해준다. confidence 포화(23-open-questions §6)도 여기서 본다.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

SRC = {"p": "route", "o": "orth", "r": "rev", "x": "offroute"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runlog", help="run_eval.py 가 만든 JSONL")
    ap.add_argument("--fails", type=int, default=20, help="오판 목록 최대 개수")
    a = ap.parse_args()

    header, probes = {}, []
    for line in Path(a.runlog).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("type") == "run_start":
            header = d
        elif d.get("type") == "probe" and "label" in d:
            probes.append(d)
    if not probes:
        print("label 필드가 있는 probe 줄이 없다 — run_eval.py 출력이 맞나?",
              file=sys.stderr)
        return 1

    # 이미지 경로 조인 (오판 목록용)
    images = {}
    lp = header.get("labels_path")
    if lp and Path(lp).exists():
        for line in Path(lp).read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                if d.get("type") == "sample":
                    images[d["sample_id"]] = d.get("image")

    tp = sum(1 for p in probes if p["label"] and p["is_trail"])
    fn = sum(1 for p in probes if p["label"] and not p["is_trail"])
    fp = sum(1 for p in probes if not p["label"] and p["is_trail"])
    tn = sum(1 for p in probes if not p["label"] and not p["is_trail"])
    n = len(probes)
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")

    print(f"run: {a.runlog}")
    if header.get("prompt"):
        print(f"prompt: {header['prompt'].get('system_version')} · "
              f"labels: {header.get('n_labels')}건")
    print(f"\nn={n}  accuracy={acc:.3f}  precision={prec:.3f}  recall={rec:.3f}")
    print("\n           pred T   pred F")
    print(f"  true  T  {tp:>6}   {fn:>6}")
    print(f"  true  F  {fp:>6}   {tn:>6}")

    # label_source 슬라이스 (sample_id 꼬리 문자)
    by_src = defaultdict(list)
    for p in probes:
        by_src[SRC.get(p["sample_id"][-1], "?")].append(p)
    print(f"\n{'source':>9} {'n':>5} {'acc':>6}   의미")
    desc = {"route": "코스 위 진행 방향 (T)", "orth": "같은 pano 직교 화각 (F, 최난)",
            "rev": "같은 pano 역방향 (F, 라벨 요주의)", "offroute": "코스 밖 도로 (F)"}
    for src in ("route", "orth", "rev", "offroute"):
        rows = by_src.get(src)
        if not rows:
            continue
        acc_s = sum(1 for p in rows if p["is_trail"] == p["label"]) / len(rows)
        print(f"{src:>9} {len(rows):>5} {acc_s:>6.3f}   {desc[src]}")

    # confidence 분포 (정오 × 라벨)
    print("\nconfidence 분포 (행 = conf 0~10):")
    hist = defaultdict(lambda: [0, 0, 0, 0])   # [T정답, T오답, F정답, F오답]
    for p in probes:
        c = p.get("confidence")
        if c is None:
            continue
        i = (0 if p["is_trail"] == p["label"] else 1) + (0 if p["label"] else 2)
        hist[int(c)][i] += 1
    print(f"{'conf':>5} {'T·정':>5} {'T·오':>5} {'F·정':>5} {'F·오':>5}")
    for c in sorted(hist):
        print(f"{c:>5} " + " ".join(f"{v:>5}" for v in hist[c]))
    n_conf = sum(sum(v) for v in hist.values())
    n_sat = sum(sum(v) for c, v in hist.items() if c >= 9)
    if n_conf and n_sat / n_conf > 0.8:
        print(f"  ⚠ confidence 9~10 이 {n_sat / n_conf:.0%} — 포화 상태 "
              f"(23-open-questions §6). 임계값 변별력이 없다는 뜻")

    # ROC 점: "conf >= t 이면서 is_trail" 을 양성 판정으로
    print("\nROC 점 (판정 = is_trail ∧ conf ≥ t):")
    print(f"{'t':>3} {'TPR':>6} {'FPR':>6}")
    n_t = tp + fn
    n_f = fp + tn
    for t in range(11):
        pt = sum(1 for p in probes if p["label"] and p["is_trail"]
                 and (p.get("confidence") or 0) >= t)
        pf = sum(1 for p in probes if not p["label"] and p["is_trail"]
                 and (p.get("confidence") or 0) >= t)
        print(f"{t:>3} {pt / n_t if n_t else 0:>6.3f} {pf / n_f if n_f else 0:>6.3f}")

    fails = [p for p in probes if p["is_trail"] != p["label"]]
    if fails:
        print(f"\n오판 {len(fails)}건 중 앞 {min(a.fails, len(fails))}건 "
              f"(app/labels/jongno/images/ 기준):")
        for p in fails[:a.fails]:
            img = images.get(p["sample_id"], "?")
            print(f"  {p['sample_id']:>16} 정답={p['label']} 판정={p['is_trail']} "
                  f"conf={p.get('confidence')} {img}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

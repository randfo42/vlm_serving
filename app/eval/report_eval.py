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

# label_source·course_id 등은 **라벨 파일에서 조인해 온다.** 예전에는
# sample_id 의 꼬리 문자로 역추론했는데(p/o/r/x), 그 규약이 바뀌면 슬라이스가
# 조용히 "?" 로 떨어져 통째로 사라진다. 정본은 대장이다.
JOIN_FIELDS = ("label_source", "review", "course_id", "theme", "gu",
               "arrow_diff_deg", "dist_to_route_m", "neg_origin")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runlog", help="run_eval.py 가 만든 JSONL")
    ap.add_argument("--fails", type=int, default=20, help="오판 목록 최대 개수")
    a = ap.parse_args()

    header, probes, n_sample_err = {}, [], 0
    for line in Path(a.runlog).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("type") == "run_start":
            header = d
        elif d.get("type") == "probe" and "label" in d:
            probes.append(d)
        elif d.get("type") == "event" and d.get("kind") == "sample_error":
            n_sample_err += 1
    if not probes:
        print("label 필드가 있는 probe 줄이 없다 — run_eval.py 출력이 맞나?",
              file=sys.stderr)
        return 1

    # 라벨 파일 조인 — 이미지 경로 + 슬라이스 축
    images: dict[str, str] = {}
    meta: dict[str, dict] = {}
    lp = header.get("labels_path")
    if lp and Path(lp).exists():
        for line in Path(lp).read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                if d.get("type") == "sample":
                    images[d["sample_id"]] = d.get("image")
                    meta[d["sample_id"]] = {k: d.get(k) for k in JOIN_FIELDS}
    else:
        print("⚠ labels_path 를 못 읽어 슬라이스를 낼 수 없다 "
              f"({lp}) — 정확도만 표시한다\n")

    tp = sum(1 for p in probes if p["label"] and p["is_trail"])
    fn = sum(1 for p in probes if p["label"] and not p["is_trail"])
    fp = sum(1 for p in probes if not p["label"] and p["is_trail"])
    tn = sum(1 for p in probes if not p["label"] and not p["is_trail"])
    n = len(probes)
    n_t, n_f = tp + fn, fp + tn
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")

    print(f"run: {a.runlog}")
    if header.get("prompt"):
        print(f"prompt: {header['prompt'].get('system_version')} · "
              f"labels: {header.get('n_labels')}건")
    print(f"\nn={n}  accuracy={acc:.3f}  precision={prec:.3f}  recall={rec:.3f}")
    expected = header.get("n_labels")
    if n_sample_err or (expected and n < expected):
        # 실패 샘플은 n 에서 조용히 빠진다 — 부분 실패가 정상 완료처럼 보이면
        # 안 된다 (리뷰 지적)
        print(f"  ⚠ 평가되지 않은 샘플이 있다: sample_error {n_sample_err}건"
              + (f", 라벨 {expected}건 중 probe {n}건" if expected else ""))
    print("\n           pred T   pred F")
    print(f"  true  T  {tp:>6}   {fn:>6}")
    print(f"  true  F  {fp:>6}   {tn:>6}")

    if n_f == 0:
        print("\n⚠ 이 세트는 **양성 전용**이다 — accuracy 는 recall 과 같고, "
              "precision·FPR·ROC 는 정의되지 않는다.\n"
              "  전역 오탐률은 '아예 산책로가 아닌 곳' 별도 수집이 필요하다 "
              "(22-labels.md §5).")

    def slice_table(axis: str, title: str, min_n: int = 1) -> None:
        buckets = defaultdict(list)
        for p in probes:
            v = (meta.get(p["sample_id"]) or {}).get(axis)
            if v is not None:
                buckets[v].append(p)
        if len(buckets) < 2:                 # 값이 하나뿐이면 표가 정보를 안 준다
            return
        print(f"\n{title}")
        print(f"{'값':<22} {'n':>5} {'acc':>6}")
        for v, rows in sorted(buckets.items(),
                              key=lambda x: -len(x[1]))[:20]:
            if len(rows) < min_n:
                continue
            acc_s = sum(1 for p in rows if p["is_trail"] == p["label"]) / len(rows)
            print(f"{v!s:<22} {len(rows):>5} {acc_s:>6.3f}")

    slice_table("review", "검수 결과별 — kept=확인된 양성의 재현율 / "
                          "flipped=사람이 만든 하드 네거티브")
    slice_table("theme", "테마별 — 어떤 산책로에서 되는가")
    slice_table("gu", "자치구별 — 지역 편향", min_n=5)
    slice_table("label_source", "라벨 출처별")

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

#!/usr/bin/env python
"""검수(파일 이동) 결과 → 확정 라벨. samples.jsonl + images/ → labels.jsonl

    python app/labels/apply_review.py

라벨 파이프라인 4단계 (← 사람 검수, → run_eval.py).

### 검수 방법 (make_samples.py 가 만든 폴더에서)

    images/<코스>/pos/      ← 자동 true
    images/<코스>/neg/      ← 자동 false
    images/<코스>/discard/  ← 못 쓸 이미지를 여기로 (가림·실내·렌더 깨짐)

뷰어로 훑으며 **파일을 옮기는 것**이 검수의 전부다: 라벨이 틀렸으면
pos↔neg 로 옮기고, 이미지가 못 쓸 것이면 discard/ 로 옮긴다. 파일명의
sample_id 가 정체성이라 어디로 옮겨도 추적되고, 파일명 끝 T/F 는 자동
라벨의 **기록**으로 남는다 — 확정 라벨은 파일이 놓인 폴더가 정한다.

### 조용히 넘기지 않는다

samples.jsonl 에 있는데 폴더에 없는 파일, 폴더에 있는데 samples.jsonl 에
없는 파일 — 둘 다 에러다. 검수 중 실수(다른 폴더로 이동, 이름 변경, 삭제)를
집계 단계에서 잡아야 라벨 수가 조용히 어긋나지 않는다.
"""
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent / "jongno"
SAMPLES = HERE / "samples.jsonl"
IMAGES = HERE / "images"
OUT = HERE / "labels.jsonl"

_FNAME = re.compile(r"^(?P<sid>.+?[porx])_(?P<pano>[^_]+)_(?P<heading>[\d.]+)_[TF]\.png$")


def scan_images() -> dict[str, tuple[str, Path]]:
    """images/ 전체 스캔. 반환: {sample_id: (폴더 판정, 경로)}."""
    found: dict[str, tuple[str, Path]] = {}
    for p in IMAGES.rglob("*.png"):
        folder = p.parent.name
        if folder not in ("pos", "neg", "discard"):
            raise SystemExit(f"FATAL: 모르는 폴더에 이미지가 있다: {p}\n"
                             f"  검수 폴더는 pos/neg/discard 셋뿐이다")
        m = _FNAME.match(p.name)
        if not m:
            raise SystemExit(f"FATAL: 파일명이 규칙과 다르다: {p}\n"
                             f"  이름을 바꾸면 samples.jsonl 과 조인할 수 없다")
        sid = m.group("sid")
        if sid in found:
            raise SystemExit(f"FATAL: sample_id 중복: {sid}\n"
                             f"  {found[sid][1]}\n  {p}")
        found[sid] = (folder, p)
    return found


def main() -> int:
    rows = [json.loads(line) for line in SAMPLES.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    samples = [r for r in rows if r.get("type") == "sample"]
    found = scan_images()

    missing = [s["sample_id"] for s in samples if s["sample_id"] not in found]
    unknown = sorted(set(found) - {s["sample_id"] for s in samples})
    if missing or unknown:
        for sid in missing:
            print(f"  이미지 없음: {sid} (삭제했으면 discard/ 로 옮길 것 — "
                  f"삭제는 기록이 안 남는다)", file=sys.stderr)
        for sid in unknown:
            print(f"  대장에 없음: {found[sid][1]}", file=sys.stderr)
        return 1

    stats: dict[str, dict[str, int]] = {}
    out_lines = []
    for s in samples:
        folder, path = found[s["sample_id"]]
        src = s["label_source"]
        st = stats.setdefault(src, {"kept": 0, "flipped": 0, "discarded": 0})
        if folder == "discard":
            review, final = "discarded", None
            st["discarded"] += 1
        else:
            final = folder == "pos"
            review = "kept" if final == s["label"] else "flipped"
            st[review] += 1
        out_lines.append(json.dumps({
            **s, "final_label": final, "review": review,
            "image": str(path.relative_to(IMAGES)),
        }, ensure_ascii=False))

    OUT.write_text(
        json.dumps({"type": "review_header",
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "stats": stats}, ensure_ascii=False)
        + "\n" + "\n".join(out_lines) + "\n", encoding="utf-8")

    print(f"{'source':>9} {'kept':>5} {'flip':>5} {'drop':>5}")
    for src, st in sorted(stats.items()):
        print(f"{src:>9} {st['kept']:>5} {st['flipped']:>5} {st['discarded']:>5}")
    n_pos = sum(1 for line in out_lines if '"final_label": true' in line)
    n_neg = sum(1 for line in out_lines if '"final_label": false' in line)
    print(f"\n확정: T {n_pos} / F {n_neg}", end="")
    if n_pos and n_neg / n_pos < 1.0:
        print("  ⚠ neg:pos 비율이 1:1 아래다 — make_samples 로 negative 를 더 만들 것"
              " (docs/02-open-questions.md §2b)", end="")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

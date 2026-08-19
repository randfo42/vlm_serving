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
import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labels import dataset as ds

from trailwalk import settings as settings_mod

# sample_id 는 course_id 를 포함하므로 `_` 를 쓰지 않는다. 접미 문자([porx])를
# 강제하지 않는 이유: 음성 3종이 폐기되면서 그 규약이 사라졌고, 파일명이
# 라벨의 종류를 아는 유일한 곳이 되면 규약이 바뀔 때 조용히 깨진다.
# 종류는 대장(samples.jsonl)의 label_source 가 정본이다.
_FNAME = re.compile(r"^(?P<sid>[^_]+)_(?P<pano>[^_]+)_(?P<heading>[\d.]+)_[TF]\.png$")


def scan_images(images: Path) -> dict[str, tuple[str, Path]]:
    """images/ 전체 스캔. 반환: {sample_id: (폴더 판정, 경로)}."""
    found: dict[str, tuple[str, Path]] = {}
    for p in images.rglob("*.png"):
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


def run(paths: ds.DatasetPaths) -> int:
    rows = [json.loads(line)
            for line in paths.samples.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    samples = [r for r in rows if r.get("type") == "sample"]
    found = scan_images(paths.images)

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
            # 검수로 만들어진 음성은 출처를 남긴다. 후속 "비산책로 별도 수집"
            # 음성과 섞이면 안 된다 — 이건 "라우터가 코스로 판단한 지점"이라는
            # 조건부 표본이라 전역 오탐률 추정에 쓸 수 없다 (22-labels.md §5).
            **({"neg_origin": "review"} if final is False else {}),
            "image": str(path.relative_to(paths.images)),
        }, ensure_ascii=False))

    paths.labels.write_text(
        json.dumps({"type": "review_header",
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "dataset": paths.name, "stats": stats}, ensure_ascii=False)
        + "\n" + "\n".join(out_lines) + "\n", encoding="utf-8")

    print(f"{'source':>9} {'kept':>5} {'flip':>5} {'drop':>5}")
    for src, st in sorted(stats.items()):
        print(f"{src:>9} {st['kept']:>5} {st['flipped']:>5} {st['discarded']:>5}")
    n_pos = sum(1 for line in out_lines if '"final_label": true' in line)
    n_neg = sum(1 for line in out_lines if '"final_label": false' in line)
    n_done = n_pos + n_neg
    flipped = sum(st["flipped"] for st in stats.values())
    print(f"\n확정: T {n_pos} / F {n_neg}")
    # 옛 경고("neg:pos 1:1 미만")는 양성 전용 설계에서 **항상** 뜬다.
    # 항상 뜨는 경고는 사람을 훈련시켜 경고를 무시하게 만든다 —
    # block-secret-reads 1판에서 이미 치른 수업료다 (docs/12-harness.md §4).
    if n_done and flipped / n_done > 0.20:
        print(f"  ⚠ 뒤집힘 {flipped / n_done:.0%} — 코스 폴리라인이 차도를 탔을 "
              f"가능성. 해당 코스의 SVG 를 볼 것 (eval/plot_course.py)")
    if n_done > 50 and flipped == 0 and stats and \
            all(st["discarded"] == 0 for st in stats.values()):
        print("  ⚠ 뒤집힘·폐기가 0건이다 — 검수가 실제로 수행됐는지 확인할 것 "
              "(자동 라벨은 검수 전 가설이다)")
    print(f"wrote {paths.labels}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ds.add_argument(ap)
    a = ap.parse_args()
    st = settings_mod.load()
    return run(ds.resolve(a.dataset or st.labels.dataset))


if __name__ == "__main__":
    sys.exit(main())

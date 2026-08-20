#!/usr/bin/env python
"""캡처한 pano 의 촬영 메타를 조회하고, 도보 촬영분만 모아 검수 폴더를 만든다.

    python app/labels/pano_meta.py --dataset seoul          # samples.jsonl → pano_meta.json
    python app/labels/pano_meta.py --walk-only seoul jongno # → app/labels/walk_only/

라벨 파이프라인의 **사후** 단계다 (← make_samples.py). 재캡처가 필요 없다 —
pano id 만으로 조회되고 914장이 ~3분이다.

### 왜 필요한가

`shot_tool` 은 차량 촬영인지 도보 촬영인지를 카카오 자신의 규칙으로 알려준다
(→ `docs/21-roadview-providers.md` §1.3b). 수집 당시엔 이 필드를 안 남겼고,
그래서 **914장의 80%가 차도 위였다는 사실을 파이프라인이 다 끝난 뒤에 알았다.**
이 스크립트는 그 사후 복구용이다. 새 수집에서는 캡처 전에 걸러야 한다
(→ `docs/22-labels.md` §10.4).

### 도보 판정은 카카오 규칙을 그대로 쓴다

`CAR_TOOLS` 의 **여집합**이 도보다. 화이트리스트를 만들면 새 코드가 생겼을 때
조용히 틀린다. 한계는 `docs/22-labels.md` §11 에 적어 뒀다 — 요약하면
**"걸어서 찍었다"는 보장하지만 "산책로다"는 보장하지 않는다.**
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labels import dataset as ds

from trailwalk import settings as settings_mod

CAR_TOOLS = {"102", "200", "202"}      # 카카오 panorama.js 의 isCar 와 동일
NODE_URL = "https://rv.map.kakao.com/roadview-search/v2/node/{}?SERVICE=glpano"
HEADERS = {"Referer": "https://map.kakao.com/", "User-Agent": "Mozilla/5.0"}
WALK_ONLY = ds.LABELS_ROOT / "walk_only"

FIELDS = ("shot_tool", "st_type", "st_name", "shot_date")


def is_walk(shot_tool) -> bool:
    """미지의 코드는 도보로 본다 — 카카오 `isWalk` 와 같은 쪽으로 틀린다.

    ⚠️ `str()` 로 맞춰서 본다. 이 엔드포인트는 타입이 일정하지 않아
    `kakao.py` 도 같은 응답의 id 를 `str(...)` 로 감싼다. `shot_tool` 이
    숫자 `102` 로 오면 `102 not in {"102", …}` 가 참이 되어 **차량 전체가
    도보로 뒤집히는데**, "미지 코드는 도보" 설계 때문에 아무 소리도 안 난다.
    """
    return shot_tool is None or str(shot_tool) not in CAR_TOOLS


def fetch_node(pano_id: str) -> dict:
    req = urllib.request.Request(NODE_URL.format(pano_id), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)["street_view"]["street"]


def sample_rows(paths: ds.DatasetPaths):
    """samples.jsonl 의 sample 줄만."""
    if not paths.samples.exists():
        raise SystemExit(f"{paths.samples} 가 없다 — make_samples.py 를 먼저 돌린다")
    for line in paths.samples.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("type") == "sample":
                yield row


def _save(paths: ds.DatasetPaths, out: dict) -> None:
    paths.pano_meta.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                               encoding="utf-8")


def fetch(paths: ds.DatasetPaths, pause: float = 0.1, every: int = 50) -> int:
    """pano_meta.json 갱신. 이미 있는 id 는 건너뛴다.

    **중간에 저장한다.** 끝에 한 번만 쓰면 Ctrl-C 한 번에 그때까지 받은 것을
    전부 잃고, "재개 가능" 이 끝까지 완주한 런에 대해서만 참이 된다.
    """
    out = {}
    if paths.pano_meta.exists():
        out = json.loads(paths.pano_meta.read_text(encoding="utf-8"))
    todo = [r["pano_id"] for r in sample_rows(paths)]
    todo = list(dict.fromkeys(p for p in todo if p not in out))
    print(f"{paths.name}: 조회 대상 {len(todo)}개 (기존 {len(out)}개)")
    n_err = 0
    try:
        for i, pano_id in enumerate(todo, 1):
            try:
                st = fetch_node(pano_id)
                out[pano_id] = {k: st.get(k) for k in FIELDS}
            except Exception as e:                   # 한 건 실패로 전체를 잃지 않는다
                n_err += 1
                print(f"  ⚠ {pano_id}: {e}", file=sys.stderr)
            time.sleep(pause)
            if i % every == 0:
                _save(paths, out)
                print(f"  {i}/{len(todo)}")
    finally:
        if out:                                      # 빈 {} 를 남기지 않는다
            _save(paths, out)
    if not out:
        raise SystemExit(f"{paths.name}: 한 건도 못 받았다")
    walk = sum(1 for m in out.values() if is_walk(m["shot_tool"]))
    print(f"{paths.name}: {len(out)}건 저장 · 도보 {walk} ({walk / len(out):.0%})")
    if n_err:
        # 실패는 대개 영구적이다(삭제된 pano). 다시 돌리면 그 몇 건만 재시도하고
        # 또 실패한다 — 종료코드를 1 로 두면 문서의 3단계 `&&` 사슬이 영영 끊긴다.
        print(f"⚠ {n_err}건은 조회하지 못했다 — 그만큼 도보 판정에서 빠진다. "
              f"다시 돌리면 이 건들만 재시도한다", file=sys.stderr)
    return n_err


def find_image(paths: ds.DatasetPaths, row: dict) -> Path | None:
    """sample_id 로 이미지를 찾는다. **`row["image"]` 경로를 믿지 않는다.**

    `samples.jsonl` 의 `image` 는 캡처 시점의 `<코스>/pos/<파일>` 이고, 사람이
    검수하면 `apply_review` 규약에 따라 `neg/`·`discard/` 로 **옮겨진다**.
    그 경로를 그대로 열면 검수를 한 번이라도 돌린 데이터셋에서 조용히
    "이미지 없음" 이 되어 도보 표본이 검수 폴더에서 사라진다.

    파일명 앞부분이 `sample_id` 라는 규약은 `apply_review._FNAME` 이 강제한다.
    """
    hits = sorted((paths.images / row["course_id"]).glob(f"*/{row['sample_id']}_*.png"))
    return hits[0] if hits else None


def build_walk_only(names: list[str]) -> int:
    """도보 촬영분 이미지만 `walk_only/<ds>/tool<코드>/` 로 하드링크.

    하드링크라 추가 용량이 없고, 원본을 지우기 전까진 실제 파일처럼 열린다.
    복사가 아니므로 여기서 지워도 `<ds>/images/` 의 원본은 남는다.
    """
    names = list(dict.fromkeys(names))               # 중복 인자 → os.link 충돌
    # ⚠️ 검증을 rmtree **앞** 에 전부 끝낸다. 뒤에서 죽으면 기존 검수 폴더를
    #    날린 채 index.jsonl 없는 반쪽만 남는다.
    loaded = []
    for name in names:
        paths = ds.resolve(name)
        if not paths.pano_meta.exists():
            raise SystemExit(f"{paths.pano_meta} 가 없다 — 먼저 --dataset {name} 로 조회한다")
        loaded.append((paths, json.loads(paths.pano_meta.read_text(encoding="utf-8"))))

    if WALK_ONLY.exists():
        shutil.rmtree(WALK_ONLY)
    index, total = [], 0
    for paths, meta in loaded:
        n_ok = n_miss = n_nometa = 0
        for row in sample_rows(paths):
            m = meta.get(row["pano_id"])
            if not m:                                # 조회 실패분. 조용히 넘기지 않는다
                n_nometa += 1
                continue
            if not is_walk(m["shot_tool"]):
                continue
            src = find_image(paths, row)
            if src is None:                          # 이미지는 gitignore 라 없을 수 있다
                n_miss += 1
                continue
            dest_dir = WALK_ONLY / paths.name / f"tool{m['shot_tool']}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            os.link(src, dest_dir / f"{row['course_id']}__{src.name}")
            n_ok += 1
            index.append({"dataset": paths.name, "folder": src.parent.name,
                          "course_id": row["course_id"], "sample_id": row["sample_id"],
                          "pano_id": row["pano_id"], "lat": row["lat"], "lng": row["lng"],
                          "heading": row["heading"],
                          "dist_to_route_m": row.get("dist_to_route_m"), **m})
        print(f"{paths.name}: 도보 {n_ok}장 (이미지 없음 {n_miss} · pano_meta 없음 {n_nometa})")
        if n_nometa:
            print(f"  ⚠ {n_nometa}건은 촬영 장비를 모른다 — 도보일 수도 있다. "
                  f"`--dataset {paths.name}` 를 다시 돌린다", file=sys.stderr)
        total += n_ok
    WALK_ONLY.mkdir(parents=True, exist_ok=True)
    (WALK_ONLY / "index.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in index), encoding="utf-8")
    print(f"총 {total}장 → {WALK_ONLY}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ds.add_argument(ap)
    ap.add_argument("--config", default=None, help="trailwalk.yaml 오버레이")
    ap.add_argument("--walk-only", nargs="+", metavar="DATASET",
                    help="이 데이터셋들의 도보 촬영분을 walk_only/ 로 모은다 (조회는 안 한다)")
    a = ap.parse_args()
    if a.walk_only:
        build_walk_only(a.walk_only)
        return 0
    st = settings_mod.load(a.config)
    fetch(ds.resolve(a.dataset or st.labels.dataset))
    return 0


if __name__ == "__main__":
    sys.exit(main())

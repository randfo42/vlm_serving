#!/usr/bin/env python
"""라벨 세트 평가 — labels.jsonl 의 이미지를 VLM 에 물어 정답과 대조한다.

    python app/run_eval.py
    python app/run_eval.py --config app/config/eval_v1.yaml

**CLI 인자는 `--config` 하나뿐이다** (run_walk.py 와 같은 규칙). 라벨 파일,
출력 경로, 재개 여부는 설정의 `eval:` 구획에 있다.

서버가 떠 있어야 한다: ./configs/smoke.sh

### 재개

`eval.resume: true`(기본)면 출력 파일이 이미 있을 때 기록된 sample_id 를
건너뛰고 이어 쓴다 — Metal OOM 으로 서버가 죽어도(종료코드 3) 서버를
재시작하고 **같은 명령을 다시 치면 그게 곧 재개**다.

### 결과 읽기

    python app/eval/report_eval.py app/runs/<런>.jsonl
"""
import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import prompt as P
from trailwalk import settings
from trailwalk.imaging import view_to_data_uri
from trailwalk.runlog import RunLog
from trailwalk.vlm import ServerDeadError, VlmClient, VlmError

APP = Path(__file__).resolve().parent


def load_labels(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    # discard(final_label=None)는 평가 대상이 아니다
    return [r for r in rows if r.get("type") == "sample" and r.get("final_label") is not None]


def resume_header(out: Path) -> dict | None:
    """기존 out 의 run_start 헤더. 없으면 None."""
    if not out.exists():
        return None
    for line in out.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            return d if d.get("type") == "run_start" else None
    return None


def resume_conflict(old: dict | None, new: dict) -> str | None:
    """재개가 안전하지 않으면 이유를 돌려준다.

    다른 프롬프트나 다른 라벨 파일로 만든 probe 를 한 파일에 이어 붙이면
    report_eval 이 하나의 정확도로 조용히 합산한다 (리뷰 지적). 지문이
    다르면 재개가 아니라 다른 런이다 — 새 out 경로를 쓰게 한다.
    """
    if old is None:
        return None
    for what, get in (
            ("prompt", lambda h: (h.get("prompt") or {}).get("system_sha256")),
            ("labels", lambda h: h.get("labels_sha256")),
            # url 은 "어느 서버였나"의 근사다. 포트만 바뀌어도 막히지만,
            # 막힘의 비용은 새 out 경로 하나다 — 다른 모델의 probe 가
            # 한 accuracy 로 합산되는 쪽보다 싸다 (리뷰 지적).
            ("url", lambda h: h.get("url"))):
        a, b = get(old), get(new)
        if a != b:
            return (f"기존 out 의 {what} 지문이 현재 설정과 다르다\n"
                    f"    기존: {a}\n    현재: {b}\n"
                    f"  섞어 붙이면 report 가 하나의 정확도로 합산한다. "
                    f"eval.out 에 새 경로를 줄 것.")
    return None


def done_ids(out: Path) -> set[str]:
    if not out.exists():
        return set()
    ids = set()
    for line in out.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("type") == "probe" and d.get("sample_id"):
            ids.add(d["sample_id"])
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None,
                    help=f"설정 파일 경로 (기본: {settings.DEFAULT_PATH})")
    a = ap.parse_args()

    try:
        st = settings.load(a.config)
    except settings.SettingsError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    labels_path = Path(st.eval.labels)
    if not labels_path.is_absolute():
        labels_path = APP.parent / labels_path
    if not labels_path.exists():
        print(f"✗ 라벨 파일이 없다: {labels_path}\n"
              f"  apply_review.py 를 먼저 돌릴 것 (파이프라인 4단계)", file=sys.stderr)
        return 2
    samples = load_labels(labels_path)
    # 이미지 루트는 라벨 파일 옆이다. 상수로 박아두면 데이터셋이 둘이 되는
    # 순간 다른 데이터셋의 이미지를 읽으면서 조용히 돈다 (실제로 jongno 로
    # 박혀 있었다). labels.jsonl 의 image 는 이 루트 기준 상대경로다.
    images_root = labels_path.parent / "images"
    out = Path(st.eval.out) if st.eval.out else (
        APP / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-eval.jsonl")

    header = {"kind": "eval", "schema": "eval", "url": st.vlm.url,
              "labels_path": str(labels_path),
              "images_root": str(images_root),
              "labels_sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
              "n_labels": len(samples),
              "config_path": str(Path(a.config).resolve() if a.config
                                 else settings.DEFAULT_PATH),
              "prompt": P.fingerprint(st.vlm.prompt_version)}
    skip = set()
    if st.eval.resume:
        conflict = resume_conflict(resume_header(out), header)
        if conflict:
            print(f"✗ 재개 불가: {conflict}", file=sys.stderr)
            return 2
        skip = done_ids(out)
    todo = [s for s in samples if s["sample_id"] not in skip]
    if not todo:
        print(f"전부 완료돼 있다 ({len(samples)}건). 리포트: "
              f"python app/eval/report_eval.py {out}")
        return 0

    # eval 스키마 강제 — confidence 0~10 이 있어야 ROC 를 그린다.
    # walk/eval 스키마가 판정을 바꾸지 않는 것은 확인돼 있다 (8/8 일치,
    # app/docs/23-open-questions.md §5) — eval 정확도를 walk 정확도로 읽어도 된다.
    client = VlmClient(url=st.vlm.url, schema_name="eval",
                       system_version=st.vlm.prompt_version, settings=st)

    print(f"라벨 {len(samples)} (건너뜀 {len(skip)}) → {out}")
    n_err = 0
    with RunLog(out, header, append=bool(skip)) as log:
        for i, s in enumerate(todo, 1):
            img_path = images_root / s["image"]
            try:
                uri, fmt = view_to_data_uri(img_path.read_bytes())
                v = client.assess(uri, heading=s["heading"])
            except ServerDeadError as e:
                log.event("server_dead", error=str(e), done=i - 1, total=len(todo))
                print(f"\n✗ 서버 사망 ({i - 1}/{len(todo)} 완료): {e}\n"
                      f"  서버 재시작 후 같은 명령으로 재개된다", file=sys.stderr)
                return 3
            except (VlmError, OSError) as e:
                n_err += 1
                log.event("sample_error", sample_id=s["sample_id"], error=str(e))
                continue
            log.probe(step=i, pano_id=s["pano_id"], lat=s["lat"], lng=s["lng"],
                      heading=s["heading"], verdict=v, src_format=fmt,
                      label=s["final_label"], sample_id=s["sample_id"])
            mark = "✓" if v.is_trail == s["final_label"] else "✗"
            print(f"\r{i}/{len(todo)}  {mark} {s['sample_id']} "
                  f"pred={v.is_trail} conf={v.confidence}", end="", flush=True)
        log.finish(errors=n_err, stats=vars(client.stats)
                   if hasattr(client.stats, "__dict__") else str(client.stats))
    print(f"\nwrote {out}\n리포트: python app/eval/report_eval.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

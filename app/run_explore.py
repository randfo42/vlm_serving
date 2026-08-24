#!/usr/bin/env python
"""분기 탐색 한 번 실행 — 시작점에서 뻗는 산책로를 전부 마킹한다.

    # 기본 설정 그대로 (app/config/trailwalk.yaml)
    python app/run_explore.py

    # 다른 설정으로
    python app/run_explore.py --config app/config/cheonggyecheon.yaml

**CLI 인자는 `--config` 하나뿐이다** (→ CLAUDE.md "설정").

배선(provider·VLM·기록·정리)은 전부 `trailwalk/runner.py` 에 있다 — 이 파일은
그 위의 얇은 껍데기로, 설정에서 시작점을 꺼내 넘기고 결과를 stdout 과 종료
코드로 바꾸는 것만 한다. 웹·워커도 같은 runner 를 부르므로 배선은 한 곳이다
(→ app/docs/23-open-questions.md §9).

판정은 SQLite(설정 `web.db`)에 쌓인다. explore 런로그 JSONL 은 더 안 쓴다.

서버가 떠 있어야 한다: ./configs/smoke.sh
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import settings
from trailwalk.runner import RunRequest, run_explore

APP = Path(__file__).resolve().parent


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

    lat, lng = st.run.start
    db = APP / st.web.db
    # run.out 은 이제 "런 이름" 이다 (DB 의 run.name). 옛 설정 파일이 적어 둔
    # .jsonl 경로도 stem 으로 읽어 같은 이름이 되게 한다
    name = Path(st.run.out).stem if st.run.out else None

    # 건너뛰기가 켜져 있으면 **런 시작 때** 말한다. 판정이 성근 런이라는 사실이
    # yaml 주석에만 있으면 실행 시점에는 알 수 없고, 리포트를 정확도로 읽게 된다
    skip_note = (f"\n⚠  건너뛰기 켜짐 — 노드 {st.skip.run_steps}개 찍고 "
                 f"{st.skip.skip_steps}개 건너뛴다 (갈림길은 전부 찍는다).\n"
                 f"   반경 안 지면을 다 보지 않는다 — 정확도를 재는 런이면 "
                 f"설정에서 skip.skip_steps 를 0 으로 둘 것."
                 if st.skip.skip_steps else "")
    print(f"provider={st.run.provider}  prompt={st.vlm.prompt_version}  "
          f"schema={st.vlm.schema}  start=({lat},{lng})  "
          f"반경 {st.budget.max_distance_m:.0f}m · 최대 {st.budget.max_seconds:.0f}s"
          f"{skip_note}\n"
          f"DB: {db}\n")

    out = run_explore(RunRequest(start=(lat, lng), bearing=st.run.bearing,
                                 config_path=a.config), db=db, name=name)
    res = out.result   # ExploreResult 원본. 배선 실패면 None

    if st.run.dump and res is not None:
        Path(st.run.dump).write_text(json.dumps(
            {"start": [lat, lng], "stop_reason": out.stop_reason,
             "calls": out.calls,
             "nodes": res.nodes, "probes": res.probes, "frontier": res.frontier},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"결과 JSON: {st.run.dump}")

    trails = sum(1 for p in res.probes if p["is_trail"]) if res else 0
    print(f"멈춘 이유: {out.stop_reason}")
    print(f"노드 {out.nodes}"
          + (f" (건너뜀 {out.skipped})" if out.skipped else "")
          + f" · 판정 {out.verdicts} (산책로 {trails}) · "
          f"VLM 호출 {out.calls} · {out.wall_s:.0f}s"
          + (f" · run_id {out.run_id}" if out.run_id is not None else ""))
    for w in out.warnings:
        print(f"⚠  {w['message']}")
    if res is not None and res.frontier:
        print(f"\n반경·예산에 걸려 못 간 갈래 {len(res.frontier)}개:")
        for f in res.frontier[:10]:
            print(f"  d{f['depth']:>2} {f['from_pano'] or '(시작)'} → "
                  f"{f['pano_id']}  [{f['reason']}]")

    if not out.ok:
        # 스택트레이스 대신 읽을 수 있는 한 줄. 무인 실행에서 exit 0 으로
        # 끝나면 실패를 감지할 수단이 없다 — server_dead 는 사람이 서버를
        # 재시작해야 하는 상태고, image_ignored 는 판정이 전부 환각인 런이다
        msg = next((w["message"] for w in out.warnings
                    if w["code"] == out.stop_reason), out.stop_reason)
        print(f"✗ {msg}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

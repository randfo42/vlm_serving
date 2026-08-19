#!/usr/bin/env python
"""분기 탐색 한 번 실행 — 시작점에서 뻗는 산책로를 전부 마킹한다.

    # 기본 설정 그대로 (app/config/trailwalk.yaml)
    python app/run_explore.py

    # 다른 설정으로
    python app/run_explore.py --config app/config/cheonggyecheon.yaml

**CLI 인자는 `--config` 하나뿐이다** (→ CLAUDE.md "설정").

서버가 떠 있어야 한다. run_walk.py 와 같은 배선이고, 루프만 다르다:
walk 는 한 길을 따라가고 explore 는 갈래를 전부 간다.
"""
import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import prompt as P
from trailwalk import providers, settings
from trailwalk.explore import ExploreConfig, explore
from trailwalk.imaging import view_to_data_uri
from trailwalk.runlog import RunLog
from trailwalk.vlm import VlmClient

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
    bearing = st.run.bearing
    out = Path(st.run.out) if st.run.out else (
        APP / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{st.run.provider}-explore.jsonl")

    cfg = ExploreConfig.from_settings(st)
    client = VlmClient(url=st.vlm.url, schema_name=st.vlm.schema,
                       system_version=st.vlm.prompt_version, settings=st)
    try:
        prov = providers.make(st.run.provider, settings=st)
    except (providers.ProviderError, RuntimeError) as e:
        print(f"✗ {e}", file=sys.stderr)
        if st.run.provider == "kakao":
            print("\n진단을 자세히 보려면: python app/check_kakao.py --headed", file=sys.stderr)
        return 2

    header = {"provider": prov.name, "mode": "explore", "schema": st.vlm.schema,
              "url": st.vlm.url,
              "start": [lat, lng], "start_bearing": bearing,
              "config": vars(cfg),
              # 어느 설정 파일로 돌았는지. 런로그만 보고 재현할 수 있어야 한다
              "config_path": str(Path(a.config).resolve() if a.config else settings.DEFAULT_PATH),
              "prompt": P.fingerprint(st.vlm.prompt_version)}

    print(f"provider={prov.name}  prompt={st.vlm.prompt_version}  schema={st.vlm.schema}  "
          f"start=({lat},{lng})  예산 {cfg.max_vlm_calls}호출 · depth {cfg.max_depth}\n"
          f"로그: {out}\n")
    res = None
    try:
        with RunLog(out, header,
                    image_dir=(APP / "runs" / "images" / out.stem)
                    if st.run.save_images else None) as log:
            if hasattr(prov, "on_event"):
                # provider 쪽 신호(렌더 미안정 등)도 런로그에 싣는다
                prov.on_event = log.event
            try:
                if st.run.warmup:
                    pano = prov.nearest(lat, lng, cfg.snap_radius_m)
                    if pano:
                        uri, _ = view_to_data_uri(prov.capture(pano, bearing))
                        t = time.perf_counter()
                        client.warmup(uri)
                        log.event("warmup", ms=round((time.perf_counter() - t) * 1000, 1))
                res = explore(prov, client, (lat, lng), bearing, cfg, log)
            finally:
                s = client.stats
                log.finish(stop_reason=res.stop_reason if res else "aborted",
                           nodes=len(res.nodes) if res else 0,
                           frontier=len(res.frontier) if res else 0,
                           calls=s.calls, retries=s.retries,
                           cache_misses=s.cache_misses, parse_failures=s.parse_failures,
                           mean_latency_ms=round(s.total_ms / s.calls, 1) if s.calls else None)
    finally:
        prov.close()

    if st.run.dump:
        Path(st.run.dump).write_text(json.dumps(
            {"start": [lat, lng], "stop_reason": res.stop_reason,
             "calls": res.calls,
             "nodes": res.nodes, "probes": res.probes, "frontier": res.frontier},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"결과 JSON: {st.run.dump}")

    s = client.stats
    trails = sum(1 for p in res.probes if p["is_trail"])
    print(f"멈춘 이유: {res.stop_reason}")
    print(f"노드 {len(res.nodes)} · 판정 {len(res.probes)} (산책로 {trails}) · "
          f"VLM 호출 {s.calls} · {res.wall_s:.0f}s"
          + (f" ({s.total_ms / s.calls / 1000:.2f}s/호출)" if s.calls else ""))
    if s.cache_misses:
        print(f"⚠  프리픽스 캐시 미스 {s.cache_misses}/{s.calls} — system turn 에 "
              f"가변값이 섞였는지 확인 (docs/10-client-guide.md §3.1)")
    if s.parse_failures:
        print(f"⚠  JSON 파싱 실패 {s.parse_failures}회")
    unsettled = getattr(prov, "_unsettled", 0)
    if unsettled:
        print(f"⚠  프레임 미안정 캡처 {unsettled}회 — 반쯤 로드된 화면이 판정에 "
              f"들어갔을 수 있다. 잦으면 대기 상수를 올릴 것 (kakao._settle)")
    if res.frontier:
        print(f"\n예산에 걸려 못 간 갈래 {len(res.frontier)}개:")
        for f in res.frontier[:10]:
            print(f"  d{f['depth']:>2} {f['from_pano'] or '(시작)'} → "
                  f"{f['pano_id']}  [{f['reason']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

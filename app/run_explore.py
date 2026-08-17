#!/usr/bin/env python
"""분기 탐색 한 번 실행 — 시작점에서 뻗는 산책로를 전부 마킹한다.

    # 배선 확인 (API 키 불필요, 로컬 이미지를 로드뷰인 척 씀)
    python app/run_explore.py --provider fixture --start 37.5665,126.9780 --max-calls 12

    # 실제 로드뷰 (Kakao JS 앱키 필요)
    python app/run_explore.py --provider kakao --start 37.5695,127.0050 --max-calls 60

서버가 떠 있어야 한다. run_walk.py 와 같은 배선이고, 루프만 다르다:
walk 는 한 길을 따라가고 explore 는 갈래를 전부 간다.
"""
import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import prompt as P
from trailwalk import providers
from trailwalk.explore import ExploreConfig, explore
from trailwalk.imaging import view_to_data_uri
from trailwalk.runlog import RunLog
from trailwalk.vlm import DEFAULT_URL, VlmClient

APP = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="fixture", choices=providers.NAMES)
    ap.add_argument("--start", required=True, help="lat,lng")
    ap.add_argument("--bearing", type=float, default=0.0,
                    help="폴백(그래프 없는 provider)에서 전방향 후보의 기준각. 그래프에서는 무시됨")
    ap.add_argument("--max-calls", type=int, default=ExploreConfig.max_vlm_calls,
                    help="VLM 호출 예산. 탐색 비용의 실질 상한 (~2.2s/호출)")
    ap.add_argument("--max-depth", type=int, default=ExploreConfig.max_depth,
                    help="시작점으로부터의 걸음 수 한계")
    ap.add_argument("--schema", default="walk", choices=sorted(P.SCHEMAS),
                    help="walk=is_trail 만(빠름) / eval=+confidence(ROC 용)")
    ap.add_argument("--prompt", default=P.DEFAULT_VERSION, choices=sorted(P.PINS),
                    help="판정 기준. 버전이 다른 런은 직접 비교하지 말 것 (run_walk.py 참조)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--headed", action="store_true",
                    help="kakao: 브라우저를 띄운다. 검은 화면이 찍힐 때 첫 확인 수단")
    ap.add_argument("--warmup", action="store_true",
                    help="첫 호출 전에 버리는 요청을 1회 보낸다. 지연을 재는 런에서는 켤 것")
    ap.add_argument("--out", default=None, help="런로그 경로 (기본: app/runs/<시각>-explore.jsonl)")
    a = ap.parse_args()

    lat, lng = (float(x) for x in a.start.split(","))
    out = Path(a.out) if a.out else (
        APP / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{a.provider}-explore.jsonl")

    cfg = ExploreConfig(max_vlm_calls=a.max_calls, max_depth=a.max_depth)
    client = VlmClient(url=a.url, schema_name=a.schema, system_version=a.prompt)
    try:
        prov = providers.make(a.provider, headless=not a.headed)
    except (providers.ProviderError, RuntimeError) as e:
        print(f"✗ {e}", file=sys.stderr)
        if a.provider == "kakao":
            print("\n진단을 자세히 보려면: python app/check_kakao.py --headed", file=sys.stderr)
        return 2

    header = {"provider": prov.name, "mode": "explore", "schema": a.schema, "url": a.url,
              "start": [lat, lng], "start_bearing": a.bearing,
              "config": vars(cfg) | {"side_offsets": list(cfg.side_offsets),
                                     "start_offsets": list(cfg.start_offsets)},
              "prompt": P.fingerprint(a.prompt)}

    print(f"provider={prov.name}  prompt={a.prompt}  schema={a.schema}  "
          f"start=({lat},{lng})  예산 {a.max_calls}호출 · depth {a.max_depth}\n로그: {out}\n")
    res = None
    try:
        with RunLog(out, header) as log:
            try:
                if a.warmup:
                    pano = prov.nearest(lat, lng, cfg.snap_radius_m)
                    if pano:
                        uri, _ = view_to_data_uri(prov.capture(pano, a.bearing, cfg.fov_deg))
                        t = time.perf_counter()
                        client.warmup(uri)
                        log.event("warmup", ms=round((time.perf_counter() - t) * 1000, 1))
                res = explore(prov, client, (lat, lng), a.bearing, cfg, log)
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

    s = client.stats
    trails = sum(1 for p in res.probes if p["is_trail"])
    print(f"멈춘 이유: {res.stop_reason}"
          + ("  (이웃 그래프 사용)" if res.used_graph else "  (좌표 밀기 폴백)"))
    print(f"노드 {len(res.nodes)} · 판정 {len(res.probes)} (산책로 {trails}) · "
          f"VLM 호출 {s.calls} · {res.wall_s:.0f}s"
          + (f" ({s.total_ms / s.calls / 1000:.2f}s/호출)" if s.calls else ""))
    if s.cache_misses:
        print(f"⚠  프리픽스 캐시 미스 {s.cache_misses}/{s.calls} — system turn 에 "
              f"가변값이 섞였는지 확인 (docs/10-client-guide.md §3.1)")
    if s.parse_failures:
        print(f"⚠  JSON 파싱 실패 {s.parse_failures}회")
    if res.frontier:
        print(f"\n예산에 걸려 못 간 갈래 {len(res.frontier)}개:")
        for f in res.frontier[:10]:
            print(f"  d{f['depth']:>2} {f['from_pano'] or '(시작)'} → "
                  f"{f['pano_id'] or '(좌표 밀기)'}  [{f['reason']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

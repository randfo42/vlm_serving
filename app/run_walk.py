#!/usr/bin/env python
"""탐색 루프 한 번 실행.

    # 배선 확인 (API 키 불필요, 로컬 이미지를 로드뷰인 척 씀)
    python app/run_walk.py --provider fixture --start 37.5665,126.9780 --steps 8

    # 실제 로드뷰 (Kakao JS 앱키 필요 — app/docs/23-open-questions.md §1)
    KAKAO_JS_KEY=xxx python app/run_walk.py --provider kakao \\
        --start 37.5768,127.0246 --bearing 90 --steps 60

서버가 떠 있어야 한다: ./configs/smoke.sh
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import prompt as P                      # noqa: E402
from trailwalk import providers                        # noqa: E402
from trailwalk.imaging import view_to_data_uri         # noqa: E402
from trailwalk.runlog import RunLog                    # noqa: E402
from trailwalk.vlm import DEFAULT_URL, VlmClient       # noqa: E402
from trailwalk.walk import WalkConfig, walk            # noqa: E402

APP = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="fixture", choices=providers.NAMES)
    ap.add_argument("--start", required=True, help="lat,lng")
    ap.add_argument("--bearing", type=float, default=0.0, help="출발 방위각 (0=북)")
    ap.add_argument("--steps", type=int, default=WalkConfig.max_steps)
    ap.add_argument("--step-m", type=float, default=WalkConfig.step_m)
    ap.add_argument("--candidates", type=int, default=WalkConfig.max_candidates,
                    help="한 스텝에서 최대 몇 방향까지 물어볼지 (기본 3)")
    ap.add_argument("--probe-sides-every", type=int, default=0,
                    help="N 스텝마다 갈림길 확인. 0=끄기 (기본). 호출이 3배로 는다")
    ap.add_argument("--schema", default="walk", choices=sorted(P.SCHEMAS),
                    help="walk=is_trail 만(빠름) / eval=+confidence(ROC 용)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--headed", action="store_true",
                    help="kakao: 브라우저를 띄운다. 검은 화면이 찍힐 때 첫 확인 수단")
    ap.add_argument("--warmup", action="store_true",
                    help="첫 호출 전에 버리는 요청을 1회 보낸다. 유휴 뒤 첫 요청이 "
                         "13초까지 튀므로, 지연을 재는 런에서는 켤 것")
    ap.add_argument("--out", default=None, help="런로그 경로 (기본: app/runs/<시각>.jsonl)")
    a = ap.parse_args()

    lat, lng = (float(x) for x in a.start.split(","))
    out = Path(a.out) if a.out else (
        APP / "runs" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{a.provider}.jsonl")

    cfg = WalkConfig(step_m=a.step_m, max_steps=a.steps,
                     probe_sides_every=a.probe_sides_every,
                     max_candidates=a.candidates)
    client = VlmClient(url=a.url, schema_name=a.schema)
    try:
        prov = providers.make(a.provider, headless=not a.headed)
    except (providers.ProviderError, RuntimeError) as e:
        # 설정 문제(키·도메인·서비스 활성화)는 버그가 아니다. 스택트레이스를
        # 쏟아내면 정작 읽어야 할 안내가 묻힌다.
        print(f"✗ {e}", file=sys.stderr)
        if a.provider == "kakao":
            print("\n진단을 자세히 보려면: python app/check_kakao.py --headed", file=sys.stderr)
        return 2

    header = {"provider": prov.name, "schema": a.schema, "url": a.url,
              "start": [lat, lng], "start_bearing": a.bearing,
              "config": vars(cfg) | {"side_offsets": list(cfg.side_offsets)},
              "prompt": P.fingerprint()}

    print(f"provider={prov.name}  schema={a.schema}  start=({lat},{lng}) "
          f"bearing={a.bearing}\n로그: {out}\n")
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
                res = walk(prov, client, (lat, lng), a.bearing, cfg, log)
            finally:
                # 예외로 죽어도 요약은 남긴다. 서버가 죽어 중단된 런일수록
                # 어디까지 갔고 몇 번 재시도했는지가 중요하다.
                s = client.stats
                log.finish(stop_reason=res.stop_reason if res else "aborted",
                           steps=res.steps if res else 0,
                           calls=s.calls, retries=s.retries,
                           cache_misses=s.cache_misses, parse_failures=s.parse_failures,
                           mean_latency_ms=round(s.total_ms / s.calls, 1) if s.calls else None)
    finally:
        prov.close()

    s = client.stats
    print(f"멈춘 이유: {res.stop_reason}"
          + ("  (이웃 그래프 사용)" if res.used_graph else "  (좌표 밀기 폴백)"))
    print(f"스텝 {res.steps} · VLM 호출 {s.calls} · {res.wall_s:.0f}s"
          + (f" ({s.total_ms / s.calls / 1000:.2f}s/호출)" if s.calls else ""))
    # 조용히 깨지는 신호는 요약에 반드시 띄운다. 로그를 안 열어봐도 보이게.
    if s.cache_misses:
        print(f"⚠  프리픽스 캐시 미스 {s.cache_misses}/{s.calls} — system turn 에 "
              f"가변값이 섞였는지 확인 (docs/10-client-guide.md §3.1)")
    if s.parse_failures:
        print(f"⚠  JSON 파싱 실패 {s.parse_failures}회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

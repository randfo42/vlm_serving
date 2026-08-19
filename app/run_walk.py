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
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import prompt as P
from trailwalk import providers
from trailwalk.imaging import view_to_data_uri
from trailwalk.runlog import RunLog
from trailwalk.vlm import DEFAULT_URL, VlmClient
from trailwalk.walk import WalkConfig, walk

APP = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="fixture", choices=providers.NAMES)
    ap.add_argument("--start", required=True, help="lat,lng")
    ap.add_argument("--bearing", type=float, default=0.0,
                    help="출발 방위각 (0=북). 시작 노드는 이웃을 전부 물으므로 "
                         "이 값은 어느 쪽을 먼저 물을지(정렬)만 정한다. "
                         "max_turn_deg 필터는 둘째 스텝부터 진행 방향 기준으로 돈다")
    ap.add_argument("--steps", type=int, default=WalkConfig.max_steps)
    ap.add_argument("--candidates", type=int, default=WalkConfig.max_candidates,
                    help="한 스텝에서 최대 몇 방향까지 물어볼지 (기본 3)")
    ap.add_argument("--probe-all", dest="probe_all", action="store_true", default=None,
                    help="후보를 전부 물어본다 (기본값이다 — 끄려면 --first-hit)")
    ap.add_argument("--first-hit", dest="probe_all", action="store_false",
                    help="첫 성공에서 멈춘다. 갈림길을 놓치는 대신 호출이 준다")
    ap.add_argument("--schema", default="walk", choices=sorted(P.SCHEMAS),
                    help="walk=is_trail 만(빠름) / eval=+confidence(ROC 용)")
    ap.add_argument("--prompt", default=P.DEFAULT_VERSION, choices=sorted(P.PINS),
                    help="판정 기준. v3=카메라가 산책로 위에 서 있는가(기본) / "
                         "v1=프레임에 산책로가 보이는가 / v2=v3 의 이전판(너무 엄격). "
                         "서로 다른 질문이라 버전이 다른 런은 직접 비교하지 말 것")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--headed", action="store_true",
                    help="kakao: 브라우저를 띄운다. 검은 화면이 찍힐 때 첫 확인 수단")
    ap.add_argument("--warmup", action="store_true",
                    help="첫 호출 전에 버리는 요청을 1회 보낸다. 유휴 뒤 첫 요청이 "
                         "13초까지 튀므로, 지연을 재는 런에서는 켤 것")
    ap.add_argument("--out", default=None, help="런로그 경로 (기본: app/runs/<시각>.jsonl)")
    ap.add_argument("--save-images", action="store_true",
                    help="probe 이미지를 app/runs/images/<런이름>/ 에 남긴다 "
                         "(판정을 눈으로 감사할 때. 약관 → docs/23-open-questions.md §2)")
    a = ap.parse_args()

    lat, lng = (float(x) for x in a.start.split(","))
    out = Path(a.out) if a.out else (
        APP / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{a.provider}.jsonl")

    cfg = WalkConfig(max_steps=a.steps, max_candidates=a.candidates,
                     **({} if a.probe_all is None else {"probe_all": a.probe_all}))
    client = VlmClient(url=a.url, schema_name=a.schema, system_version=a.prompt)
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
              "config": vars(cfg),
              "prompt": P.fingerprint(a.prompt)}

    print(f"provider={prov.name}  prompt={a.prompt}  schema={a.schema}  "
          f"start=({lat},{lng}) bearing={a.bearing}\n로그: {out}\n")
    res = None
    try:
        with RunLog(out, header,
                    image_dir=(APP / "runs" / "images" / out.stem)
                    if a.save_images else None) as log:
            if hasattr(prov, "on_event"):
                # provider 쪽 신호(렌더 미안정 등)도 런로그에 싣는다
                prov.on_event = log.event
            try:
                if a.warmup:
                    pano = prov.nearest(lat, lng, cfg.snap_radius_m)
                    if pano:
                        uri, _ = view_to_data_uri(prov.capture(pano, a.bearing))
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
    print(f"멈춘 이유: {res.stop_reason}")
    print(f"스텝 {res.steps} · VLM 호출 {s.calls} · {res.wall_s:.0f}s"
          + (f" ({s.total_ms / s.calls / 1000:.2f}s/호출)" if s.calls else ""))
    # 조용히 깨지는 신호는 요약에 반드시 띄운다. 로그를 안 열어봐도 보이게.
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
        # 가지 않은 갈래. 버리지 않고 보여준다 — 분기 탐색을 붙일 때 이게 입력이다.
        print(f"\n가지 않은 산책로 갈래 {len(res.frontier)}개:")
        for f in res.frontier[:10]:
            print(f"  s{f['from_step']:>3} {f['from_pano']} → {f['heading']:6.1f}° "
                  f"{f['pano_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

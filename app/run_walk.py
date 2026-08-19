#!/usr/bin/env python
"""탐색 루프 한 번 실행.

    # 기본 설정 그대로 (app/config/trailwalk.yaml)
    python app/run_walk.py

    # 다른 설정으로
    python app/run_walk.py --config app/config/cheonggyecheon.yaml

**CLI 인자는 `--config` 하나뿐이다.** 좌표도, 예산도, 프롬프트도 전부 설정
파일에 있다 — 런 하나를 재현하려면 그 파일 하나만 있으면 된다는 뜻이다.
값을 바꾸려면 설정 파일을 복사해서 고친다 (→ CLAUDE.md "설정").

서버가 떠 있어야 한다: ./configs/smoke.sh
"""
import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import prompt as P
from trailwalk import providers, settings
from trailwalk.imaging import view_to_data_uri
from trailwalk.runlog import RunLog
from trailwalk.vlm import VlmClient
from trailwalk.walk import WalkConfig, walk

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
        APP / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{st.run.provider}.jsonl")

    cfg = WalkConfig.from_settings(st)
    client = VlmClient(url=st.vlm.url, schema_name=st.vlm.schema,
                       system_version=st.vlm.prompt_version, settings=st)
    try:
        prov = providers.make(st.run.provider, settings=st)
    except (providers.ProviderError, RuntimeError) as e:
        # 설정 문제(키·도메인·서비스 활성화)는 버그가 아니다. 스택트레이스를
        # 쏟아내면 정작 읽어야 할 안내가 묻힌다.
        print(f"✗ {e}", file=sys.stderr)
        if st.run.provider == "kakao":
            print("\n진단을 자세히 보려면: python app/check_kakao.py --headed", file=sys.stderr)
        return 2

    header = {"provider": prov.name, "schema": st.vlm.schema, "url": st.vlm.url,
              "start": [lat, lng], "start_bearing": bearing,
              "config": vars(cfg),
              # 어느 설정 파일로 돌았는지. 런로그만 보고 재현할 수 있어야 한다
              "config_path": str(Path(a.config).resolve() if a.config else settings.DEFAULT_PATH),
              "prompt": P.fingerprint(st.vlm.prompt_version)}

    print(f"provider={prov.name}  prompt={st.vlm.prompt_version}  schema={st.vlm.schema}  "
          f"start=({lat},{lng}) bearing={bearing}\n로그: {out}\n")
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
                res = walk(prov, client, (lat, lng), bearing, cfg, log)
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

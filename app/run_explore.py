#!/usr/bin/env python
"""분기 탐색 한 번 실행 — 시작점에서 뻗는 산책로를 전부 마킹한다.

    # 기본 설정 그대로 (app/config/trailwalk.yaml)
    python app/run_explore.py

    # 다른 설정으로
    python app/run_explore.py --config app/config/cheonggyecheon.yaml

**CLI 인자는 `--config` 하나뿐이다** (→ CLAUDE.md "설정").

서버가 떠 있어야 한다: ./configs/smoke.sh
"""
import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import prompt as P
from trailwalk import providers, settings
from trailwalk import warn as warn_mod
from trailwalk.explore import ExploreConfig, explore
from trailwalk.imaging import view_to_data_uri
from trailwalk.runlog import RunLog
from trailwalk.vlm import VlmClient

APP = Path(__file__).resolve().parent

# provider 이벤트 중 **결과 품질에 영향을 주는** 것들. 나머지는 런로그에만 남는다
WARN_KINDS = {"tiles_timeout", "render_unsettled"}

# 이 stop_reason 으로 끝난 런은 결과를 믿으면 안 된다 → stderr + exit 2.
# 무인 실행에서 exit 0 으로 끝나면 실패를 감지할 수단이 없다
FATAL = {"image_ignored", "server_dead", "vlm_error", "provider_error", "no_coverage"}


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
              # asdict 여야 한다 — cfg.image 는 중첩 dataclass 라 vars() 로는
              # 객체가 그대로 들어가고 런로그 첫 줄에서 죽는다 (실제로 죽었다)
              "config": asdict(cfg),
              # 어느 설정 파일로 돌았는지. 런로그만 보고 재현할 수 있어야 한다
              "config_path": str(Path(a.config).resolve() if a.config else settings.DEFAULT_PATH),
              "prompt": P.fingerprint(st.vlm.prompt_version)}

    # 건너뛰기가 켜져 있으면 **런 시작 때** 말한다. 판정이 성근 런이라는 사실이
    # yaml 주석에만 있으면 실행 시점에는 알 수 없고, 리포트를 정확도로 읽게 된다
    skip_note = (f"\n⚠  건너뛰기 켜짐 — 노드 {cfg.run_steps}개 찍고 {cfg.skip_steps}개 "
                 f"건너뛴다 (갈림길은 전부 찍는다).\n"
                 f"   반경 안 지면을 다 보지 않는다 — 정확도를 재는 런이면 "
                 f"설정에서 skip.skip_steps 를 0 으로 둘 것."
                 if cfg.skip_steps else "")
    print(f"provider={prov.name}  prompt={st.vlm.prompt_version}  schema={st.vlm.schema}  "
          f"start=({lat},{lng})  반경 {cfg.max_distance_m:.0f}m · 최대 {cfg.max_seconds:.0f}s"
          f"{skip_note}\n"
          f"로그: {out}\n")
    res = None
    # 루프가 모르는 신호(provider 렌더 품질, 클라이언트 캐시/파싱)를 여기서 모은다.
    # res.warnings 와 합쳐서 한 번에 찍는다 — 신호를 추가할 때마다 출력 블록을
    # 늘리던 자리다
    run_warnings: list[dict] = []

    def tally(log, code, **detail):
        """런로그와 stdout 이 같은 수를 보게 한다. count 규칙은 RunLog.tally 와 같다."""
        log.tally(code, **detail)
        n = int(detail.get("count", 1))
        for w in run_warnings:
            if w["code"] == code:
                merged = {**detail, "count": w["count"] + n}
                w.update(warn_mod.make(code, **merged))
                return
        run_warnings.append(warn_mod.make(code, **{**detail, "count": n}))

    try:
        with RunLog(out, header,
                    image_dir=(APP / "runs" / "images" / out.stem)
                    if st.run.save_images else None) as log:
            if hasattr(prov, "on_event"):
                # provider 쪽 신호도 런로그에 싣되, 결과 품질에 영향을 주는
                # 것들은 경고로도 올린다 — 루프는 provider 내부를 모른다
                def on_event(kind, **kw):
                    log.event(kind, **kw)
                    if kind in WARN_KINDS:
                        tally(log, kind)
                prov.on_event = on_event
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
                if s.cache_misses:
                    tally(log, "cache_miss", count=s.cache_misses, calls=s.calls)
                if s.parse_failures:
                    tally(log, "parse_failure", count=s.parse_failures)
                log.finish(stop_reason=res.stop_reason if res else "aborted",
                           nodes=len(res.nodes) if res else 0,
                           # 판정이 적은 이유가 "건너뛰어서" 인지 "못 받아서" 인지
                           # 런로그만 보고 알 수 있어야 한다
                           skipped=res.skipped if res else 0,
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
    print(f"노드 {len(res.nodes)}"
          + (f" (건너뜀 {res.skipped})" if res.skipped else "")
          + f" · 판정 {len(res.probes)} (산책로 {trails}) · "
          f"VLM 호출 {s.calls} · {res.wall_s:.0f}s"
          + (f" ({s.total_ms / s.calls / 1000:.2f}s/호출)" if s.calls else ""))
    for w in res.warnings + run_warnings:
        print(f"⚠  {w['message']}")
    if res.frontier:
        print(f"\n반경·예산에 걸려 못 간 갈래 {len(res.frontier)}개:")
        for f in res.frontier[:10]:
            print(f"  d{f['depth']:>2} {f['from_pano'] or '(시작)'} → "
                  f"{f['pano_id']}  [{f['reason']}]")

    if res.stop_reason in FATAL:
        # 스택트레이스 대신 읽을 수 있는 한 줄. providers.make() 실패가 이미
        # 쓰는 방식이다. 무인 실행에서 exit 0 으로 끝나면 실패를 감지할
        # 수단이 없다 — server_dead 는 사람이 서버를 재시작해야 하는 상태고,
        # image_ignored 는 판정이 전부 환각인 런이다
        msg = next((w["message"] for w in res.warnings
                    if w["code"] == res.stop_reason), res.stop_reason)
        print(f"✗ {msg}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

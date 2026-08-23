#!/usr/bin/env python
"""캡처만 모은다 — explore 와 같은 순서로 같은 화각을 찍되 VLM 에 묻지 않는다.

    python app/run_collect.py --config app/config/yaksu-collect.yaml

**CLI 인자는 `--config` 하나뿐이다** (→ CLAUDE.md "설정").

VLM 서버가 필요 없다. 서버가 내려가 있어도 돈다.

### 왜 나눠 찍나

판정 1건 = 캡처 + VLM 이고 캡처가 대부분을 먹는다. 2026-08-22 약수역 500m
런에서 2.40s/판정 중 서버 inference 2.10s 가 캡처 뒤에 숨어 사라졌다 —
그 상태로 VLM 을 재면 재고 있는 것은 VLM 이 아니라 Playwright 다.
그래서 캡처를 먼저 파일로 떠 두고, VLM 은 그 파일들로 따로 때린다.

### 왜 순서가 explore 와 같은가

`explore._candidates` 를 그대로 쓰고 큐·visited 규칙도 같다. 판정값은
탐색을 바꾸지 않으므로(→ explore.py "아님 판정도 확장한다", "큐는 하나다")
VLM 을 빼도 밟는 경로가 같다. 그래서 여기서 모은 N 장은 같은 설정의 explore
런이 서버로 보냈을 바로 그 N 장이다.

### 파일 바이트 = 전선 위 바이트

`imaging.view_to_data_uri` 를 통과시킨 뒤의 JPEG 를 저장한다. provider 원본
(PNG 스크린샷)이 아니다 — 리사이즈·크롭·품질이 전부 반영돼 있어야 이미지
토큰 수가 explore 와 같아진다 (264).

⚠️ **읽는 쪽은 이 파일을 imaging 에 다시 넣지 말 것.** 그대로 base64 해서
보내면 explore 가 보냈을 것과 바이트 단위로 같다. 다시 통과시키면 이미 JPEG
인 것을 디코드해서 다시 JPEG 로 굽는 것이라(세대 손실) 바이트가 달라진다 —
실측으로 확인했다. 토큰 수는 같지만 "같은 바이트를 보냈다" 가 깨진다.
"""
import argparse
import base64
import hashlib
import json
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import geo, providers, settings
from trailwalk.explore import ExploreConfig, _candidates
from trailwalk.imaging import view_to_data_uri
from trailwalk.providers.base import Pano, ProviderError


def _wire_bytes(raw: bytes, image) -> tuple[bytes, str]:
    """캡처 원본 → 서버로 나갈 JPEG 바이트. (바이트, 원본포맷)"""
    uri, src_format = view_to_data_uri(raw, image)
    return base64.b64decode(uri.split(",", 1)[1]), src_format


def walk(provider, cfg, start_pano, bearing: float,
         on_view, deadline: float) -> dict:
    """explore 와 **같은 순서로** 후보를 밟으며 `on_view` 를 부른다.

    캡처도 파일 쓰기도 여기서 안 한다 — 그건 `on_view` 의 몫이다. 순서를
    정하는 일만 여기 있어야 explore 와 같은지를 테스트가 확인할 수 있다
    (→ tests/test_collect.py).

    `on_view(pano, heading, nb, depth)` 는 **캡처에 성공했는지**를 돌려준다.
    False 면 그 갈래는 큐에 넣지 않는다 — explore 도 캡처 실패한 갈래는
    확장하지 않는다 (판정이 없으니 갈래를 밟은 것이 아니다).

    **장수로는 안 멈춘다.** 멈추는 조건은 explore 와 같은 둘뿐이다 —
    반경(`cfg.max_distance_m`)과 벽시계(`deadline`). 장수 상한을 두면 이
    스크립트의 존재 이유("explore 가 보냈을 바로 그 N 장")가 깨진다:
    2026-08-23 GS25 반경 500m 수집이 1000장에서 끊겨 398m 까지밖에 못 갔고,
    그건 500m 를 모은 것이 아니었다.

    돌려주는 dict: stop / views / capture_failed / neighbors_missing.
    """
    origin = (start_pano.lat, start_pano.lng)
    # explore 와 같은 자료구조 — 하나의 FIFO 큐, pano_id 로 visited
    q = deque([(0, geo.norm_deg(bearing), None, start_pano)])
    visited = {start_pano.pano_id}
    n = capture_failed = neighbors_missing = 0
    stop = "exhausted"

    done = False
    while q and not done:
        if time.time() > deadline:
            stop = "time_budget"
            break
        depth, brg, came_from, pano = q.popleft()

        if geo.haversine_m(origin, (pano.lat, pano.lng)) > cfg.max_distance_m:
            continue        # 반경 밖은 확장하지 않는다 (explore 와 같다)

        cands, loaded = _candidates(provider, pano, brg, came_from, visited, cfg)
        if not loaded:
            # 이웃 목록을 못 얻었다 — 갈래가 없는 게 아니라 렌더/스니핑 실패다
            neighbors_missing += 1
            continue

        for hdg, nb in cands:
            # 예산은 노드 경계가 아니라 **후보마다** 본다 — 한 지점이 최대
            # max_candidates 장이라 경계에서만 보면 통째로 넘겨서 찍는다
            if time.time() > deadline:
                stop, done = "time_budget", True
                break
            if not on_view(pano, hdg, nb, depth):
                capture_failed += 1
                continue
            n += 1
            # 첫 접근의 화각이 그 pano 의 것이다 — explore 와 같은 규칙
            visited.add(nb.pano_id)
            q.append((depth + 1, hdg, pano.pano_id,
                      Pano(pano_id=nb.pano_id, lat=nb.lat, lng=nb.lng)))

    return {"stop": stop, "views": n, "capture_failed": capture_failed,
            "neighbors_missing": neighbors_missing}


# 이 이유로 끊긴 런은 종료 코드 2 다 (→ main 끝). run_explore.py 와 같은 규칙.
FATAL = {"provider_error"}


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

    cfg = ExploreConfig.from_settings(st)
    lat, lng = st.run.start
    out_dir = Path(st.collect.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "views.jsonl"

    try:
        prov = providers.make(st.run.provider, settings=st)
    except (providers.ProviderError, RuntimeError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    print(f"provider={prov.name}  start=({lat},{lng})  "
          f"반경 {cfg.max_distance_m:.0f}m · 최대 {cfg.max_seconds:.0f}s\n"
          f"폴더: {out_dir}\n")

    t0 = time.time()
    mf = manifest.open("w", encoding="utf-8")
    n = 0
    try:
        mf.write(json.dumps({
            "type": "header",
            "provider": prov.name,
            "start": [lat, lng], "start_bearing": st.run.bearing,
            "created_at": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}",
            "config_path": str(Path(a.config).resolve() if a.config
                               else settings.DEFAULT_PATH),
            "max_distance_m": cfg.max_distance_m,
            "max_candidates": cfg.max_candidates,
            # 이 세 값이 이미지 토큰 수를 정한다. 나중에 재현하려면 필요하다
            "target_size": list(st.image.target_size),
            "jpeg_quality": st.image.jpeg_quality,
            "expected_image_tokens": st.image.expected_image_tokens,
            "note": "각 파일은 서버로 나갈 JPEG 그대로다 — 그대로 base64 해서 "
                    "보낼 것. imaging 에 다시 넣으면 재압축되어 바이트가 달라진다",
        }, ensure_ascii=False) + "\n")
        mf.flush()

        try:
            start_pano = prov.nearest(lat, lng, cfg.snap_radius_m)
        except Exception as e:
            print(f"✗ provider_error: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        if start_pano is None:
            print(f"✗ no_coverage: {cfg.snap_radius_m:.0f}m 안에 로드뷰가 없다",
                  file=sys.stderr)
            return 2

        def on_view(pano, hdg, nb, depth) -> bool:
            nonlocal n
            try:
                raw = prov.capture(pano, hdg)
            except Exception:
                return False
            data, src_format = _wire_bytes(raw, st.image)
            n += 1
            name = f"{n:04d}_s{depth:02d}_{pano.pano_id}_{hdg:05.1f}.jpg"
            (out_dir / name).write_bytes(data)
            mf.write(json.dumps({
                "type": "view", "i": n, "file": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "src_format": src_format,
                "pano_id": pano.pano_id,
                "lat": round(pano.lat, 7), "lng": round(pano.lng, 7),
                "captured_at": pano.captured_at,
                "heading": round(hdg, 1), "depth": depth,
                "to_pano": nb.pano_id,
                "to_lat": round(nb.lat, 7), "to_lng": round(nb.lng, 7),
                "road": nb.name,
            }, ensure_ascii=False) + "\n")
            mf.flush()      # 중간에 죽어도 여기까지는 쓸 수 있는 대장이 남는다
            if n % 25 == 0:
                el = time.time() - t0
                print(f"  {n:>4}장 · {el:.0f}s · {el / n:.2f}s/장", flush=True)
            return True

        try:
            r = walk(prov, cfg, start_pano, st.run.bearing,
                     on_view, deadline=t0 + cfg.max_seconds)
        except ProviderError as e:
            print(f"✗ provider_error: {e}", file=sys.stderr)
            r = {"stop": "provider_error", "views": n,
                 "capture_failed": 0, "neighbors_missing": 0}
    finally:
        mf.close()
        prov.close()

    el = time.time() - t0
    print(f"\n멈춘 이유: {r['stop']}")
    print(f"{n}장 · {el:.0f}s" + (f" ({el / n:.2f}s/장)" if n else ""))
    if r["capture_failed"]:
        print(f"⚠  캡처 실패 {r['capture_failed']}건")
    if r["neighbors_missing"]:
        print(f"⚠  이웃 목록 실패 {r['neighbors_missing']}개 지점 — 렌더/스니핑 실패다")
    print(f"대장: {manifest}")
    # 몇 장 모았는지와 무관하게 **끊긴 런은 실패다.** 500장에서 브라우저가
    # 죽은 것과 1000장을 다 모은 것이 둘 다 exit 0 이면 무인 실행에서 구별할
    # 수단이 없다 — run_explore.py 의 FATAL 과 같은 규칙이다.
    if r["stop"] in FATAL:
        return 2
    return 0 if n else 2


if __name__ == "__main__":
    raise SystemExit(main())

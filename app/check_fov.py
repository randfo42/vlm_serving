#!/usr/bin/env python
"""화각 진단 — `zoom 0` 이 실제로 몇 도인가. VLM 불필요.

23-open-questions.md §3 을 닫기 위한 도구다. Kakao 의 `zoom` 은 −3~3 의 이산
배율이라 "90도" 를 지정할 방법이 없고, `zoom 0` 이 몇 도인지는 문서에 없다.
그런데 `side_offsets`(폴백의 좌우 후보각)와 판정의 의미가 여기 걸려 있다.

    python app/check_fov.py                      # 청계천 보행로 기본 좌표
    python app/check_fov.py --start 37.57,127.00 --out /tmp/fov

두 가지를 한다.

### 1. 재는 것 — 회전량 대비 픽셀 이동

정사영(rectilinear) 화면에서 중심으로부터 각 θ 인 점은 x = cx + f·tan(θ) 에
찍힌다. 카메라를 오른쪽으로 Δ 만큼 돌리면 화면 중앙에 있던 것이
x = cx − f·tan(Δ) 로 간다. 그 이동량을 재면 f 가 나오고, 화각은

    fov = 2·atan(W / 2f)

Δ 를 여러 개 쓰는 이유는 **모델이 맞는지 같이 보기 위해서**다. Δ 가 달라도
f 가 일정해야 정사영이다. 값이 흐르면 투영이 다른 것이므로 숫자만 믿으면 안 된다.

화살표는 이 측정에서 **지운다**(`hide_arrows`). 화살표는 세상에 붙어 있지 않고
UI 규칙으로 놓이므로 — 뒤쪽 화살표는 화면 하단에 고정된다 — 상관관계를 망친다.

### 2. 보여주는 것 — 눈으로 판단할 스윕

이미지를 디스크에 남긴다. 자동 측정이 틀렸을 때 그걸 알 방법은 결국 사람이
보는 것뿐이다. 화살표를 켠 스윕도 같이 저장해서 **화살표 방향과 그림이
맞는지** 확인할 수 있게 한다 (이웃의 정확한 방위각은 표로 같이 준다).

⚠️ 이미지 저장은 Kakao 운영정책상 회색지대다 (23-open-questions.md §2).
로컬 진단 용도로만 쓰고, 기본 저장 위치(app/runs/images/)는 gitignore 아래다.
"""
from __future__ import annotations

import argparse
import io
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trailwalk import config
from trailwalk.providers.kakao import VIEW_H, VIEW_W, KakaoProvider

# 청계천 보행로. 도보 촬영 계열(103959…)이라 좌우로 길이 이어져 있다 —
# 회전시켜도 특징이 남아 있어야 상관관계가 잡힌다.
DEFAULT_START = (37.5697109, 127.0063616)

# 측정에 쓸 회전량(도). 작은 값은 tan 이 작아 픽셀 오차에 민감하고, 큰 값은
# 시차(parallax)와 프레임 이탈이 커진다. 그 사이를 훑는다.
DELTAS = (4.0, 8.0, 12.0, 16.0, 20.0, 25.0, 30.0)

# 종횡비 비교용 뷰포트. 세로를 720 으로 묶고 가로만 바꾸면 "세로 화각 고정,
# 가로만 종횡비를 따라간다" 가 그림으로 바로 보인다. 1280×720 이 현재 설정이다.
VIEWPORTS = ((640, 720), (960, 720), (VIEW_W, VIEW_H), (1920, 720), (2560, 720))

# 템플릿을 뜰 세로 구간. 하늘(위)과 발밑 노면(아래)은 특징이 없어서 못 쓴다.
BAND = (0.30, 0.60)
TPL_HALF_W = 90        # 템플릿 가로 반폭(px)


def _gray(raw: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(raw)).convert("L"), dtype=np.float64)


def _match_x(base: np.ndarray, target: np.ndarray) -> tuple[float, float]:
    """base 중앙에서 뜬 템플릿이 target 의 어느 x 에 있는가. (x_center, NCC 점수)."""
    h = base.shape[0]
    y0, y1 = int(h * BAND[0]), int(h * BAND[1])
    cx = base.shape[1] // 2
    tpl = base[y0:y1, cx - TPL_HALF_W: cx + TPL_HALF_W]
    tpl = tpl - tpl.mean()
    tnorm = np.sqrt((tpl * tpl).sum())
    if tnorm == 0:
        return float("nan"), 0.0

    band = target[y0:y1]
    tw = tpl.shape[1]
    best_x, best_s = -1, -2.0
    for x in range(0, band.shape[1] - tw + 1):
        win = band[:, x:x + tw]
        win = win - win.mean()
        wnorm = np.sqrt((win * win).sum())
        if wnorm == 0:
            continue
        s = float((win * tpl).sum() / (wnorm * tnorm))
        if s > best_s:
            best_s, best_x = s, x
    return best_x + tw / 2, best_s


def measure(prov: KakaoProvider, pano, axis: float, outdir: Path,
            prefix: str = "measure") -> float | None:
    """가로 화각(도)을 돌려준다. 못 재면 None."""
    print(f"\n── 측정 (화살표 끔) · 기준 방위 {axis:.1f}° · "
          f"뷰포트 {prov.view_w}×{prov.view_h} ──")
    base_raw = prov.capture(pano, axis)
    (outdir / f"{prefix}_d00.png").write_bytes(base_raw)
    base = _gray(base_raw)
    cx = base.shape[1] / 2

    print(f"{'Δ회전':>6} {'이동 px':>9} {'NCC':>6} {'f(px)':>9} {'추정 화각':>10}")
    print("─" * 46)
    fovs = []
    for d in DELTAS:
        raw = prov.capture(pano, axis + d)
        (outdir / f"{prefix}_d{int(d):02d}.png").write_bytes(raw)
        x, score = _match_x(base, _gray(raw))
        shift = cx - x
        if shift <= 0 or score < 0.5:
            print(f"{d:6.0f}° {shift:9.1f} {score:6.2f}   — 매칭 실패/부호 이상, 버린다")
            continue
        f = shift / math.tan(math.radians(d))
        fov = 2 * math.degrees(math.atan(prov.view_w / (2 * f)))
        fovs.append(fov)
        print(f"{d:6.0f}° {shift:9.1f} {score:6.2f} {f:9.1f} {fov:9.1f}°")

    if fovs:
        med = sorted(fovs)[len(fovs) // 2]
        spread = max(fovs) - min(fovs)
        print(f"\n  가로 화각 중앙값 **{med:.1f}°**  (편차 {spread:.1f}°, {len(fovs)}개)")
        vfov = 2 * math.degrees(math.atan(
            (prov.view_h / prov.view_w) * math.tan(math.radians(med / 2))))
        print(f"  세로 화각 {vfov:.1f}°")
        if spread > 8:
            print("  ⚠️ Δ 마다 값이 흐른다 — 정사영 가정이 안 맞는다. 그림을 직접 볼 것")
        return med
    print("\n  ✗ 전부 실패. 특징이 없는 장면일 수 있다 — 다른 좌표로 다시.")
    return None


def arrows(prov: KakaoProvider, pano, outdir: Path) -> None:
    """화살표를 켠 스윕. 이웃의 정확한 방위와 화면 위치를 같이 남긴다."""
    nbrs = prov.neighbors(pano)
    print(f"\n── 이웃 {len(nbrs)}개 (노드 JSON 의 spot[] = 정확한 방위각) ──")
    for n in nbrs:
        print(f"   {n.heading:7.2f}°  → {n.pano_id}  {n.name or ''}")

    js = """() => {
      const out = [];
      document.querySelectorAll('#rv [id^="_at_"]').forEach(el => {
        const cs = getComputedStyle(el);
        const m = /matrix\\(1, 0, 0, 1, ([-\\d.]+), ([-\\d.]+)\\)/.exec(cs.transform || '');
        out.push({label: (el.textContent||'').trim(), shown: cs.display !== 'none',
                  x: m ? parseFloat(m[1]) : null, y: m ? parseFloat(m[2]) : null});
      });
      return out;
    }"""

    axis = nbrs[0].heading if nbrs else 0.0
    print(f"\n── 스윕 (화살표 켬) · 기준 {axis:.1f}° ──")
    print(f"{'pan':>7}  화면에 뜬 화살표 (라벨@x)")
    print("─" * 60)
    for off in (-90, -60, -30, 0, 30, 60, 90):
        pan = (axis + off) % 360
        raw = prov.capture(pano, pan)
        (outdir / f"sweep_{off:+04d}.png").write_bytes(raw)
        shown = [e for e in prov._page.evaluate(js) if e["shown"] and e["x"] is not None]
        cells = "  ".join(f"{e['label']}@{e['x']:.0f}" for e in shown) or "(없음)"
        print(f"{pan:6.1f}°  {cells}")


def viewports(key: str, lat: float, lng: float, radius: float,
              outdir: Path, headless: bool) -> None:
    """뷰포트를 바꿔가며 같은 자리·같은 방위를 찍는다.

    `zoom` 은 고정이다. 그런데도 담기는 세상의 양이 달라진다면 화각을 정하는
    것은 zoom 이 아니라 **뷰포트 종횡비**다 — 눈으로 확인할 수 있게 남긴다.
    """
    print("\n── 뷰포트별 (zoom 0 고정, 같은 pano·같은 방위) ──")
    print(f"{'뷰포트':>12} {'종횡비':>7} {'가로 화각':>10} {'세로 화각':>10}   파일")
    print("─" * 62)
    for w, h in VIEWPORTS:
        prov = KakaoProvider(key, headless=headless, hide_arrows=True,
                             view_w=w, view_h=h)
        try:
            pano = prov.nearest(lat, lng, radius)
            if pano is None:
                print(f"{w:>5}×{h:<6} 로드뷰 없음")
                continue
            nbrs = prov.neighbors(pano)
            axis = nbrs[0].heading if nbrs else 0.0
            name = f"viewport_{w}x{h}.png"
            (outdir / name).write_bytes(prov.capture(pano, axis))

            # 화각은 회전 이동으로 다시 잰다 (Δ 세 개면 충분하다)
            base = _gray((outdir / name).read_bytes())
            cx = base.shape[1] / 2
            fovs = []
            for d in (8.0, 16.0, 25.0):
                x, score = _match_x(base, _gray(prov.capture(pano, axis + d)))
                shift = cx - x
                if shift > 0 and score >= 0.5:
                    fcal = shift / math.tan(math.radians(d))
                    fovs.append(2 * math.degrees(math.atan(w / (2 * fcal))))
            if fovs:
                hf = sorted(fovs)[len(fovs) // 2]
                vf = 2 * math.degrees(math.atan((h / w) * math.tan(math.radians(hf / 2))))
                mark = "  ← 현재 설정" if (w, h) == (VIEW_W, VIEW_H) else ""
                print(f"{w:>5}×{h:<6} {w / h:7.2f} {hf:9.1f}° {vf:9.1f}°   {name}{mark}")
            else:
                print(f"{w:>5}×{h:<6} {w / h:7.2f}   측정 실패          {name}")
        finally:
            prov.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default=None, help="lat,lng (기본: 청계천 보행로)")
    ap.add_argument("--out", default=None, help="이미지 저장 폴더 (기본: app/runs/images/fov)")
    ap.add_argument("--radius", type=float, default=25.0)
    ap.add_argument("--headed", action="store_true", help="브라우저를 띄워 눈으로 본다")
    ap.add_argument("--viewports", action="store_true",
                    help="뷰포트(종횡비)를 바꿔가며 찍는다. 화각을 정하는 것이 "
                         "zoom 이 아니라 종횡비임을 눈으로 확인하는 용도")
    a = ap.parse_args()

    lat, lng = ((float(x) for x in a.start.split(",")) if a.start else DEFAULT_START)
    outdir = Path(a.out) if a.out else Path(__file__).resolve().parent / "runs" / "images" / "fov"
    outdir.mkdir(parents=True, exist_ok=True)

    config.load_env()
    key = config.kakao_appkey()

    # 측정과 스윕은 **화살표 유무가 달라야** 해서 세션을 나눈다. hide_arrows 는
    # 페이지 CSS 에 박히므로 도중에 못 바꾼다.
    prov = KakaoProvider(key, headless=not a.headed, hide_arrows=True)
    try:
        pano = prov.nearest(lat, lng, a.radius)
        if pano is None:
            print(f"✗ ({lat}, {lng}) 반경 {a.radius:.0f}m 안에 로드뷰가 없다.")
            return 1
        print(f"pano {pano.pano_id}  ({pano.lat:.7f}, {pano.lng:.7f})")
        nbrs = prov.neighbors(pano)
        axis = nbrs[0].heading if nbrs else 0.0
        measure(prov, pano, axis, outdir)
    finally:
        prov.close()

    prov = KakaoProvider(key, headless=not a.headed, hide_arrows=False)
    try:
        pano = prov.nearest(lat, lng, a.radius)
        prov.capture(pano, 0.0)      # 띄워야 노드 응답이 온다 (§7)
        arrows(prov, pano, outdir)
    finally:
        prov.close()

    if a.viewports:
        viewports(key, lat, lng, a.radius, outdir, not a.headed)

    print(f"\n이미지 {len(list(outdir.glob('*.png')))}장 → {outdir}")
    print("  measure_d??.png  회전량 ?도. 같은 장면이 얼마나 흘렀는지 보는 용도")
    print("  sweep_+000.png   진행 방위 정면. ±30/±60/±90 이 좌우 스윕")
    if a.viewports:
        print("  viewport_WxH.png 같은 자리·같은 방위를 뷰포트만 바꿔 찍은 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

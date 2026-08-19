"""탐색 루프 — 로드뷰를 따라 산책로를 이어간다.

VLM 에게 묻는 것은 오직 **"이 장면이 산책로인가"** 하나다. 어디로 갈지, 언제
멈출지, 갈림길에서 무엇을 고를지는 전부 여기서 정한다. 서빙 쪽이 그은 경계다
(docs/00-design.md §1).

### 한 스텝의 모양

    현재 pano
      → 이웃 목록 (화면의 흰 화살표와 같은 것). 온 길과 이미 밟은 곳은 뺀다
      → 진행 방향에 가까운 순으로 정렬 — 맨 앞이 "정면"
      → 후보를 한 장씩 캡처해 VLM 에 묻는다 (기본: 전부. probe_all=False 면 첫 성공까지)
      → 산책로 중 **가장 정면인 것**으로 그 pano 로 바로 이동
      → 나머지 산책로 갈래는 frontier 에 남긴다 — 여러 방향이 동시에 산책로일 수 있다

### 왜 이웃 그래프인가

처음엔 이웃 목록을 얻을 방법이 없다고 보고 좌표를 직접 미는 방식으로 짰다
(heading 방향 12m 전진 → 가장 가까운 pano 로 스냅). 그런데 로드뷰 화면에 그려지는
흰 화살표가 바로 그 정보였고, 데이터로 꺼낼 수 있었다
(→ `providers/kakao.py` `neighbors()`, `docs/21-roadview-providers.md` §1.3).

그래프가 좌표 밀기보다 나은 점:

- **보폭을 정할 필요가 없다.** 촬영 간격이 곧 보폭이다. 12m 로 밀다가 촬영
  간격이 5m 인 구간에서 pano 를 건너뛰거나, 20m 인 구간에서 제자리를 밟는 일이 없다
- **방위가 정확하다.** 이웃마다 실제 방위각(91.36° 같은)이 딸려 온다.
  "정면" 이 추정이 아니라 정의가 된다
- **온 길을 정확히 뺄 수 있다.** 각도로 어림하지 않고 pano_id 로 지운다
- **막다른 길이 명확하다.** 이웃이 없으면 없는 것이다. "스냅이 실패했나
  길이 끝났나" 를 구분할 필요가 없다

이동은 이 그래프 **하나뿐이다.** 좌표를 heading 방향으로 밀어 다시 스냅하던
폴백은 없앴다 — 이웃을 못 얻으면 지어내지 않고 `neighbors_missing` 으로
멈춘다. fixture 도 격자를 그래프로 정직하게 준다 (→ providers/fixture.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import geo, settings
from .explore import _candidates
from .imaging import view_to_data_uri
from .providers.base import Neighbor, Pano
from .vlm import ImageIgnoredError, ServerDeadError, VlmError


@dataclass
class WalkConfig:
    """탐색 정책. **기본값은 여기가 아니라 app/config/trailwalk.yaml 에 있다.**

    필드에 기본값을 다시 적으면 정본이 둘이 된다 — 어느 쪽이 먹는지 코드를
    읽어야 알게 되고, 그게 정확히 이 리팩터링이 없앤 문제다. 그래서 여기는
    자료구조일 뿐이고, 값은 `from_settings()` 가 YAML 에서 채운다.
    """
    max_candidates: int
    max_steps: int
    probe_all: bool
    max_vlm_calls: int
    max_seconds: float
    miss_tolerance: int
    snap_radius_m: float

    # 캡처한 바이트를 서버로 보낼 때의 규칙 (크기·품질). 루프가 imaging 에
    # 넘긴다 — 모듈 상수로 읽게 두면 --config 가 무시된다
    image: object

    @classmethod
    def from_settings(cls, s) -> WalkConfig:
        return cls(
            max_candidates=s.candidates.max_candidates,
            max_steps=s.budget.walk_max_steps,
            probe_all=s.candidates.probe_all,
            max_vlm_calls=s.budget.max_vlm_calls,
            max_seconds=s.budget.max_seconds,
            miss_tolerance=s.candidates.miss_tolerance,
            snap_radius_m=s.geo.snap_radius_m,
            image=s.image,
        )


@dataclass
class WalkResult:
    path: list[dict] = field(default_factory=list)   # 실제로 밟은 pano 들
    stop_reason: str = ""
    steps: int = 0
    calls: int = 0
    wall_s: float = 0.0

    # 산책로라고 판정됐지만 가지 않은 갈래들. 한 지점에서 여러 방향이 동시에
    # 산책로일 수 있고(갈림길이 그런 곳이다), 우리는 그중 하나만 따라간다.
    # 나머지를 버리지 않고 남긴다 — 나중에 분기 탐색을 붙일 때 이게 그대로
    # 프론티어가 된다 (→ docs/23-open-questions.md §8).
    frontier: list[dict] = field(default_factory=list)


def walk(provider, client, start: tuple[float, float], start_bearing: float,
         cfg: WalkConfig | None = None, log=None) -> WalkResult:
    """start 에서 start_bearing 방향으로 출발해 산책로를 따라간다."""
    # 기본 인자로 WalkConfig(...) 를 두면 인스턴스 하나가 호출 간에 공유된다.
    # 지금은 아무도 cfg 를 고치지 않지만, 고치는 순간 다음 런에 조용히 새어 간다.
    cfg = cfg or WalkConfig.from_settings(settings.SETTINGS)
    res = WalkResult()
    t0 = time.time()
    bearing = geo.norm_deg(start_bearing)
    pos = start
    pano: Pano | None = None
    came_from: str | None = None
    visited: set[str] = set()
    misses = 0

    def probe(p: Pano, hdg: float, step: int) -> bool | None:
        """한 방향을 물어본다. None 은 '판정 불가' (캡처 실패)."""
        try:
            raw = provider.capture(p, hdg)
        except Exception as e:
            if log:
                log.event("capture_failed", step=step, pano_id=p.pano_id,
                          heading=round(hdg, 1), error=f"{type(e).__name__}: {e}")
            return None
        uri, src_format = view_to_data_uri(raw, cfg.image)
        v = client.assess(uri, heading=hdg)
        res.calls += 1
        if log:
            log.probe(step=step, pano_id=p.pano_id, lat=p.lat, lng=p.lng,
                      heading=hdg, verdict=v, src_format=src_format, image=raw)
        return v.is_trail

    while True:
        # 예산 셋. 실질 상한은 호출 수다 — 갈림길이 많으면 같은 걸음 수라도
        # 호출이 몇 배가 되므로, max_steps 만으로는 비용이 안 보인다.
        # explore 가 같은 이유로 호출 수를 쓴다 (두 루프의 예산을 비교 가능하게).
        if res.calls >= cfg.max_vlm_calls:
            res.stop_reason = "call_budget"; break
        if res.steps >= cfg.max_steps:
            res.stop_reason = "max_steps"; break
        if time.time() - t0 > cfg.max_seconds:
            res.stop_reason = "time_budget"; break

        # ── 현재 pano 확정 ────────────────────────────────────────────
        # 그래프 이동이면 이미 정해져 있다. 최초 스텝만 좌표에서 스냅한다.
        if pano is None:
            try:
                pano = provider.nearest(pos[0], pos[1], cfg.snap_radius_m)
            except Exception as e:
                if log:
                    log.event("provider_error", step=res.steps,
                              error=f"{type(e).__name__}: {str(e).splitlines()[0]}")
                res.stop_reason = "provider_error"; break
            if pano is None:
                # 로드뷰가 없는 구간. 산책로가 끝난 것과 구분되지 않는다 —
                # 판정 정확도를 볼 때 이 둘을 섞지 말 것.
                res.stop_reason = "no_coverage"
                if log:
                    log.event("no_coverage", step=res.steps, lat=pos[0], lng=pos[1])
                break

        # 재방문 감지가 여기 있었다. 지웠다 — 이제 **구조적으로 불가능**하기 때문이다.
        # `_candidates` 가 visited 를 후보에서 빼므로 다음 지점은 항상 처음 밟는
        # 곳이다. 같은 pano 로 되돌아올 수 있었던 것은 좌표 밀기가 근처 pano 로
        # 다시 스냅했기 때문이고, 그 이동을 없앴다 (→ docs/20-app-design.md §3).
        visited.add(pano.pano_id)

        # ── 방향 결정 ──────────────────────────────────────────────────
        cands, loaded = _candidates(provider, pano, bearing, came_from, visited, cfg)
        if not loaded:
            # 이웃 목록을 못 얻었다. 갈래가 없는 게 아니라 렌더/스니핑 실패다.
            # 예전에는 여기서 좌표 밀기로 되돌아갔는데, 그러면 없는 길을 지어내면서
            # 런은 멀쩡해 보인다 — 가장 나쁜 종류의 실패다. 멈추고 이름을 붙인다.
            res.stop_reason = "neighbors_missing"
            if log:
                log.event("neighbors_missing", step=res.steps, pano_id=pano.pano_id)
            break
        if not cands:
            res.stop_reason = "dead_end"
            if log:
                log.event("no_candidates", step=res.steps, pano_id=pano.pano_id)
            break

        probe_all = cfg.probe_all
        chosen: tuple[float, Neighbor] | None = None
        oks: list[tuple[float, Neighbor]] = []
        judged: list[tuple[float, Neighbor]] = []   # 판정을 실제로 받은 후보 ("아님" 포함)
        failed = 0
        budget_hit = False
        try:
            for i, (hdg, nb) in enumerate(cands):
                # 예산은 **후보마다** 본다. 스텝 경계에서만 보면 하드 캡이 아니다 —
                # 시작 노드는 max_candidates 슬라이싱도 안 받으므로(_candidates),
                # 이웃이 4개면 max_vlm_calls=1 이어도 4번 부르고 나서야 멈춘다.
                if res.calls >= cfg.max_vlm_calls:
                    res.stop_reason = "call_budget"
                    # 못 물은 후보는 버리지 않는다 — 판정을 안 받았을 뿐 갈래는
                    # 갈래다 (explore 와 같은 규칙)
                    for _h2, n2 in cands[i:]:
                        res.frontier.append({
                            "from_pano": pano.pano_id, "from_step": res.steps,
                            "heading": round(_h2, 1), "pano_id": n2.pano_id,
                            "lat": n2.lat, "lng": n2.lng})
                    budget_hit = True
                    break
                ok = probe(pano, hdg, res.steps)
                if ok is None:
                    # 판정 불가(캡처 실패). "아님" 이 아니라 API 쪽 실패다 —
                    # miss 로 세면 렌더 장애가 dead_end 로 둔갑한다
                    failed += 1
                    continue
                judged.append((hdg, nb))
                if ok:
                    oks.append((hdg, nb))
                    if not probe_all:
                        break       # 정면에 가까운 쪽부터 봤으니 첫 성공이 최선이다
        except ImageIgnoredError:
            res.stop_reason = "image_ignored"; raise
        except ServerDeadError:
            res.stop_reason = "server_dead"; raise
        except VlmError as e:
            if log:
                log.event("vlm_error", step=res.steps, error=str(e)[:400])
            res.stop_reason = "vlm_error"; break

        if budget_hit:
            # 예산이 끝났다. 반쪽짜리 후보 목록으로 고르면 "가장 정면인 산책로"
            # 가 아니게 되므로 이 스텝은 마무리하지 않는다.
            #
            # 다만 **이미 산책로로 확인된 갈래는 반드시 남긴다.** 호출을 써서
            # 얻은 판정이고, frontier 의 계약이 "산책로인데 안 간 갈래" 다.
            # 여기서 빠뜨리면 돈 주고 산 판정이 로그 어디에도 안 남는다.
            for hdg, nb in oks:
                res.frontier.append({
                    "from_pano": pano.pano_id, "from_step": res.steps,
                    "heading": round(hdg, 1), "pano_id": nb.pano_id,
                    "lat": nb.lat, "lng": nb.lng})
            break

        if oks:
            chosen = oks[0]         # 가장 정면에 가까운 산책로

        # 갈림길: 산책로가 둘 이상이면 하나만 따라가고 나머지는 남긴다.
        # 여기가 "여러 방향이 동시에 산책로일 수 있다" 를 실제로 담는 곳이다.
        if len(oks) > 1:
            for hdg, nb in oks[1:]:
                res.frontier.append({
                    "from_pano": pano.pano_id, "from_step": res.steps,
                    "heading": round(hdg, 1), "pano_id": nb.pano_id,
                    "lat": nb.lat, "lng": nb.lng,
                })
            if log:
                log.event("branch", step=res.steps, pano_id=pano.pano_id,
                          taken=round(oks[0][0], 1),
                          left=[round(h, 1) for h, _ in oks[1:]])

        res.path.append({"pano_id": pano.pano_id, "lat": pano.lat, "lng": pano.lng,
                         "bearing": round(bearing, 1), "is_trail": chosen is not None,
                         "n_candidates": len(cands), "n_trails": len(oks),
                         "n_failed": failed})    # 판정 불가(캡처 실패) 후보 수

        if chosen is None and not judged:
            # 어느 후보도 판정을 못 받았다. "어느 방향도 산책로가 아니다" 가
            # 아니라 **아무것도 알아내지 못한 것**이다 — 모르는 채 한 칸 밀면
            # 예전의 캡처실패=아님 혼동이 되돌아온다. API 실패로 이름 붙여 멈춘다.
            res.stop_reason = "capture_failed"
            if log:
                log.event("all_captures_failed", step=res.steps, pano_id=pano.pano_id)
            break

        if chosen is None:
            misses += 1
            if misses > cfg.miss_tolerance:
                res.stop_reason = "dead_end"; break
            # 아직 참는다: **판정을 받은** 후보 중 가장 정면인 것으로 한 칸
            # 밀어보고 다시 판단한다. 나무 그늘이나 역광 한 장 때문에 멀쩡한
            # 길을 포기하는 일이 실제로 있다. cands[0] 을 그대로 쓰면 안 된다 —
            # 캡처가 실패한(판정 없는) 후보일 수 있고, 그러면 모르는 채 걷는다.
            chosen = judged[0]
        else:
            misses = 0

        # ── 이동 ───────────────────────────────────────────────────────
        hdg, nb = chosen
        bearing = hdg
        came_from = pano.pano_id
        # 좌표를 밀 필요도, 다시 스냅할 필요도 없다. 이웃이 곧 다음 지점이다.
        pano = Pano(pano_id=nb.pano_id, lat=nb.lat, lng=nb.lng)
        res.steps += 1

    res.wall_s = time.time() - t0
    return res

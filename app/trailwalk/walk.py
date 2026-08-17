"""탐색 루프 — 로드뷰를 따라 산책로를 이어간다.

VLM 에게 묻는 것은 오직 **"이 장면이 산책로인가"** 하나다. 어디로 갈지, 언제
멈출지, 갈림길에서 무엇을 고를지는 전부 여기서 정한다. 서빙 쪽이 그은 경계다
(docs/00-design.md §1).

### 한 스텝의 모양

    현재 pano
      → 이웃 목록 (화면의 흰 화살표와 같은 것). 온 길과 이미 밟은 곳은 뺀다
      → 진행 방향에 가까운 순으로 정렬 — 맨 앞이 "정면"
      → 후보를 한 장씩 캡처해 VLM 에 묻는다 (그래프면 전부, 폴백이면 첫 성공까지)
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

이웃을 못 주는 provider(fixture 등)에서는 **자동으로 좌표 밀기로 되돌아간다.**
두 방식이 같은 판정 루프를 공유하도록 후보 목록 형태로 통일했다.
"""
import time
from dataclasses import dataclass, field

from . import geo
from .imaging import view_to_data_uri
from .providers.base import Neighbor, Pano
from .vlm import ImageIgnoredError, ServerDeadError, VlmError


@dataclass
class WalkConfig:
    fov_deg: float = 90.0
    max_candidates: int = 3       # 한 스텝에서 최대 몇 방향까지 물어볼지
    max_turn_deg: float = 120.0   # 이보다 크게 꺾이는 이웃은 후보에서 뺀다 (U턴 방지)
    max_steps: int = 120

    # 후보를 전부 물을 것인가, 첫 성공에서 멈출 것인가.
    #
    # None = 자동: **그래프면 전부, 폴백이면 첫 성공에서 멈춤.**
    #
    # 근거가 비용이다. 그래프 후보는 온 길을 뺀 실제 이웃이라 직선 구간에서는
    # 개수가 1이다 — 전부 물어도 1호출이고, 개수가 2 이상인 곳은 진짜 갈림길이라
    # 어차피 알아야 한다. 즉 늘어나야 할 곳에서만 늘어난다.
    #
    # 폴백 후보(bearing, ±60°)는 성격이 다르다. 실제 갈래가 아니라 추측이고
    # 항상 3개다. 전부 물으면 매 스텝 3배가 되는데 얻는 게 없다.
    probe_all: bool | None = None
    max_seconds: float = 900.0
    miss_tolerance: int = 2       # 어느 방향도 산책로가 아닌 스텝을 몇 번 참을지
    revisit_tolerance: int = 3

    # ── 이웃 그래프가 없는 provider 를 위한 폴백 ──
    step_m: float = 12.0
    snap_radius_m: float = 25.0
    side_offsets: tuple[float, ...] = (-60.0, 60.0)


@dataclass
class WalkResult:
    path: list[dict] = field(default_factory=list)   # 실제로 밟은 pano 들
    stop_reason: str = ""
    steps: int = 0
    calls: int = 0
    wall_s: float = 0.0
    used_graph: bool = False

    # 산책로라고 판정됐지만 가지 않은 갈래들. 한 지점에서 여러 방향이 동시에
    # 산책로일 수 있고(갈림길이 그런 곳이다), 우리는 그중 하나만 따라간다.
    # 나머지를 버리지 않고 남긴다 — 나중에 분기 탐색을 붙일 때 이게 그대로
    # 프론티어가 된다 (→ docs/23-open-questions.md §8).
    frontier: list[dict] = field(default_factory=list)


def _candidates(provider, pano: Pano, bearing: float, came_from: str | None,
                visited: dict, cfg: WalkConfig) -> tuple[list[tuple[float, Neighbor | None]], bool]:
    """갈 만한 방향들을 **진행 방향에 가까운 순으로**. (후보, 그래프였나).

    맨 앞이 "정면" 이다. 이웃 그래프가 있으면 정면이 실측 방위각이고,
    없으면 그냥 현재 진행 방위다.
    """
    try:
        nbrs = provider.neighbors(pano)
    except Exception:
        nbrs = []

    if nbrs:
        # 온 길과 이미 밟은 곳을 뺀다. 이게 그래프를 쓰는 가장 큰 이유다 —
        # 각도로 어림하지 않고 정확히 지운다.
        fresh = [n for n in nbrs
                 if n.pano_id != came_from and n.pano_id not in visited]
        fresh.sort(key=lambda n: geo.angle_diff(n.heading, bearing))
        turnable = [n for n in fresh if geo.angle_diff(n.heading, bearing) <= cfg.max_turn_deg]
        picked = (turnable or fresh)[:cfg.max_candidates]
        if picked:
            return [(n.heading, n) for n in picked], True
        # 이웃은 있는데 전부 온 길/기방문이면 막다른 길이다. 폴백으로 새지 않는다.
        return [], True

    # 그래프를 못 주는 provider — 예전 방식(좌표 밀기)으로 돈다
    offs = (0.0, *cfg.side_offsets)[:cfg.max_candidates]
    return [(geo.norm_deg(bearing + o), None) for o in offs], False


def walk(provider, client, start: tuple[float, float], start_bearing: float,
         cfg: WalkConfig = WalkConfig(), log=None) -> WalkResult:
    """start 에서 start_bearing 방향으로 출발해 산책로를 따라간다."""
    res = WalkResult()
    t0 = time.time()
    bearing = geo.norm_deg(start_bearing)
    pos = start
    pano: Pano | None = None
    came_from: str | None = None
    visited: dict[str, int] = {}
    misses = revisits = 0

    def probe(p: Pano, hdg: float, step: int) -> bool | None:
        """한 방향을 물어본다. None 은 '판정 불가' (캡처 실패)."""
        try:
            raw = provider.capture(p, hdg, cfg.fov_deg)
        except Exception as e:
            if log:
                log.event("capture_failed", step=step, pano_id=p.pano_id,
                          heading=round(hdg, 1), error=f"{type(e).__name__}: {e}")
            return None
        uri, src_format = view_to_data_uri(raw)
        v = client.assess(uri, heading=hdg)
        res.calls += 1
        if log:
            log.probe(step=step, pano_id=p.pano_id, lat=p.lat, lng=p.lng,
                      heading=hdg, verdict=v, src_format=src_format)
        return v.is_trail

    while True:
        if res.steps >= cfg.max_steps:
            res.stop_reason = "max_steps"; break
        if time.time() - t0 > cfg.max_seconds:
            res.stop_reason = "time_budget"; break

        # ── 현재 pano 확정 ────────────────────────────────────────────
        # 그래프 이동이면 이미 정해져 있다. 최초 스텝이나 폴백 이동이면 스냅한다.
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

        seen = visited.get(pano.pano_id, 0)
        visited[pano.pano_id] = seen + 1
        if seen:
            revisits += 1
            if log:
                log.event("revisit", step=res.steps, pano_id=pano.pano_id, count=seen + 1)
            if revisits > cfg.revisit_tolerance:
                res.stop_reason = "revisit_loop"; break

        # ── 방향 결정 ──────────────────────────────────────────────────
        cands, graph = _candidates(provider, pano, bearing, came_from, visited, cfg)
        res.used_graph = res.used_graph or graph
        if not cands:
            res.stop_reason = "dead_end"
            if log:
                log.event("no_candidates", step=res.steps, pano_id=pano.pano_id)
            break

        probe_all = cfg.probe_all if cfg.probe_all is not None else graph
        chosen: tuple[float, Neighbor | None] | None = None
        oks: list[tuple[float, Neighbor | None]] = []
        try:
            for hdg, nb in cands:
                if probe(pano, hdg, res.steps):
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

        if oks:
            chosen = oks[0]         # 가장 정면에 가까운 산책로

        # 갈림길: 산책로가 둘 이상이면 하나만 따라가고 나머지는 남긴다.
        # 여기가 "여러 방향이 동시에 산책로일 수 있다" 를 실제로 담는 곳이다.
        if len(oks) > 1:
            for hdg, nb in oks[1:]:
                res.frontier.append({
                    "from_pano": pano.pano_id, "from_step": res.steps,
                    "heading": round(hdg, 1),
                    "pano_id": nb.pano_id if nb else None,
                    "lat": nb.lat if nb else None, "lng": nb.lng if nb else None,
                })
            if log:
                log.event("branch", step=res.steps, pano_id=pano.pano_id,
                          taken=round(oks[0][0], 1),
                          left=[round(h, 1) for h, _ in oks[1:]])

        res.path.append({"pano_id": pano.pano_id, "lat": pano.lat, "lng": pano.lng,
                         "bearing": round(bearing, 1), "is_trail": chosen is not None,
                         "n_candidates": len(cands), "n_trails": len(oks)})

        if chosen is None:
            misses += 1
            if misses > cfg.miss_tolerance:
                res.stop_reason = "dead_end"; break
            # 아직 참는다: 가장 정면인 후보로 한 칸 밀어보고 다시 판단한다.
            # 나무 그늘이나 역광 한 장 때문에 멀쩡한 길을 포기하는 일이 실제로 있다.
            chosen = cands[0]
        else:
            misses = 0

        # ── 이동 ───────────────────────────────────────────────────────
        hdg, nb = chosen
        bearing = hdg
        came_from = pano.pano_id
        if nb is not None:
            # 그래프 이동. 좌표를 밀 필요도, 다시 스냅할 필요도 없다.
            pano = Pano(pano_id=nb.pano_id, lat=nb.lat, lng=nb.lng)
        else:
            pos = geo.destination((pano.lat, pano.lng), hdg, cfg.step_m)
            pano = None             # 다음 바퀴에서 스냅한다
        res.steps += 1

    res.wall_s = time.time() - t0
    return res

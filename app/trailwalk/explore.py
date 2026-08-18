"""분기 탐색 — 시작점에서 뻗는 산책로를 전부 그린다.

walk.py 가 "한 길을 따라간다" 라면 여기는 "이 지점에서 갈 수 있는 산책로를
전부 마킹한다" 다. 갈림길에서 하나를 고르는 대신 산책로로 판정된 갈래를
**전부 큐에 넣고** 너비 우선으로 소비한다. walk.py 가 frontier 를 기록만
하고 버리지 않았던 이유가 이것이다 (docs/23-open-questions.md §8).

VLM 에게 묻는 것은 walk 와 완전히 같다: "이 장면이 산책로인가" 하나.
캡처와 판정도 walk 의 후보 생성(`walk._candidates`)을 그대로 쓴다 —
두 루프의 판정 조건이 갈라지면 결과를 비교할 수 없게 된다.

### 예산은 depth 가 아니라 호출 수로 건다

같은 depth 라도 갈림길 수에 따라 호출 수가 크게 달라진다. 지갑에서 나가는
것은 depth 가 아니라 VLM 호출(~2.2s/건)이므로 실질 상한은 `max_vlm_calls` 다.
`max_depth` 는 "시작점에서 몇 걸음 반경까지 볼 것인가" 라는 의미적 한계로
따로 둔다. 어느 쪽이든 예산에 걸려 못 간 갈래는 버리지 않고 frontier 에
남긴다 — 이어서 탐색할 때 그게 그대로 입력이다.

### pano 하나에 판정 하나

같은 pano 를 두 노드에서 접근할 수 있다 (마름모꼴 골목). 판정은 접근
방향의 화면에 대한 것이라 방향마다 다를 수 있지만, **첫 접근의 판정을
그 pano 의 판정으로 삼고 다시 묻지 않는다.** 재판정은 호출을 두 배로
쓰는데, 갈림길 반대편에서 어차피 다른 pano 들로 이어지므로 얻는 것이
적다. visited 에는 "큐에 들어간 것" 과 "아니라고 판정된 것" 이 함께 든다.

### walk 와 달리 miss 를 참지 않는다

walk 는 그늘 한 장 때문에 길을 포기하지 않으려고 miss_tolerance 만큼
밀어본다. 여기서는 안 한다 — 갈래마다 밀어붙이면 오판 하나가 가짜 가지
하나를 통째로 만들고, 호출 예산이 그리로 샌다. 한 방향의 오판은 그 갈래
하나가 일찍 끝나는 것으로 값을 치른다.
"""
import time
from collections import deque
from dataclasses import dataclass, field

from . import geo
from .imaging import view_to_data_uri
from .providers.base import Pano
from .vlm import ImageIgnoredError, ServerDeadError, VlmError
from .walk import _candidates


@dataclass
class ExploreConfig:
    # ── 예산. 실질 상한은 호출 수다 ────────────────────────────────────
    max_vlm_calls: int = 60       # VLM 호출 총량. 탐색 비용의 실체(~2.2s/건)라 이것이 실질 상한
    max_depth: int = 15           # 시작 pano 로부터의 걸음 수 한계. "어디까지 볼 것인가" 의 반경
    max_seconds: float = 900.0    # 벽시계 예산. 캡처(프레임 안정화) 지연까지 포함한 안전망

    # ── 판정 ──────────────────────────────────────────────────────────
    fov_deg: float = 90.0         # 캡처 화각. walk 와 같은 값이어야 판정 기준이 비교 가능하다

    # ── 후보 ──────────────────────────────────────────────────────────
    max_candidates: int = 4       # 한 노드에서 최대 몇 방향까지 물어볼지 (그래프 이웃 상한)
    max_turn_deg: float = 180.0   # 180 = 방향 필터 없음. walk 와 달리 U턴 방지가 필요 없다 —
                                  # 온 길은 visited 로 정확히 빠지고, 탐색은 모든 방향을 본다

    # ── 이웃 그래프가 없는 provider 를 위한 폴백 (walk 와 같은 값) ─────
    step_m: float = 12.0          # 좌표 밀기 보폭
    snap_radius_m: float = 25.0   # 민 좌표를 pano 로 스냅할 때 허용 반경
    side_offsets: tuple[float, ...] = (-60.0, 60.0)   # 진행 방향 기준 좌우 후보 각
    start_offsets: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)
                                  # 시작 노드 전용. 폴백에는 "온 길" 이 없으므로 전방향을 본다.
                                  # 그래프 provider 에서는 이웃 목록 자체가 전방향이라 안 쓴다


@dataclass
class ExploreResult:
    # 방문(확장)한 지점들. 시작 노드 외에는 전부 산책로로 판정돼 들어온 것이다
    nodes: list[dict] = field(default_factory=list)    # pano_id, lat, lng, depth, parent
    # 판정 하나하나 = 그래프의 간선. UI 마킹의 원천 데이터다
    probes: list[dict] = field(default_factory=list)   # from_pano, heading, to_pano, is_trail
    # 예산(depth/호출/시간)에 걸려 못 간 갈래. 이어서 탐색할 때의 입력
    frontier: list[dict] = field(default_factory=list)  # from_pano, pano_id, 좌표, depth, reason
    stop_reason: str = ""          # exhausted = 갈 곳을 다 갔다. 나머지는 예산/오류
    calls: int = 0
    wall_s: float = 0.0
    used_graph: bool = False


@dataclass
class _Node:
    depth: int
    bearing: float                 # 이 노드에 들어온 진행 방위. 폴백 후보 생성의 기준
    came_from: str | None          # 부모 pano_id
    pano: Pano | None = None       # 그래프 자식은 pano 를 이미 안다
    pos: tuple[float, float] | None = None   # 폴백 자식은 좌표만 안다. 꺼낼 때 스냅한다


def explore(provider, client, start: tuple[float, float], start_bearing: float = 0.0,
            cfg: ExploreConfig | None = None, log=None) -> ExploreResult:
    """start 에서 모든 방향으로 산책로 그래프를 넓힌다."""
    cfg = cfg or ExploreConfig()   # 기본 인자로 두면 호출 간에 공유된다 (walk.py 와 같은 이유)
    res = ExploreResult()
    t0 = time.time()

    def probe(p: Pano, hdg: float, depth: int) -> bool | None:
        """한 방향을 물어본다. None 은 '판정 불가' (캡처 실패). walk.probe 와 같은 모양."""
        try:
            raw = provider.capture(p, hdg, cfg.fov_deg)
        except Exception as e:
            if log:
                log.event("capture_failed", step=depth, pano_id=p.pano_id,
                          heading=round(hdg, 1), error=f"{type(e).__name__}: {e}")
            return None
        uri, src_format = view_to_data_uri(raw)
        v = client.assess(uri, heading=hdg)
        res.calls += 1
        if log:
            log.probe(step=depth, pano_id=p.pano_id, lat=p.lat, lng=p.lng,
                      heading=hdg, verdict=v, src_format=src_format)
        return v.is_trail

    def leftover(node: _Node, reason: str) -> dict:
        """예산에 걸려 못 간 큐 항목을 frontier 형태로."""
        return {"from_pano": node.came_from,
                "pano_id": node.pano.pano_id if node.pano else None,
                "lat": node.pano.lat if node.pano else (node.pos[0] if node.pos else None),
                "lng": node.pano.lng if node.pano else (node.pos[1] if node.pos else None),
                "depth": node.depth, "reason": reason}

    # ── 시작 pano ──────────────────────────────────────────────────────
    try:
        start_pano = provider.nearest(start[0], start[1], cfg.snap_radius_m)
    except Exception as e:
        if log:
            log.event("provider_error", step=0,
                      error=f"{type(e).__name__}: {str(e).splitlines()[0]}")
        res.stop_reason = "provider_error"
        res.wall_s = time.time() - t0
        return res
    if start_pano is None:
        # 시작점에 로드뷰가 없다. 갈래 하나가 아니라 탐색 전체가 성립하지 않는다
        res.stop_reason = "no_coverage"
        if log:
            log.event("no_coverage", step=0, lat=start[0], lng=start[1])
        res.wall_s = time.time() - t0
        return res

    queue: deque[_Node] = deque(
        [_Node(depth=0, bearing=geo.norm_deg(start_bearing), came_from=None, pano=start_pano)])
    # 큐에 들어갔거나 "아니오" 판정을 받은 pano. 어느 쪽이든 다시 묻지 않는다
    visited: set[str] = {start_pano.pano_id}

    while queue:
        if time.time() - t0 > cfg.max_seconds:
            res.stop_reason = "time_budget"
            res.frontier += [leftover(n, "time_budget") for n in queue]
            break

        node = queue.popleft()

        # 폴백 자식은 좌표만 들고 온다 — 여기서 스냅한다
        if node.pano is None:
            try:
                node.pano = provider.nearest(node.pos[0], node.pos[1], cfg.snap_radius_m)
            except Exception as e:
                if log:
                    log.event("provider_error", step=node.depth,
                              error=f"{type(e).__name__}: {str(e).splitlines()[0]}")
                res.stop_reason = "provider_error"
                break
            if node.pano is None:
                # 이 갈래에 로드뷰가 없다. walk 와 달리 다른 갈래가 남아 있으므로
                # 탐색 전체를 멈추지 않고 이 가지만 접는다
                if log:
                    log.event("no_coverage", step=node.depth, lat=node.pos[0], lng=node.pos[1])
                continue
            if node.pano.pano_id in visited:
                continue               # 다른 갈래가 먼저 도달했다 (격자 스냅 중복 등)
            visited.add(node.pano.pano_id)

        res.nodes.append({"pano_id": node.pano.pano_id, "lat": node.pano.lat,
                          "lng": node.pano.lng, "depth": node.depth,
                          "parent": node.came_from})

        if node.depth >= cfg.max_depth:
            # 마킹은 하되 확장하지 않는다. 저 너머는 안 본 것이지 없는 것이 아니다
            res.frontier.append(leftover(node, "max_depth"))
            continue

        # ── 후보 생성. walk 와 같은 코드 경로 — 판정 조건이 갈라지지 않게 ──
        cands, graph = _candidates(provider, node.pano, node.bearing,
                                   node.came_from, visited, cfg)
        if node.came_from is None and not graph:
            # 폴백의 시작 노드: 진행 방향이라는 개념이 아직 없다. 전방향을 본다
            cands = [(geo.norm_deg(start_bearing + o), None) for o in cfg.start_offsets]
        res.used_graph = res.used_graph or graph

        budget_hit = False
        for i, (hdg, nb) in enumerate(cands):
            if res.calls >= cfg.max_vlm_calls:
                res.stop_reason = "call_budget"
                # 못 물은 후보들도 frontier 다 — 판정을 안 받았을 뿐 갈래는 갈래다
                for _h2, n2 in cands[i:]:
                    res.frontier.append({
                        "from_pano": node.pano.pano_id,
                        "pano_id": n2.pano_id if n2 else None,
                        "lat": n2.lat if n2 else None, "lng": n2.lng if n2 else None,
                        "depth": node.depth + 1, "reason": "call_budget"})
                budget_hit = True
                break

            try:
                ok = probe(node.pano, hdg, node.depth)
            except ImageIgnoredError:
                res.stop_reason = "image_ignored"; raise
            except ServerDeadError:
                res.stop_reason = "server_dead"; raise
            except VlmError as e:
                if log:
                    log.event("vlm_error", step=node.depth, error=str(e)[:400])
                res.stop_reason = "vlm_error"
                budget_hit = True
                break

            if ok is None:
                # 캡처 실패는 판정이 아니다. probes 에 넣으면 "아니오" 와 섞인다 —
                # 이 레포의 사고는 전부 그런 혼동에서 났다. 로그(capture_failed)만 남긴다
                continue

            # to_* 는 그리기/UI 용이다. 폴백 후보는 목표 pano 가 없어 None —
            # 그 경우 좌표는 heading 방향으로 step_m 민 곳이 근사값이다
            res.probes.append({"from_pano": node.pano.pano_id, "heading": round(hdg, 1),
                               "to_pano": nb.pano_id if nb else None,
                               "to_lat": nb.lat if nb else None,
                               "to_lng": nb.lng if nb else None,
                               "is_trail": ok, "depth": node.depth})
            if nb is not None:
                # 첫 접근의 판정이 그 pano 의 판정이다 — 참이든 거짓이든 다시 묻지 않는다
                visited.add(nb.pano_id)
            if not ok:
                continue

            if nb is not None:
                queue.append(_Node(depth=node.depth + 1, bearing=hdg,
                                   came_from=node.pano.pano_id,
                                   pano=Pano(pano_id=nb.pano_id, lat=nb.lat, lng=nb.lng)))
            else:
                queue.append(_Node(depth=node.depth + 1, bearing=hdg,
                                   came_from=node.pano.pano_id,
                                   pos=geo.destination((node.pano.lat, node.pano.lng),
                                                       hdg, cfg.step_m)))

        if budget_hit:
            res.frontier += [leftover(n, res.stop_reason) for n in queue]
            break

    if not res.stop_reason:
        res.stop_reason = "exhausted"
    res.wall_s = time.time() - t0
    return res

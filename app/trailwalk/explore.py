"""분기 탐색 — 시작점에서 뻗는 산책로를 전부 그린다.

"이 지점에서 갈 수 있는 산책로를 전부 마킹한다" 가 하는 일이다. 갈림길에서
하나를 고르는 대신 갈래를 **전부 큐에 넣고** 너비 우선으로 소비한다.

한때 갈림길에서 하나만 따라가는 루프(walk)가 따로 있었고 나머지 갈래를
frontier 에 기록만 했다. 그 "나머지" 를 실제로 가는 것이 여기이므로 walk 는
이것의 부분집합이었다 — 2026-08-20 에 지웠다. 탐색 범위는 반경으로 정한다.

VLM 에게 묻는 것은 하나다: "이 장면이 산책로인가".

### 예산 축은 둘뿐이다: 시간과 거리

`max_distance_m` 이 시작점에서의 직선 반경을, `max_seconds` 가 벽시계를
자른다. 한때 호출 수와 걸음 수(depth)도 예산이었는데, 넷을 두면 어느 것이
실제로 끊었는지가 런마다 달라져 "여기까지가 지도인가 예산인가" 를 매번
되짚어야 했다. 사용자가 정하는 것은 반경이고, 시간은 그 안전망이다.

예산에 걸려 못 간 갈래는 버리지 않고 frontier 에 남긴다 — 이어서 탐색할 때
그게 그대로 입력이다. depth 는 예산 축에서 빠졌지만 출력에는 남는다
(노드가 시작점에서 몇 홉인지는 그리는 쪽이 쓴다).

### pano 하나에 판정 하나

같은 pano 를 두 노드에서 접근할 수 있다 (마름모꼴 골목). 판정은 접근
방향의 화면에 대한 것이라 방향마다 다를 수 있지만, **첫 접근의 판정을
그 pano 의 판정으로 삼고 다시 묻지 않는다.** 재판정은 호출을 두 배로
쓰는데, 갈림길 반대편에서 어차피 다른 pano 들로 이어지므로 얻는 것이
적다. visited 에는 "큐에 들어간 것" 과 "아니라고 판정된 것" 이 함께 든다.

### "아님" 판정도 확장한다 (2026-08-18 결정, 2026-08-20 고정)

처음엔 산책로로 판정된 갈래만 확장했는데, 그러면 차도 pano 에서 시작하는
순간 모든 방향이 "아님" 이라 탐색이 2호출 만에 죽는다 — 실측으로 확인됐고,
웹 UI 유저는 길가를 찍기 마련이다. 그래서 판정과 무관하게 반경 안에서는
계속 간다. 차도를 다리 삼아 건너면 하천 램프가 나온다.

끄는 손잡이(`expand_non_trail`)를 뒀다가 지웠다. "반경 안을 전부 본다" 가
정책인 이상 끄면 그 정책이 깨지고, 끈 런과 안 끈 런의 결과를 비교할 수 없다.

폭주는 반경(max_distance_m)이 막는다. 차도로 새더라도 반경 밖으로는 못 간다.

### 큐는 하나다

한때 산책로 갈래와 "아님" 갈래를 두 큐로 나눠 산책로 쪽을 먼저 비웠다.
호출이 차도 격자에 새는 것을 막으려는 것이었지만, 그러면 소비 순서가
depth 를 따르지 않아 **"너비 우선" 이 더는 사실이 아니게 된다** — 아님
큐의 depth 2 가 산책로 큐의 depth 8 뒤로 밀린다. 그러면 frontier 도
"시작점에서 가까운 순서로 잘린 경계" 가 아니게 되어 이어서 탐색할 때의
입력으로서 의미가 흐려진다.

지금은 발견 순서 그대로 FIFO 로 소비한다. 판정은 큐의 순서를 바꾸지
않는다. 차도로 새는 것은 반경이 막는다.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from . import geo, settings
from .imaging import view_to_data_uri
from .providers.base import Neighbor, Pano, ProviderError
from .vlm import ImageIgnoredError, ServerDeadError, VlmError


@dataclass
class ExploreConfig:
    """탐색 정책. **기본값은 여기가 아니라 app/config/trailwalk.yaml 에 있다.**

    기본값을 코드에 다시 적으면 정본이 둘이 된다 (→ 루트 CLAUDE.md "설정").
    """
    max_seconds: float
    max_distance_m: float
    max_candidates: int
    snap_radius_m: float

    # 캡처한 바이트를 서버로 보낼 때의 규칙 (크기·품질). 루프가 imaging 에
    # 넘긴다 — 모듈 상수로 읽게 두면 --config 가 무시된다
    image: object

    @classmethod
    def from_settings(cls, s) -> ExploreConfig:
        return cls(
            max_seconds=s.budget.max_seconds,
            max_distance_m=s.budget.max_distance_m,
            max_candidates=s.candidates.max_candidates,
            snap_radius_m=s.geo.snap_radius_m,
            image=s.image,
        )


@dataclass
class ExploreResult:
    # 방문(확장)한 지점들. 시작 노드 외에는 전부 산책로로 판정돼 들어온 것이다
    nodes: list[dict] = field(default_factory=list)    # pano_id, lat, lng, depth, parent
    # 판정 하나하나 = 그래프의 간선. UI 마킹의 원천 데이터다
    probes: list[dict] = field(default_factory=list)   # from_pano, heading, to_pano, is_trail
    # 예산(거리/시간)이나 이웃 로드 실패로 못 간 갈래. 이어서 탐색할 때의 입력
    frontier: list[dict] = field(default_factory=list)  # from_pano, pano_id, 좌표, depth, reason
    stop_reason: str = ""          # exhausted = 갈 곳을 다 갔다. 나머지는 예산/오류
    calls: int = 0
    wall_s: float = 0.0


def _candidates(provider, pano: Pano, bearing: float, came_from: str | None,
                visited: set[str], cfg: ExploreConfig) -> tuple[list[tuple[float, Neighbor]], bool]:
    """갈 만한 방향들을 **진행 방향에 가까운 순으로**. (후보, 이웃을 얻었나).

    맨 앞이 "정면" 이고, 그 방위는 이웃의 **실측 방위각**이다 (노드 JSON 의
    `spot[].pan`). 우리가 각도를 지어내는 자리는 없다.

    시작 노드(`came_from is None`)는 후보를 **하나도 안 뺀다** — 아래 참조.

    두 번째 값이 False 면 **이웃 목록 자체를 못 얻은 것**이다 — 갈래가 없는
    것과 구분해야 한다. 빈 후보 목록은 "이웃은 있는데 전부 온 길/기방문"
    (진짜 막다른 길)을 뜻한다.
    """
    try:
        nbrs = provider.neighbors(pano)
    except ProviderError:
        raise           # provider 가 이름 붙여 터뜨린 실패다 (형식 변경 등).
                        # neighbors_missing 으로 뭉개면 원인이 소실된다
    except Exception:
        nbrs = []
    if not nbrs:
        return [], False

    # 온 길과 이미 밟은 곳을 뺀다. 이게 그래프를 쓰는 가장 큰 이유다 —
    # 각도로 어림하지 않고 정확히 지운다.
    fresh = [n for n in nbrs
             if n.pano_id != came_from and n.pano_id not in visited]
    fresh.sort(key=lambda n: geo.angle_diff(n.heading, bearing))

    if came_from is None:
        # ── 시작 노드: 화살표를 하나도 빼지 않는다 ──
        #
        # 시작점 판정은 "여기서 갈 수 있는 길이 하나라도 산책로인가" 이므로
        # 갈 수 있는 방향을 전부 봐야 한다. 호출 수 = 화살표 개수.
        return [(n.heading, n) for n in fresh], True

    # 각도로 거르지 않는다. 예전엔 max_turn_deg(walk 120°)로 U턴을 막았는데,
    # U턴은 이미 위에서 came_from/visited 가 **pano_id 로 정확히** 막는다.
    # 각도 필터가 실제로 한 일은 두 가지뿐이었다:
    #
    #   - 시작 노드에서 사용자가 준 bearing 이 지도의 갈래를 지웠다. 청계천
    #     실측: 이웃이 동 91.4°/서 267.8° 인데 bearing 45 를 주면 서쪽이
    #     137° 로 걸려 아예 안 물어보고 frontier 에도 안 남았다. 그래서 시작
    #     노드는 위에서 면제됐다.
    #   - 남은 자리에서도 `turnable or fresh` 폴백 때문에 하드 필터가 아니라
    #     선호도였다. 전멸하면 통째로 무시됐다.
    #
    # explore 는 이미 180(=필터 없음)으로 돌고 있었고 문제가 없었다. 정렬이
    # 이미 정면을 앞에 두므로 자르기만 하면 된다.
    return [(n.heading, n) for n in fresh[:cfg.max_candidates]], True


@dataclass
class _Node:
    depth: int
    bearing: float                 # 이 노드에 들어온 진행 방위. 후보 정렬의 기준
    came_from: str | None          # 부모 pano_id
    pano: Pano                     # 이웃이 곧 다음 지점이라 큐에 들어올 때 이미 안다
    is_trail: bool | None = None   # 이 노드를 발견한 판정. 시작 노드만 None (판정 전)


def explore(provider, client, start: tuple[float, float], start_bearing: float = 0.0,
            cfg: ExploreConfig | None = None, log=None) -> ExploreResult:
    """start 에서 모든 방향으로 산책로 그래프를 넓힌다."""
    # 기본 인자로 ExploreConfig(...) 를 두면 인스턴스 하나가 호출 간에 공유된다
    cfg = cfg or ExploreConfig.from_settings(settings.SETTINGS)
    res = ExploreResult()
    t0 = time.time()

    def probe(p: Pano, hdg: float, depth: int) -> bool | None:
        """한 방향을 물어본다. None 은 '판정 불가' (캡처 실패)."""
        try:
            raw = provider.capture(p, hdg)
        except Exception as e:
            if log:
                log.event("capture_failed", step=depth, pano_id=p.pano_id,
                          heading=round(hdg, 1), error=f"{type(e).__name__}: {e}")
            return None
        uri, src_format = view_to_data_uri(raw, cfg.image)
        v = client.assess(uri, heading=hdg)
        res.calls += 1
        if log:
            log.probe(step=depth, pano_id=p.pano_id, lat=p.lat, lng=p.lng,
                      heading=hdg, verdict=v, src_format=src_format, image=raw)
        return v.is_trail

    def leftover(node: _Node, reason: str) -> dict:
        """예산에 걸려 못 간 큐 항목을 frontier 형태로."""
        return {"from_pano": node.came_from, "pano_id": node.pano.pano_id,
                "lat": node.pano.lat, "lng": node.pano.lng,
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

    # 거리 예산의 기준점. 요청 좌표가 아니라 **스냅된 pano** 다 (→ yaml 주석)
    origin = (start_pano.lat, start_pano.lng)

    # 큐는 하나. 발견 순서대로 FIFO 라 소비 순서가 곧 depth 순서다
    q: deque[_Node] = deque(
        [_Node(depth=0, bearing=geo.norm_deg(start_bearing), came_from=None, pano=start_pano)])
    # 큐에 들어갔거나 "아니오" 판정을 받은 pano. 어느 쪽이든 다시 묻지 않는다
    visited: set[str] = {start_pano.pano_id}

    def drain() -> list[dict]:
        return [leftover(n, res.stop_reason) for n in q]

    while q:
        if time.time() - t0 > cfg.max_seconds:
            res.stop_reason = "time_budget"
            res.frontier += drain()
            break

        node = q.popleft()

        res.nodes.append({"pano_id": node.pano.pano_id, "lat": node.pano.lat,
                          "lng": node.pano.lng, "depth": node.depth,
                          "parent": node.came_from, "is_trail": node.is_trail})

        if geo.haversine_m(origin, (node.pano.lat, node.pano.lng)) > cfg.max_distance_m:
            # 마킹은 하되 확장하지 않는다. 반경 밖은 안 본 것이지 없는 것이 아니다
            res.frontier.append(leftover(node, "distance_budget"))
            continue

        # ── 후보 생성 (이 파일 위쪽 _candidates) ──
        cands, loaded = _candidates(provider, node.pano, node.bearing,
                                    node.came_from, visited, cfg)
        if not loaded:
            # 이웃 목록을 못 얻었다 — 갈래가 없는 게 아니라 렌더/스니핑 실패다.
            # 추측으로 걸으면 없는 길을 만들어내므로 여기서 접고, "안 본 곳" 으로
            # frontier 에 남긴다. 이어서 탐색할 때 그대로 입력이 된다.
            if log:
                log.event("neighbors_missing", step=node.depth, pano_id=node.pano.pano_id)
            res.frontier.append(leftover(node, "neighbors_missing"))
            continue

        budget_hit = False
        for i, (hdg, nb) in enumerate(cands):
            # 시간은 노드 경계가 아니라 **후보마다** 본다. 한 노드의 후보가
            # 최대 max_candidates 개라, 노드 경계에서만 보면 캡처가 느릴 때
            # 그 노드 하나가 통째로 예산을 넘겨 실행된다.
            if time.time() - t0 > cfg.max_seconds:
                res.stop_reason = "time_budget"
                # 못 물은 후보들도 frontier 다 — 판정을 안 받았을 뿐 갈래는 갈래다
                for _h2, n2 in cands[i:]:
                    res.frontier.append({
                        "from_pano": node.pano.pano_id, "pano_id": n2.pano_id,
                        "lat": n2.lat, "lng": n2.lng,
                        "depth": node.depth + 1, "reason": res.stop_reason})
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

            # to_* 는 그리기/UI 용이다. 후보가 곧 목표 pano 라 항상 채워진다.
            res.probes.append({"from_pano": node.pano.pano_id, "heading": round(hdg, 1),
                               "to_pano": nb.pano_id, "to_lat": nb.lat, "to_lng": nb.lng,
                               "is_trail": ok, "depth": node.depth})
            # 첫 접근의 판정이 그 pano 의 판정이다 — 참이든 거짓이든 다시 묻지 않는다
            visited.add(nb.pano_id)

            child = _Node(depth=node.depth + 1, bearing=hdg,
                          came_from=node.pano.pano_id, is_trail=ok,
                          pano=Pano(pano_id=nb.pano_id, lat=nb.lat, lng=nb.lng))
            q.append(child)

        if budget_hit:
            res.frontier += drain()
            break

    if not res.stop_reason:
        res.stop_reason = "exhausted"
    res.wall_s = time.time() - t0
    return res

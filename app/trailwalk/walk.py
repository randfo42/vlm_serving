"""탐색 루프 — 로드뷰를 따라 산책로를 이어간다.

VLM 에게 묻는 것은 오직 **"이 장면이 산책로인가"** 하나다. 어디로 갈지, 언제
멈출지, 갈림길에서 무엇을 고를지는 전부 여기서 정한다. 서빙 쪽이 그은 경계다
(docs/00-design.md §1).

### 한 스텝의 모양

    현재 좌표 → provider.nearest 로 pano 스냅
             → 진행 방향 화각 1장 캡처 → VLM 1턴
             → 산책로면 그대로 전진, 아니면 좌우를 재본다
             → 고른 방향으로 STEP_M 전진한 좌표를 다음 스텝의 입력으로

### 왜 "직진 먼저, 실패하면 좌우" 인가

호출 하나가 2.1초다. 스텝마다 3방향을 다 물으면 6.3초/스텝이고, 100스텝이면
10분이 넘는다. 그런데 산책로 위에서는 직진이 거의 항상 답이다. 직진이 통하는
동안은 1호출/스텝(2.1초)으로 가고, 막혔을 때만 좌우를 여는 게 같은 시간에
훨씬 멀리 간다.

대가는 **갈림길을 지나친다**는 것이다. 분기를 놓치면 안 되는 실험에서는
`probe_sides_every=N` 으로 N 스텝마다 강제로 좌우를 열 수 있다.

### 왜 이웃 pano 그래프를 안 쓰는가

없어서다. Kakao/Naver 는 이웃 파노라마 목록 API 를 공개하지 않는다
(docs/21-roadview-providers.md §4). 대신 좌표를 직접 밀고 스냅시킨다. 그래프가
없어도 걸을 수 있고, 오히려 provider 를 갈아끼우기 쉬워진다.
"""
import time
from dataclasses import dataclass, field

from . import geo
from .imaging import view_to_data_uri
from .providers.base import Pano
from .vlm import ImageIgnoredError, ServerDeadError, VlmError


@dataclass
class WalkConfig:
    step_m: float = 12.0          # 한 스텝 전진 거리. Kakao 도로 촬영 간격이 ~10m
    snap_radius_m: float = 25.0   # 이보다 멀면 커버리지가 없는 것으로 본다
    fov_deg: float = 90.0
    side_offsets: tuple[float, ...] = (-60.0, 60.0)   # 직진이 막혔을 때 재볼 각도
    probe_sides_every: int = 0    # >0 이면 N 스텝마다 갈림길 확인 (0=끄기)
    max_steps: int = 120
    max_seconds: float = 900.0
    miss_tolerance: int = 2       # 어느 방향도 산책로가 아닌 스텝을 몇 번 참을지
    revisit_tolerance: int = 3    # 같은 pano 를 다시 밟는 것을 몇 번 참을지


@dataclass
class WalkResult:
    path: list[dict] = field(default_factory=list)   # 실제로 밟은 pano 들
    stop_reason: str = ""
    steps: int = 0
    calls: int = 0
    wall_s: float = 0.0


def walk(provider, client, start: tuple[float, float], start_bearing: float,
         cfg: WalkConfig = WalkConfig(), log=None) -> WalkResult:
    """start 에서 start_bearing 방향으로 출발해 산책로를 따라간다."""
    res = WalkResult()
    t0 = time.time()
    pos, bearing = start, geo.norm_deg(start_bearing)
    visited: dict[str, int] = {}
    misses = revisits = 0

    def probe(pano: Pano, hdg: float, step: int) -> bool | None:
        """한 방향을 물어본다. None 은 '판정 불가' (캡처 실패)."""
        try:
            raw = provider.capture(pano, hdg, cfg.fov_deg)
        except Exception as e:
            if log:
                log.event("capture_failed", step=step, pano_id=pano.pano_id,
                          heading=round(hdg, 1), error=f"{type(e).__name__}: {e}")
            return None
        uri, src_format = view_to_data_uri(raw)
        v = client.assess(uri, heading=hdg)
        res.calls += 1
        if log:
            log.probe(step=step, pano_id=pano.pano_id, lat=pano.lat, lng=pano.lng,
                      heading=hdg, verdict=v, src_format=src_format)
        return v.is_trail

    while True:
        if res.steps >= cfg.max_steps:
            res.stop_reason = "max_steps"; break
        if time.time() - t0 > cfg.max_seconds:
            res.stop_reason = "time_budget"; break

        try:
            pano = provider.nearest(pos[0], pos[1], cfg.snap_radius_m)
        except Exception as e:
            # provider 가 스스로 이상을 알린 경우(위치 갱신 지연 등). 조용히
            # 넘기면 엉뚱한 좌표에서 탐색이 계속되므로 여기서 멈춘다.
            if log:
                log.event("provider_error", step=res.steps,
                          error=f"{type(e).__name__}: {str(e).splitlines()[0]}")
            res.stop_reason = "provider_error"
            break
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
        # 직진을 먼저 본다. 통하면 좌우는 아예 묻지 않는다 (호출 1회).
        force_sides = cfg.probe_sides_every and res.steps % cfg.probe_sides_every == 0
        chosen: float | None = None
        try:
            straight_ok = probe(pano, bearing, res.steps)
            if straight_ok and not force_sides:
                chosen = bearing
            else:
                branches: list[float] = []
                for off in cfg.side_offsets:
                    hdg = geo.norm_deg(bearing + off)
                    if probe(pano, hdg, res.steps):
                        branches.append(hdg)
                        if not force_sides:
                            break   # 직진이 막힌 상황이면 첫 성공을 바로 택한다
                if branches and log:
                    log.event("branch", step=res.steps, pano_id=pano.pano_id,
                              headings=[round(h, 1) for h in branches])
                # 갈림길이어도 직진이 살아 있으면 직진을 유지한다. 양쪽 다 산책로일 때
                # 방향을 트는 것은 근거 없는 선택이고, 경로가 제자리를 맴돌게 만든다.
                if straight_ok:
                    chosen = bearing
                elif branches:
                    chosen = branches[0]
        except ImageIgnoredError:
            res.stop_reason = "image_ignored"; raise
        except ServerDeadError:
            res.stop_reason = "server_dead"; raise
        except VlmError as e:
            if log:
                log.event("vlm_error", step=res.steps, error=str(e)[:400])
            res.stop_reason = "vlm_error"; break

        res.path.append({"pano_id": pano.pano_id, "lat": pano.lat, "lng": pano.lng,
                         "bearing": round(bearing, 1), "is_trail": chosen is not None})

        if chosen is None:
            misses += 1
            if misses > cfg.miss_tolerance:
                res.stop_reason = "dead_end"; break
            # 아직 참는다: 직진으로 한 칸 밀어보고 다시 판단한다. 나무 그늘이나
            # 역광 한 장 때문에 멀쩡한 길을 포기하는 일이 실제로 일어난다.
            chosen = bearing
        else:
            misses = 0

        pos = geo.destination((pano.lat, pano.lng), chosen, cfg.step_m)
        bearing = chosen
        res.steps += 1

    res.wall_s = time.time() - t0
    return res

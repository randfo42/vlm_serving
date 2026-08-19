"""분기 탐색 — 큐 소비와 예산.

VLM 도 브라우저도 없이 돈다. 검증하는 것은 판정 **품질**이 아니라 판정을
받은 뒤의 **행동**이다. 그 둘은 다르고, 섞으면 둘 다 못 잰다.

특히 지키려는 것:

- 시작점에서는 모든 방향을 본다 (진행 방위가 갈래를 지우지 않는다)
- 갈림길에서 갈래를 버리지 않고 전부 간다
- pano 하나에 판정 하나 — 두 경로로 접근해도 다시 묻지 않는다
- 예산(거리/시간)에 걸린 갈래는 사라지지 않고 frontier 에 남는다
- **이동을 지어내지 않는다.** 이웃 목록을 못 얻은 것과 길이 끝난 것은 다른 사실이다
"""
import math
from dataclasses import replace

import pytest

from conftest import Client, Provider, nb
from trailwalk import explore as explore_mod
from trailwalk import geo, settings
from trailwalk.explore import ExploreConfig, explore
from trailwalk.providers.base import ProviderError

# 위도만 움직여 거리를 만든다. 경도를 안 건드리면 cos(lat) 항이 안 끼어
# haversine 이 R·Δφ 그대로라 오차가 없다. 값을 다시 적지 않으려고 geo 에서 파생.
_M_PER_DEG = geo.R * math.pi / 180        # 위도 1도 ≈ 111_195 m


def north(m: float, base: float = 37.5) -> float:
    """base 에서 정북으로 m 미터 떨어진 위도."""
    return base + m / _M_PER_DEG


def run(provider, verdicts, bearing=0.0, client=None, **cfg):
    """정본 설정에서 출발해 인자로 준 것만 덮어쓴다.

    기본값을 여기 다시 적지 않는 것이 요점이다 — 테스트가 자기만의 기본값을
    들고 있으면 설정 파일을 바꿔도 테스트는 예전 값으로 계속 통과한다.
    """
    client = client or Client(provider, verdicts)
    base = ExploreConfig.from_settings(settings.SETTINGS)
    return explore(provider, client, (37.5, 127.0), bearing, replace(base, **cfg))


# ── 시작 노드 ───────────────────────────────────────────────────────────────

def test_시작점에서_모든_이웃을_묻는다():
    """시작점에는 "온 길" 이 없다. 갈 수 있는 방향을 전부 봐야 한다."""
    p = Provider({"S": [nb("A", 90.0), nb("B", 270.0), nb("C", 0.0)],
                  "A": [nb("S", 270.0)], "B": [nb("S", 90.0)], "C": [nb("S", 180.0)]})
    run(p, {})
    assert set(p.probes) == {("S", 90.0), ("S", 270.0), ("S", 0.0)}


def test_시작점은_max_candidates로도_안_자른다():
    """호출 수 = 화살표 개수. 갈림길 한복판에서 시작할 수 있어야 한다."""
    p = Provider({"S": [nb("A", 0.0), nb("B", 90.0), nb("C", 180.0), nb("D", 270.0)],
                  "A": [], "B": [], "C": [], "D": []})
    run(p, {}, max_candidates=2)
    at_s = [h for pid, h in p.probes if pid == "S"]
    assert len(at_s) == 4, f"화살표 4개인데 {len(at_s)}번만 물었다"


def test_시작점의_이웃을_못_얻으면_아무것도_묻지_않는다():
    """전방향을 흉내내는 start_offsets 가 여기 있었다. 지웠다 — 방위를 지어내면
    없는 길을 물어보게 된다. 시작점의 갈래도 이웃이 알려주는 것이어야 한다."""
    p = Provider({})
    res = run(p, {})
    assert p.probes == []
    assert [f["reason"] for f in res.frontier] == ["neighbors_missing"]


def test_시작점에_로드뷰가_없으면_no_coverage():
    p = Provider({})
    p.nearest = lambda *a, **k: None
    res = run(p, {})
    assert res.stop_reason == "no_coverage"
    assert res.calls == 0


def test_provider가_이름붙여_터뜨린_실패는_삼키지_않는다():
    """ProviderError(노드 응답 형식 변경 등)를 neighbors_missing 으로 뭉개면
    원인이 사라진다. 형식이 바뀐 것과 렌더가 실패한 것은 다른 사실이다."""
    p = Provider({"S": [nb("A", 90.0)]})

    def boom(_pano):
        raise ProviderError("이웃 응답 파싱 실패 — 형식이 바뀐 것으로 보인다")

    p.neighbors = boom
    with pytest.raises(ProviderError):
        run(p, {})


def test_provider가_예외를_던져도_지어내지_않는다():
    """neighbors() 가 터지는 것도 '갈래 없음' 이 아니다. ProviderError 는 위로
    던지지만(→ 앞 테스트) 그 밖의 예외는 이 갈래만 접는다 — 어느 쪽이든 추측
    방위로 걷지는 않는다."""
    p = Provider({"S": [nb("A", 90.0)]})

    def boom(_pano):
        raise RuntimeError("스니핑 실패")

    p.neighbors = boom
    res = run(p, {})
    assert p.probes == [], "이웃도 모르는데 무언가를 물었다"
    assert p.nearest_calls == 1, "좌표를 밀어 다른 pano 를 만들었다"
    assert [f["reason"] for f in res.frontier] == ["neighbors_missing"]


# ── 분기 ───────────────────────────────────────────────────────────────────

def test_갈림길의_갈래를_전부_간다():
    """갈림길에서 하나를 고르지 않는다. 산책로로 판정된 갈래는 전부 큐에 든다."""
    p = Provider({"S": [nb("A", 90.0), nb("C", 270.0)],
                  "A": [nb("S", 270.0), nb("A2", 90.0)],
                  "C": [nb("S", 90.0), nb("C2", 270.0)],
                  "A2": [nb("A", 270.0)], "C2": [nb("C", 90.0)]})
    res = run(p, {("S", 90.0): True, ("S", 270.0): True,
                  ("A", 90.0): True, ("C", 270.0): True})
    ids = {n["pano_id"] for n in res.nodes}
    assert {"S", "A", "C", "A2", "C2"} <= ids
    assert res.stop_reason == "exhausted"


def test_같은_pano는_한_번만_판정한다():
    """마름모: S→A→D 와 S→B→D. 첫 접근의 판정이 D 의 판정이다."""
    p = Provider({"S": [nb("A", 45.0), nb("B", 315.0)],
                  "A": [nb("D", 315.0)], "B": [nb("D", 45.0)], "D": [nb("A", 135.0)]})
    res = run(p, {("S", 45.0): True, ("S", 315.0): True,
                  ("A", 315.0): True, ("B", 45.0): True})
    d_probes = [pr for pr in res.probes if pr["to_pano"] == "D"]
    assert len(d_probes) == 1
    assert sum(1 for n in res.nodes if n["pano_id"] == "D") == 1


def test_아니라고_판정된_pano도_다시_묻지_않는다():
    """A 쪽에서 D 를 거절했으면 B 쪽에서 또 묻지 않는다. 호출은 돈이다."""
    p = Provider({"S": [nb("A", 45.0), nb("B", 315.0)],
                  "A": [nb("D", 315.0)], "B": [nb("D", 45.0)], "D": [nb("A", 135.0)]})
    res = run(p, {("S", 45.0): True, ("S", 315.0): True})
    assert len([pr for pr in res.probes if pr["to_pano"] == "D"]) == 1


def test_온_길은_다시_묻지_않는다():
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0), nb("B", 90.0)],
                  "B": [nb("A", 270.0)]})
    run(p, {("S", 90.0): True})
    assert ("A", 270.0) not in p.probes


def test_시작점_이후에는_다시_상한이_걸린다():
    """전부 묻기는 시작점 한정이다. 매 노드 그러면 호출이 갈림길 수만큼 곱해진다."""
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 80.0), nb("C", 100.0), nb("D", 60.0)],
                  "B": [], "C": [], "D": []})
    run(p, {("S", 90.0): True}, max_candidates=2)
    at_a = [h for pid, h in p.probes if pid == "A"]
    assert len(at_a) == 2, f"시작점 뒤에도 상한이 안 걸린다: {at_a}"


def test_온_길은_각도가_아니라_pano_id로_빠진다():
    """U턴 방지로 max_turn_deg 를 걸었었다. 지웠다.

    온 길은 came_from 이 **pano_id 로 정확히** 빼므로 각도 필터가 할 일이
    없었다. 그래서 크게 꺾이는 **다른** 이웃은 후보에 남는다 — 골목이 예각으로
    갈라지는 곳에서 멀쩡한 갈래를 각도만 보고 지우지 않는다.
    """
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 90.0), nb("SHARP", 220.0)],
                  "B": [], "SHARP": []})
    run(p, {("S", 90.0): True})
    assert ("A", 270.0) not in p.probes, "온 길(S)을 다시 물었다"
    assert ("A", 220.0) in p.probes, "130° 꺾이는 이웃이 각도만으로 사라졌다"


def test_그래프_막다른_길은_폴백으로_새지_않는다():
    """폴백으로 새면 로드뷰에 없는 길을 만들어낸다."""
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0)]})
    res = run(p, {("S", 90.0): True})
    assert res.stop_reason == "exhausted"
    assert p.nearest_calls == 1, "폴백 스냅이 돌았다"


# ── 예산 ───────────────────────────────────────────────────────────────────

def test_예산_안에서_끝나면_frontier가_비어_있다():
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0)]})
    res = run(p, {("S", 90.0): True})
    assert res.frontier == []
    assert res.stop_reason == "exhausted"


# ── "아님" 판정도 확장한다 ──────────────────────────────────────────────────

def test_아님_판정도_확장해서_건너편_산책로를_찾는다():
    """차도 pano 에서 시작하는 실측 시나리오. 차도(false)를 다리 삼아 건너면
    램프(true)가 나온다 — 예전 설계는 여기서 2호출 만에 죽었다."""
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 180.0)],
                  "B": [nb("A", 0.0)]})
    res = run(p, {("A", 180.0): True})           # S→A 는 차도(false), A→B 가 산책로
    assert {n["pano_id"] for n in res.nodes} == {"S", "A", "B"}
    by_id = {n["pano_id"]: n for n in res.nodes}
    assert by_id["A"]["is_trail"] is False       # 다리였다는 사실이 남는다
    assert by_id["B"]["is_trail"] is True
    assert by_id["S"]["is_trail"] is None        # 시작 노드는 판정 전


def test_큐는_발견_순서대로_비운다():
    """판정은 소비 순서를 바꾸지 않는다. 한때 산책로 갈래 큐를 먼저 비웠는데,
    그러면 소비 순서가 depth 를 따르지 않아 너비 우선이 깨진다 — "아님" 갈래의
    depth 2 가 산책로 갈래의 depth 8 뒤로 밀린다."""
    p = Provider({"S": [nb("X", 0.0), nb("A", 90.0)],
                  "X": [nb("S", 180.0), nb("X2", 0.0)],
                  "A": [nb("S", 270.0), nb("A2", 90.0)],
                  "X2": [nb("X", 180.0)], "A2": [nb("A", 270.0)]})
    run(p, {("S", 90.0): True})
    # S 에서 X(아님)·A(산책로)를 물었으면, 세 번째 호출은 먼저 발견된 X 쪽이다
    assert p.probes[2][0] == "X"


# ── 그래프 provider 의 이웃 로드 실패 ──────────────────────────────────────

def test_이웃을_못_얻은_갈래는_neighbors_missing으로_남긴다():
    """2026-08-18 청계천 실주행(차도 시작, 50호출)에서 노드 22개 중 12개가
    이웃 로드에 실패해 호출 34/50 이 좌표 밀기로 샜다. 빈 목록은 "갈래 없음" 이
    아니라 렌더/스니핑 실패다 — 추측 방위로 걷지 않고 frontier 에 남긴다."""
    # pano id 는 그 실주행에서 실제로 폴백에 빠졌던 것들이다
    p = Provider({"1212370258": [nb("1039598393", 33.0, lat=37.5696, lng=127.0051)],
                  "1039598393": []},                    # ← 이웃 로드 실패
                 start=("1212370258", 37.5695, 127.005))
    res = run(p, {("1212370258", 33.0): True})
    assert p.nearest_calls == 1, "좌표 밀기 스냅이 돌았다"
    assert all(pr["to_pano"] is not None for pr in res.probes), "추측 방위를 물었다"
    assert [(f["pano_id"], f["reason"]) for f in res.frontier] == \
        [("1039598393", "neighbors_missing")]


def test_이웃_실패_뒤에도_다른_갈래는_계속_간다():
    """한 갈래의 실패가 탐색 전체를 끝내지 않는다 — 갈래가 남아 있다."""
    p = Provider({"S": [nb("DEAD", 0.0), nb("LIVE", 90.0)],
                  "DEAD": [],                                   # ← 이웃 로드 실패
                  "LIVE": [nb("S", 270.0), nb("FAR", 90.0)],
                  "FAR": [nb("LIVE", 270.0)]})
    res = run(p, {("S", 0.0): True, ("S", 90.0): True, ("LIVE", 90.0): True})
    assert res.stop_reason == "exhausted"
    assert "FAR" in [n["pano_id"] for n in res.nodes], "한 갈래 실패로 탐색이 죽었다"
    assert [(f["pano_id"], f["reason"]) for f in res.frontier] == \
        [("DEAD", "neighbors_missing")]


def test_두_갈래가_같은_pano로_모이면_한_번만_간다():
    """마름모꼴 골목. 같은 pano 를 두 노드에서 접근할 수 있다 — 한 번만 확장한다."""
    p = Provider({"S": [nb("L", 0.0), nb("R", 90.0)],
                  "L": [nb("MERGED", 45.0)],
                  "R": [nb("MERGED", 45.0)],
                  "MERGED": []})
    res = run(p, {("S", 0.0): True, ("S", 90.0): True,
                  ("L", 45.0): True, ("R", 45.0): True})
    assert sum(1 for n in res.nodes if n["pano_id"] == "MERGED") == 1


# ── 거리 예산 ───────────────────────────────────────────────────────────────

def test_거리_예산_밖은_확장하지_않고_frontier에_남긴다():
    """depth 와 같은 처리다 — 반경 밖은 안 본 것이지 없는 것이 아니다."""
    p = Provider({"S": [nb("NEAR", 0.0, lat=north(10))],
                  "NEAR": [nb("FAR", 0.0, lat=north(30))],
                  "FAR": [nb("BEYOND", 0.0, lat=north(50))],
                  "BEYOND": []})
    res = run(p, {("S", 0.0): True, ("NEAR", 0.0): True}, max_distance_m=25.0)
    assert ("FAR", 0.0) not in p.probes, "반경 밖 노드를 확장했다"
    assert "FAR" in [n["pano_id"] for n in res.nodes], "마킹조차 안 했다"
    assert [(f["pano_id"], f["reason"]) for f in res.frontier] == \
        [("FAR", "distance_budget")]


def test_거리는_누적이_아니라_직선이다():
    """되돌아오는 갈래가 예산을 먹지 않는다.

    누적이면 S→A(30m)→B(다시 1m) 가 59m 로 계산돼 40m 예산에서 죽는다.
    직선이면 B 는 시작점에서 1m 라 멀쩡히 확장된다.
    """
    p = Provider({"S": [nb("A", 0.0, lat=north(30))],
                  "A": [nb("B", 180.0, lat=north(1))],
                  "B": []})
    res = run(p, {("S", 0.0): True, ("A", 180.0): True}, max_distance_m=40.0)
    assert [f["reason"] for f in res.frontier] == ["neighbors_missing"], \
        "되돌아온 갈래가 거리 예산에 걸렸다 — 누적으로 재고 있다"


def test_거리_기준점은_요청_좌표가_아니라_스냅된_pano다():
    """요청 좌표 기준이면 스냅이 튄 만큼(최대 snap_radius_m)을 아무것도 안
    하고 까먹는다. 웹이 그리는 원과 그만큼 어긋나는 대신, 예산이 루프가
    실제로 간 거리를 재게 된다."""
    p = Provider({"S": [nb("A", 0.0, lat=north(1000))],
                  "A": [nb("B", 0.0, lat=north(1100))],
                  "B": []},
                 start=("S", north(100), 127.0))       # 요청 좌표보다 100m 북쪽
    run(p, {("S", 0.0): True, ("A", 0.0): True}, max_distance_m=950.0)
    assert ("A", 0.0) in p.probes, \
        "스냅점에서 900m 인데 요청 좌표 기준(1000m)으로 잘렸다"


# ── 시간 예산 ───────────────────────────────────────────────────────────────

class _Clock:
    """가짜 시계. 판정 1건 = 1초로 친다 — 실시간을 기다리면 flaky 해진다."""

    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now


class _TickingClient(Client):
    def __init__(self, provider, verdicts, clock):
        super().__init__(provider, verdicts)
        self.clock = clock

    def assess(self, uri, *, heading=None):
        self.clock.now += 1.0
        return super().assess(uri, heading=heading)


def test_시간_예산은_노드_경계가_아니라_후보마다_걸린다(monkeypatch):
    """한 노드의 후보가 max_candidates 개까지 되므로, 노드 경계에서만 보면
    캡처가 느릴 때 그 노드 하나가 통째로 예산을 넘겨 실행된다."""
    clock = _Clock()
    monkeypatch.setattr(explore_mod, "time", clock)
    p = Provider({"S": [nb("A", 0.0), nb("B", 90.0), nb("C", 180.0), nb("D", 270.0)],
                  "A": [], "B": [], "C": [], "D": []})
    res = run(p, {}, client=_TickingClient(p, {}, clock), max_seconds=1.5)
    assert res.calls == 2, f"후보 루프 안에서 안 끊었다: {res.calls}호출"
    assert res.stop_reason == "time_budget"
    # 못 물은 후보 둘(D·C)과, "아님" 판정으로 큐에 들어갔다 못 간 둘(A·B).
    # 어느 쪽도 버리지 않는다 — 판정을 안 받았을 뿐 갈래는 갈래다
    assert {f["pano_id"] for f in res.frontier} == {"A", "B", "C", "D"}
    assert {f["reason"] for f in res.frontier} == {"time_budget"}


# ── 경고 ───────────────────────────────────────────────────────────────────

def test_캡처_실패가_결과에_남는다():
    """⚠️ 조용한 실패였다. 캡처 실패는 판정이 아니라 probes 에 못 넣고, 갈래가
    사라진 것도 아니라 frontier 에도 안 넣었다 — 그래서 후보가 전부 실패해도
    런이 exhausted 로 정상 종료한 것처럼 보였고, log=None 이면 흔적조차 없었다.
    """
    p = Provider({"S": [nb("A", 0.0), nb("B", 90.0)], "A": [], "B": []})

    def boom(_pano, _hdg):
        raise RuntimeError("렌더 실패")

    p.capture = boom
    res = run(p, {})
    assert res.stop_reason == "exhausted"        # 런 자체는 계속된다
    w = next(w for w in res.warnings if w["code"] == "capture_failed")
    assert w["count"] == 2, "방향마다 세야 한다"


def test_no_coverage는_stop_reason과_warning_둘_다다():
    """다른 질문에 답하는 두 필드다 — "결과가 완결됐나" 와 "사용자에게 뭐라고
    말하나". 웹은 뒤쪽만 읽어도 된다."""
    p = Provider({})
    p.nearest = lambda *a, **k: None
    res = run(p, {})
    assert res.stop_reason == "no_coverage"
    w = next(w for w in res.warnings if w["code"] == "no_coverage")
    assert "로드뷰가 없다" in w["message"]


def test_이웃_로드_실패는_한_줄로_모인다():
    """갈래마다 나므로 1회성이면 시끄럽다 — 실주행에서 22노드 중 12개였다."""
    p = Provider({"S": [nb("A", 0.0), nb("B", 90.0)], "A": [], "B": []})
    res = run(p, {("S", 0.0): True, ("S", 90.0): True})
    w = next(w for w in res.warnings if w["code"] == "neighbors_missing")
    assert w["count"] == 2


def test_이미지_무시는_터지지_않고_반환된다():
    """한때 stop_reason 을 세팅한 **직후 raise** 했다. 그 res 는 호출자에게
    반환되지 않으므로 런로그에는 "aborted" 가 남았다 — 세팅한 값이 아무도
    못 읽는 객체 위에 있었다."""
    from trailwalk.vlm import ImageIgnoredError

    p = Provider({"S": [nb("A", 0.0)], "A": []})

    class Ignoring(Client):
        def assess(self, uri, *, heading=None):
            raise ImageIgnoredError("prompt_tokens 90 < 198")

    res = run(p, {}, client=Ignoring(p, {}))
    assert res.stop_reason == "image_ignored"
    assert [w["code"] for w in res.warnings] == ["image_ignored"]

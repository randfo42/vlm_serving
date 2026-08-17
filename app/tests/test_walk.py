"""탐색 루프 — 방향 결정과 종료 조건.

VLM 도 브라우저도 없이 돈다. 여기서 검증하는 것은 판정 **품질**이 아니라
판정을 받은 뒤의 **행동**이다. 그 둘은 다르고, 섞으면 둘 다 못 잰다.

특히 지키려는 것:

- 온 길로 되돌아가지 않는다 (그래프의 가장 큰 이점이 이거다)
- 그래프 막다른 길이 좌표 밀기로 **새지 않는다**. 새면 없는 길을 만들어낸다
- 갈림길에서 하나만 따라가되 나머지를 버리지 않는다
"""
import pytest

from conftest import make_image
from trailwalk.providers.base import Neighbor, Pano
from trailwalk.walk import WalkConfig, walk


class Verdict:
    def __init__(self, is_trail):
        self.is_trail = is_trail
        self.confidence = None


class Provider:
    """이웃 그래프를 흉내낸다. graph 가 비면 walk 는 좌표 밀기로 되돌아간다."""

    name = "fake"

    def __init__(self, graph=None, start=("S", 37.5, 127.0)):
        self.graph = graph or {}
        self.start = Pano(pano_id=start[0], lat=start[1], lng=start[2])
        self.probes = []          # (pano_id, heading) — 무엇을 물었는지
        self.nearest_calls = 0
        self._img = make_image(size=(320, 180))

    def nearest(self, lat, lng, radius_m):
        self.nearest_calls += 1
        if self.nearest_calls == 1:
            return self.start
        # 폴백 경로: 밀린 좌표마다 새 pano 를 만든다
        return Pano(pano_id=f"p{self.nearest_calls}", lat=lat, lng=lng)

    def neighbors(self, pano):
        return list(self.graph.get(pano.pano_id, []))

    def capture(self, pano, heading, fov_deg):
        self.probes.append((pano.pano_id, round(heading, 1)))
        return self._img

    def close(self):
        pass


class Client:
    """probes 의 마지막 항목을 보고 판정을 돌려준다.

    verdicts 에 없는 조합은 False. "명시한 것만 산책로" 라서 테스트가
    실수로 통과하는 일이 없다.
    """

    def __init__(self, provider, verdicts):
        self.provider = provider
        self.verdicts = verdicts

    def assess(self, uri, *, heading=None):
        return Verdict(self.verdicts.get(self.provider.probes[-1], False))


def nb(pano_id, heading, lat=37.5, lng=127.0):
    return Neighbor(pano_id=pano_id, heading=heading, lat=lat, lng=lng)


def run(provider, verdicts, bearing=90.0, **cfg):
    client = Client(provider, verdicts)
    return walk(provider, client, (37.5, 127.0), bearing, WalkConfig(**cfg))


# ── 그래프 순회 ─────────────────────────────────────────────────────────────

def test_직선_구간은_스텝당_한_번만_묻는다():
    """온 길을 빼면 후보가 1개다. '전부 물어보기' 가 공짜인 이유가 이것이다."""
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 90.0)],
                  "B": [nb("A", 270.0)]})
    res = run(p, {("S", 90.0): True, ("A", 90.0): True}, max_steps=2)
    assert p.probes == [("S", 90.0), ("A", 90.0)]
    assert res.used_graph is True


def test_온_길은_후보에서_빠진다():
    """⚠️ pano_id 로 정확히 지운다. 각도로 어림하면 곡선에서 틀린다."""
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 88.0)]})
    run(p, {("S", 90.0): True, ("A", 88.0): True}, max_steps=2)
    assert ("A", 270.0) not in p.probes, "온 길을 다시 물었다"


def test_이미_밟은_pano도_후보에서_빠진다():
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("B", 90.0)],
                  "B": [nb("S", 180.0), nb("A", 270.0)]})
    run(p, {("S", 90.0): True, ("A", 90.0): True,
            ("B", 180.0): True, ("B", 270.0): True}, max_steps=5)
    assert ("B", 180.0) not in p.probes and ("B", 270.0) not in p.probes


def test_그래프_막다른_길은_좌표밀기로_새지_않는다():
    """⚠️ 여기서 폴백으로 새면 로드뷰에 없는 길을 걸어간 것처럼 보인다.
    '길이 끝났다' 와 '스냅에 실패했다' 는 완전히 다른 사실이다."""
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0)]})
    res = run(p, {("S", 90.0): True}, max_steps=10)
    assert res.stop_reason == "dead_end"
    assert res.used_graph is True
    assert p.nearest_calls == 1, "폴백 스냅이 돌았다"


def test_그래프_이동은_다시_스냅하지_않는다():
    """이웃이 좌표를 들고 오므로 스냅이 필요 없다. 스냅은 오차를 더할 뿐이다."""
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("B", 90.0)], "B": []})
    run(p, {("S", 90.0): True, ("A", 90.0): True}, max_steps=3)
    assert p.nearest_calls == 1


def test_많이_꺾이는_이웃은_후보에서_뺀다():
    """U턴 방지. 뒤로 도는 것은 '다른 길' 이 아니라 온 길이다."""
    p = Provider({"S": [nb("BACK", 275.0), nb("A", 95.0)]})
    run(p, {("S", 95.0): True}, max_steps=1, max_turn_deg=120.0)
    assert ("S", 275.0) not in p.probes


# ── 갈림길 ─────────────────────────────────────────────────────────────────

def test_갈림길에서_정면을_고르고_나머지를_남긴다():
    """한 지점에서 여러 방향이 동시에 산책로일 수 있다. 하나만 따라가되
    나머지를 버리지 않는다 — 분기 탐색을 붙일 때 이게 그대로 입력이다."""
    p = Provider({"S": [nb("A", 92.0), nb("C", 10.0)], "A": [], "C": []})
    res = run(p, {("S", 92.0): True, ("S", 10.0): True}, bearing=90.0, max_steps=1)
    assert res.path[0]["n_trails"] == 2
    assert [f["pano_id"] for f in res.frontier] == ["C"]
    assert res.frontier[0]["from_pano"] == "S"


def test_산책로가_하나뿐이면_frontier가_비어_있다():
    p = Provider({"S": [nb("A", 92.0), nb("C", 10.0)], "A": []})
    res = run(p, {("S", 92.0): True}, max_steps=1)
    assert res.frontier == []


def test_그래프에서는_기본이_전부_묻기다():
    p = Provider({"S": [nb("A", 92.0), nb("C", 10.0)], "A": []})
    run(p, {("S", 92.0): True, ("S", 10.0): True}, max_steps=1)
    assert len(p.probes) == 2, "첫 성공에서 멈췄다"


def test_first_hit로_바꾸면_첫_성공에서_멈춘다():
    p = Provider({"S": [nb("A", 92.0), nb("C", 10.0)], "A": []})
    run(p, {("S", 92.0): True, ("S", 10.0): True}, max_steps=1, probe_all=False)
    assert len(p.probes) == 1


# ── 좌표 밀기 폴백 ──────────────────────────────────────────────────────────

def test_이웃이_없으면_폴백으로_돈다():
    p = Provider({})                       # 그래프 없음
    res = run(p, {("S", 90.0): True}, max_steps=1)
    assert res.used_graph is False
    assert p.nearest_calls >= 1


def test_폴백의_기본은_첫_성공에서_멈춤이다():
    """폴백 후보는 실제 갈래가 아니라 추측(bearing, ±60°)이고 항상 3개다.
    전부 물으면 매 스텝 3배인데 얻는 게 없다."""
    p = Provider({})
    run(p, {("S", 90.0): True}, max_steps=1)
    assert len(p.probes) == 1


def test_폴백에서_직진이_막히면_좌우를_본다():
    p = Provider({})
    run(p, {("S", 30.0): True}, max_steps=1)
    assert p.probes == [("S", 90.0), ("S", 30.0)]


# ── 종료 조건 ───────────────────────────────────────────────────────────────

def test_계속_아니면_dead_end로_멈춘다():
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("B", 90.0)], "B": [nb("C", 90.0)],
                  "C": [nb("D", 90.0)], "D": []})
    res = run(p, {}, max_steps=10, miss_tolerance=2)
    assert res.stop_reason == "dead_end"


def test_한두_장_실패는_참는다():
    """나무 그늘이나 역광 한 장 때문에 멀쩡한 길을 포기하는 일이 실제로 있다."""
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("B", 90.0)], "B": [nb("C", 90.0)],
                  "C": []})
    res = run(p, {("B", 90.0): True}, max_steps=5, miss_tolerance=2)
    assert res.steps >= 3, "한 번 실패했다고 바로 멈췄다"


def test_no_coverage와_dead_end를_섞지_않는다():
    """앞은 지도 사업자가 안 찍은 것이고 뒤는 모델의 판정이다.
    정확도를 볼 때 한 통에 넣으면 모델이 억울해진다."""
    p = Provider({})
    p.nearest = lambda *a, **k: None
    res = run(p, {}, max_steps=3)
    assert res.stop_reason == "no_coverage"


def test_max_steps에서_멈춘다():
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("B", 90.0)], "B": [nb("C", 90.0)]})
    res = run(p, {("S", 90.0): True, ("A", 90.0): True, ("B", 90.0): True}, max_steps=2)
    assert res.stop_reason == "max_steps" and res.steps == 2


def test_기본_설정이_호출_간에_공유되지_않는다():
    """cfg 를 기본 인자로 두면 인스턴스 하나가 모든 호출에 공유된다.
    지금은 아무도 안 고치지만, 고치는 순간 다음 런에 조용히 새어 간다."""
    p = Provider({"S": [nb("A", 90.0)], "A": []})
    client = Client(p, {("S", 90.0): True})
    r1 = walk(p, client, (37.5, 127.0), 90.0)
    r2 = walk(Provider({"S": [nb("A", 90.0)], "A": []}),
              Client(p, {("S", 90.0): True}), (37.5, 127.0), 90.0)
    assert r1.frontier == [] and r2.frontier == []


@pytest.mark.parametrize("tol", [1, 3])
def test_재방문이_한도를_넘으면_멈춘다(tol):
    """같은 pano 를 계속 밟으면 탐색이 제자리를 맴돈다."""
    p = Provider({})
    p.nearest = lambda *a, **k: Pano(pano_id="LOOP", lat=37.5, lng=127.0)
    res = run(p, {("LOOP", 90.0): True}, max_steps=50, revisit_tolerance=tol)
    assert res.stop_reason == "revisit_loop"
    assert res.steps <= tol + 2

"""분기 탐색 — 큐 소비와 예산.

test_walk.py 와 같은 원칙이다: VLM 도 브라우저도 없이, 판정 **품질**이 아니라
판정을 받은 뒤의 **행동**을 검증한다. 가짜 provider/client 도 그쪽 것을 그대로
쓴다 — 두 루프가 같은 판정 배선을 공유한다는 사실 자체가 지킬 불변식이다.

특히 지키려는 것:

- 시작점에서는 모든 방향을 본다 (walk 와 달리 "정면" 이 없다)
- 갈림길에서 갈래를 버리지 않고 전부 간다
- pano 하나에 판정 하나 — 두 경로로 접근해도 다시 묻지 않는다
- 예산(depth/호출)에 걸린 갈래는 사라지지 않고 frontier 에 남는다
- 한 갈래의 no_coverage 가 탐색 전체를 멈추지 않는다 (walk 와 다른 점)
"""
from test_walk import Client, Provider, nb

from trailwalk.explore import ExploreConfig, explore
from trailwalk.providers.base import Pano


def run(provider, verdicts, bearing=0.0, **cfg):
    client = Client(provider, verdicts)
    return explore(provider, client, (37.5, 127.0), bearing, ExploreConfig(**cfg))


# ── 시작 노드 ───────────────────────────────────────────────────────────────

def test_시작점에서_모든_이웃을_묻는다():
    """walk 는 정면부터 보지만 explore 의 시작점에는 "온 길" 이 없다."""
    p = Provider({"S": [nb("A", 90.0), nb("B", 270.0), nb("C", 0.0)],
                  "A": [nb("S", 270.0)], "B": [nb("S", 90.0)], "C": [nb("S", 180.0)]})
    run(p, {})
    assert set(p.probes) == {("S", 90.0), ("S", 270.0), ("S", 0.0)}


def test_폴백_시작점은_전방향을_본다():
    """그래프가 없으면 이웃 목록이 없다. start_offsets 로 전방향을 흉내낸다."""
    p = Provider({})
    res = run(p, {}, max_vlm_calls=4)
    assert p.probes == [("S", 0.0), ("S", 90.0), ("S", 180.0), ("S", 270.0)]
    assert res.used_graph is False


def test_시작점에_로드뷰가_없으면_no_coverage():
    p = Provider({})
    p.nearest = lambda *a, **k: None
    res = run(p, {})
    assert res.stop_reason == "no_coverage"
    assert res.calls == 0


# ── 분기 ───────────────────────────────────────────────────────────────────

def test_갈림길의_갈래를_전부_간다():
    """walk 는 하나를 고르고 나머지를 frontier 에 남긴다. explore 는 다 간다."""
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


def test_그래프_막다른_길은_폴백으로_새지_않는다():
    """walk 와 같은 불변식. 새면 로드뷰에 없는 길을 만들어낸다."""
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0)]})
    res = run(p, {("S", 90.0): True})
    assert res.stop_reason == "exhausted"
    assert p.nearest_calls == 1, "폴백 스냅이 돌았다"


# ── 예산 ───────────────────────────────────────────────────────────────────

def test_max_depth에_걸린_노드는_frontier에_남는다():
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("B", 90.0)], "B": [nb("C", 90.0)]})
    res = run(p, {("S", 90.0): True, ("A", 90.0): True, ("B", 90.0): True},
              max_depth=2)
    assert ("B", 90.0) not in p.probes, "depth 한계 너머를 물었다"
    assert [f["pano_id"] for f in res.frontier] == ["B"]
    assert res.frontier[0]["reason"] == "max_depth"


def test_call_budget에서_멈추고_나머지를_frontier에_남긴다():
    p = Provider({"S": [nb("A", 90.0), nb("B", 180.0), nb("C", 270.0)]})
    res = run(p, {("S", 90.0): True, ("S", 180.0): True, ("S", 270.0): True},
              max_vlm_calls=2)
    assert res.calls == 2
    assert res.stop_reason == "call_budget"
    # 못 물은 C + 큐에 남은 A, B 가 전부 frontier 다
    assert {f["pano_id"] for f in res.frontier} == {"A", "B", "C"}


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


def test_expand_non_trail을_끄면_예전처럼_갈래가_죽는다():
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 180.0)], "B": [nb("A", 0.0)]})
    res = run(p, {("A", 180.0): True}, expand_non_trail=False)
    assert [n["pano_id"] for n in res.nodes] == ["S"]
    assert ("A", 180.0) not in p.probes


def test_산책로_갈래_큐를_먼저_비운다():
    """예산은 산책로를 따라가는 데 먼저 쓰인다. 다리(아님)는 그다음이다."""
    p = Provider({"S": [nb("A", 0.0), nb("X", 90.0)],
                  "A": [nb("S", 180.0), nb("A2", 0.0)],
                  "X": [nb("S", 270.0), nb("X2", 90.0)],
                  "A2": [nb("A", 180.0)], "X2": [nb("X", 270.0)]})
    run(p, {("S", 0.0): True, ("A", 0.0): True}, max_vlm_calls=3)
    # S 에서 A(산책로)·X(아님)를 물은 뒤, 세 번째 호출은 X 가 아니라 A 쪽이어야 한다
    assert p.probes[2][0] == "A"


# ── 폴백 갈래 ──────────────────────────────────────────────────────────────

def test_갈래의_no_coverage는_탐색_전체를_멈추지_않는다():
    """walk 는 외길이라 no_coverage 가 곧 끝이다. explore 는 다른 갈래가 남아 있다."""
    p = Provider({})
    real_nearest = p.nearest

    def nearest(lat, lng, r):
        pano = real_nearest(lat, lng, r)
        return pano if p.nearest_calls == 1 else None   # 시작만 성공, 갈래는 전부 미촬영

    p.nearest = nearest
    res = run(p, {("S", 0.0): True, ("S", 90.0): True})
    assert res.stop_reason == "exhausted"
    assert [n["pano_id"] for n in res.nodes] == ["S"]


def test_폴백_갈래가_같은_pano로_스냅되면_한_번만_간다():
    """격자 스냅 중복. 두 갈래가 같은 자리로 모이면 한 번만 확장한다."""
    p = Provider({})
    calls = {"n": 0}

    def nearest(lat, lng, r):
        # 첫 호출(시작)은 S, 이후 갈래 스냅은 전부 같은 pano 로 모은다
        calls["n"] += 1
        if calls["n"] == 1:
            return p.start
        return Pano(pano_id="MERGED", lat=37.5001, lng=127.0)

    p.nearest = nearest
    res = run(p, {("S", 0.0): True, ("S", 90.0): True}, max_depth=1)
    assert sum(1 for n in res.nodes if n["pano_id"] == "MERGED") == 1

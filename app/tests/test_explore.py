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


def test_시작점의_이웃을_못_얻으면_아무것도_묻지_않는다():
    """전방향을 흉내내는 start_offsets 가 여기 있었다. 지웠다 — 방위를 지어내면
    없는 길을 물어보게 된다. 시작점의 갈래도 이웃이 알려주는 것이어야 한다."""
    p = Provider({})
    res = run(p, {}, max_vlm_calls=4)
    assert p.probes == []
    assert [f["reason"] for f in res.frontier] == ["neighbors_missing"]


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


# ── 그래프 provider 의 이웃 로드 실패 ──────────────────────────────────────

def test_이웃을_못_얻은_갈래는_neighbors_missing으로_남긴다():
    """2026-08-18 청계천 실주행(차도 시작, 50호출)에서 노드 22개 중 12개가
    이웃 로드에 실패해 호출 34/50 이 좌표 밀기로 샜다. 빈 목록은 "갈래 없음" 이
    아니라 렌더/스니핑 실패다 — 추측 방위로 걷지 않고 frontier 에 남긴다."""
    # pano id 는 그 실주행에서 실제로 폴백에 빠졌던 것들이다
    p = Provider({"1212370258": [nb("1039598393", 33.0)],
                  "1039598393": []},                    # ← 이웃 로드 실패
                 start=("1212370258", 37.5695, 127.005))
    res = run(p, {("1212370258", 33.0): True})
    assert p.nearest_calls == 1, "좌표 밀기 스냅이 돌았다"
    assert all(pr["to_pano"] is not None for pr in res.probes), "추측 방위를 물었다"
    assert [(f["pano_id"], f["reason"]) for f in res.frontier] == \
        [("1039598393", "neighbors_missing")]


def test_이웃_실패_뒤에도_다른_갈래는_계속_간다():
    """walk 는 외길이라 실패가 곧 끝이지만, explore 는 갈래가 남아 있다."""
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
                  ("L", 45.0): True, ("R", 45.0): True}, max_depth=3)
    assert sum(1 for n in res.nodes if n["pano_id"] == "MERGED") == 1

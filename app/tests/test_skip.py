"""노드 건너뛰기 — 무엇을 안 찍고, 무엇은 그대로인가.

건너뛰기가 지켜야 하는 것은 "적게 찍는다" 가 아니라 **"적게 찍어도 그래프는
같다"** 다. 캡처와 판정만 빠지고 이웃 탐색·확장·frontier 는 그대로여야,
성글게 돈 런과 빽빽하게 돈 런을 같은 지도로 읽을 수 있다.

깨지면 조용히 틀리는 자리가 셋이다:

  1. 건너뛴 노드를 확장까지 안 하면 **지도가 작아진다.** 판정이 성근 게
     아니라 탐색이 잘린 것인데, 결과만 보면 구분이 안 된다
  2. 주기를 전역 카운터로 세면 BFS 의 큐 순서에 따라 어느 갈래가 몇 번째인지가
     달라져, 같은 설정의 두 런이 다른 곳을 찍는다
  3. run_collect 가 같은 규칙을 따로 구현하면 언젠가 한쪽만 고쳐지고,
     "explore 가 보냈을 바로 그 N 장" 이 거짓이 된다 (→ test_collect.py)
"""
import math
import threading
from dataclasses import replace

import pytest

from conftest import Client, Provider, nb
from trailwalk import geo, settings
from trailwalk.explore import ExploreConfig, cadence, explore

_M_PER_DEG = geo.R * math.pi / 180


def north(m: float, base: float = 37.5) -> float:
    return base + m / _M_PER_DEG


def _cfg(**over):
    """정본에서 출발해 준 것만 덮는다 — 테스트가 자기 기본값을 갖지 않게."""
    return replace(ExploreConfig.from_settings(settings.SETTINGS), **over)


def _line(n: int) -> dict:
    """갈림길 없는 직선 n+1 노드. 각 노드의 갈래는 (온 길 빼면) 하나뿐이다."""
    g = {}
    for i in range(n + 1):
        here, nxt, prev = f"p{i}", f"p{i + 1}", f"p{i - 1}"
        g[here] = ([nb(prev, 180.0, lat=north(10 * (i - 1)))] if i else []) + \
                  ([nb(nxt, 0.0, lat=north(10 * (i + 1)))] if i < n else [])
    return g


def _run(graph, cfg, start="p0"):
    p = Provider(graph, start=(start, 37.5, 127.0))
    res = explore(p, Client(p, {}), (37.5, 127.0), 0.0, cfg)
    return p, res


# ── 주기 ────────────────────────────────────────────────────────────────────

def test_직선에서는_한_번_찍고_넷을_건너뛴다():
    """정본 값(1찍고 4건너뛰기)이 실제로 그 주기인가."""
    p, _res = _run(_line(12), _cfg())
    shot = sorted(int(pid[1:]) for pid, _h in p.probes)
    assert shot == [0, 5, 10], f"주기가 5가 아니다: {shot}"


def test_skip_steps가_0이면_전부_찍는다():
    """끄는 방법은 이것 하나다. 갈림길 예외에 손잡이를 두지 않은 이유이기도 하다."""
    p, res = _run(_line(6), _cfg(skip_steps=0))
    assert sorted(int(pid[1:]) for pid, _h in p.probes) == [0, 1, 2, 3, 4, 5]
    assert res.skipped == 0


def test_주기는_부모에서_자식으로_이어진다_전역이_아니다():
    """⚠️ 전역 카운터로 세면 BFS 가 갈래를 번갈아 소비하는 순간 어긋난다.

    갈래 둘이 각각 자기 시작점부터 세야, 같은 설정의 두 런이 같은 곳을 찍는다.
    """
    # S 에서 동/서로 갈라져 각각 직선으로 뻗는다
    g = {"S": [nb("E1", 90.0), nb("W1", 270.0)]}
    for side, brg in (("E", 90.0), ("W", 270.0)):
        for i in range(1, 7):
            prev = "S" if i == 1 else f"{side}{i - 1}"
            g[f"{side}{i}"] = [nb(prev, (brg + 180) % 360),
                               nb(f"{side}{i + 1}", brg)]
        g[f"{side}6"] = [nb(f"{side}5", (brg + 180) % 360)]
    p, _res = _run(g, _cfg(), start="S")
    shot = {pid for pid, _h in p.probes}
    # S 는 갈림길이라 찍힌다. 그다음 각 갈래는 1,2,3,4 를 건너뛰고 5에서 찍는다
    assert shot == {"S", "E5", "W5"}, shot


# ── 갈림길 ──────────────────────────────────────────────────────────────────

def test_갈림길은_주기와_무관하게_전부_찍는다():
    """갈래가 갈리는 곳에서 건너뛰면 두 갈래가 무엇이었는지 영영 모른 채
    둘 다 확장하게 된다."""
    g = _line(6)
    # p2 에 곁가지를 붙여 갈림길로 만든다 (주기상 p2 는 건너뛸 자리다)
    g["p2"] = g["p2"] + [nb("X", 90.0, lng=127.001)]
    g["X"] = [nb("p2", 270.0)]
    p, _res = _run(g, _cfg())
    shot_at_p2 = {h for pid, h in p.probes if pid == "p2"}
    assert shot_at_p2 == {0.0, 90.0}, \
        f"갈림길인데 건너뛰었거나 일부만 찍었다: {shot_at_p2}"


def test_갈림길은_주기를_리셋한다():
    """새 갈래는 자기 시작점부터 다시 센다 — 갈림길이 곧 0번이다."""
    g = _line(8)
    g["p1"] = g["p1"] + [nb("X", 90.0, lng=127.001)]
    g["X"] = [nb("p1", 270.0)]
    p, _res = _run(g, _cfg())
    shot = sorted(int(pid[1:]) for pid, _h in p.probes if pid.startswith("p"))
    # p0 찍고, p1 은 갈림길이라 찍고(리셋), 그다음 p2~p5 건너뛰고 p6
    assert shot == [0, 1, 1, 6], shot


# ── 그래프는 그대로다 ───────────────────────────────────────────────────────

def test_건너뛴_노드도_이웃을_묻고_확장한다():
    """⚠️ 여기가 이 기능의 전부다. 확장까지 멈추면 판정이 성근 게 아니라
    지도가 작아진 것인데, 결과만 보면 구분할 수 없다."""
    dense, _ = _run(_line(12), _cfg(skip_steps=0))
    sparse, _ = _run(_line(12), _cfg())
    assert len(sparse.probes) < len(dense.probes), "건너뛰지 않았다"


def test_밟는_노드는_skip_설정과_무관하다():
    g = _line(12)
    _p, dense = _run(g, _cfg(skip_steps=0))
    _p, sparse = _run(g, _cfg())
    assert [n["pano_id"] for n in dense.nodes] == [n["pano_id"] for n in sparse.nodes]
    assert [f["pano_id"] for f in dense.frontier] == [f["pano_id"] for f in sparse.frontier]


def test_건너뛴_노드는_결과에_표시된다():
    """is_trail=None 하나로 뭉개면 '안 물어본 것' 과 '못 받은 것' 이 섞인다."""
    _p, res = _run(_line(12), _cfg())
    by_id = {n["pano_id"]: n for n in res.nodes}
    assert by_id["p0"]["skipped"] is False
    assert by_id["p1"]["skipped"] is True
    assert res.skipped == sum(1 for n in res.nodes if n["skipped"])


def test_건너뛴_부모에서_온_노드는_판정이_없다():
    """그 간선을 안 찍었으므로 판정이 있을 수 없다. 있으면 어딘가에서
    지어낸 것이다."""
    _p, res = _run(_line(12), _cfg())
    by_id = {n["pano_id"]: n for n in res.nodes}
    assert by_id["p2"]["is_trail"] is None      # p1 이 건너뛴 자리
    assert by_id["p1"]["is_trail"] is not None  # p0 은 찍었다


# ── 설정 검증 ───────────────────────────────────────────────────────────────

def test_run_steps가_0이면_터진다():
    """갈림길 말고는 아무 판정도 안 받는 런이 조용히 도는 것을 막는다."""
    s = settings.load()
    bad = replace(s, skip=replace(s.skip, run_steps=0))
    with pytest.raises(settings.SettingsError, match="run_steps"):
        ExploreConfig.from_settings(bad)


def test_skip_steps가_음수면_터진다():
    s = settings.load()
    bad = replace(s, skip=replace(s.skip, skip_steps=-1))
    with pytest.raises(settings.SettingsError, match="skip_steps"):
        ExploreConfig.from_settings(bad)


# ── cadence 자체 ────────────────────────────────────────────────────────────

def test_cadence는_판정을_인자로_받지_않는다():
    """⚠️ 판정이 skip 을 정하는 설계였다면 답을 기다려야 하고, 캡처와 VLM 을
    겹치는 것이 통째로 사라진다 (→ explore.py '캡처와 VLM 을 겹친다').
    시그니처가 그 사실을 강제한다."""
    import inspect
    assert list(inspect.signature(cadence).parameters) == ["pos", "fork", "cfg"]


def test_cadence_주기가_한_바퀴_돈다():
    cfg = _cfg(run_steps=2, skip_steps=3)
    pos, seq = 0, []
    for _ in range(10):
        shoot, pos = cadence(pos, False, cfg)
        seq.append(shoot)
    assert seq == [True, True, False, False, False] * 2


def test_막다른_길은_건너뛴_것으로_세지_않는다():
    """⚠️ 후보가 0개면 찍을 것이 없었던 것이지 건너뛴 것이 아니다. 한 카운터에
    섞으면 "판정이 적은 이유가 건너뛰기인가 막다른 길인가" 를 결과만 보고
    알 수 없다 — 이 카운터가 있는 이유가 정확히 그 구분이다."""
    # p1 이 막다른 길이고, 주기상 건너뛸 자리다
    g = {"p0": [nb("p1", 0.0, lat=north(10))], "p1": [nb("p0", 180.0)]}
    _p, res = _run(g, _cfg())
    by_id = {n["pano_id"]: n for n in res.nodes}
    assert by_id["p1"]["skipped"] is False, "갈 곳이 없던 노드를 건너뛴 것으로 셌다"
    assert res.skipped == 0


def test_건너뛰는_동안_서버가_죽으면_거기서_멈춘다():
    """⚠️ 건너뛰는 노드도 `pump` 로 실패를 알아챈다. 거기서 안 끊으면 서버가
    죽은 것을 알고도 건너뛰기 구간이 끝날 때까지 큐를 계속 넓힌다.

    실제로 이 순서가 나는 이유는 캡처와 VLM 이 겹쳐 돌기 때문이다 — 답이
    도착하는 시점이 그것을 띄운 노드가 아니라 **몇 노드 뒤**다. 그 몇 노드가
    건너뛰는 자리면 여기 검사가 없는 한 아무도 안 본다.

    타이밍을 재현하려고 판정을 게이트로 붙잡아 둔다: p0 의 판정은 루프가
    p1(건너뛰는 노드)에 도착할 때까지 도착하지 않는다.
    """
    from trailwalk.vlm import ServerDeadError

    gate, raised = threading.Event(), threading.Event()

    class Dying:
        def assess(self, uri, *, heading=None):
            gate.wait(5)
            try:
                raise ServerDeadError("Metal OOM 좀비")
            finally:
                raised.set()

    class Late(Provider):
        def neighbors(self, pano):
            if pano.pano_id == "p1":            # 건너뛰는 첫 노드
                gate.set()
                raised.wait(5)
                threading.Event().wait(0.2)     # future 가 done 으로 바뀔 틈
            return super().neighbors(pano)

    p = Late(_line(12), start=("p0", 37.5, 127.0))
    res = explore(p, Dying(), (37.5, 127.0), 0.0, _cfg())
    assert res.stop_reason == "server_dead"
    assert any(w["code"] == "server_dead" for w in res.warnings)
    seen = [n["pano_id"] for n in res.nodes]
    assert seen == ["p0", "p1"], f"죽은 것을 알고도 계속 넓혔다: {seen}"
    # ⚠️ 중단은 갈래를 **버릴 이유가 아니다.** p1 의 후보(p2)는 아직 큐에
    # 안 들어갔으므로 여기서 안 넣으면 어디에도 안 남는다 — 이어서 탐색해도
    # 그 갈래는 영영 입력이 안 된다 (파일 위 "버리지 않고 frontier 에 남긴다")
    assert "p2" in {f["pano_id"] for f in res.frontier}, \
        f"중단된 노드의 갈래가 사라졌다: {res.frontier}"


def test_어떤_이유로_끊기든_갈래는_어디에도_안_사라진다():
    """⚠️ 불변식: 밟은 노드에서 뻗은 모든 이웃은 **노드이거나 frontier** 다.
    한쪽에만 있어도 안 되고, 양쪽 다 없으면 그래프에 구멍이 난다.

    단, **확장한 노드**에 대해서만이다. frontier 에 든 노드는 확장을 안 한
    것이므로(반경 밖 · 이웃 실패 등) 그 이웃이 없는 것이 정상이다 — 이어서
    탐색하면 그 노드부터 다시 편다. 그래서 frontier 에 든 노드는 뺀다.

    노드를 중간에 접는 자리가 넷이다(시간 예산 · 거리 예산 · 이웃 실패 ·
    VLM 중단). 새 자리를 늘릴 때 이 테스트가 같이 안 늘면 여기서 터진다.
    """
    for name, cfg in (("시간 예산", _cfg(max_seconds=-1.0)),
                      ("거리 예산", _cfg(max_distance_m=25.0)),
                      ("예산 없음", _cfg())):
        p, res = _run(_line(12), cfg)
        halted = {f["pano_id"] for f in res.frontier}
        placed = {n["pano_id"] for n in res.nodes} | halted
        for n in res.nodes:                      # **확장한** 노드의 이웃은 전부
            if n["pano_id"] in halted:
                continue                         # 확장을 안 한 노드다
            for nbr in p.graph.get(n["pano_id"], []):
                assert nbr.pano_id in placed, \
                    f"[{name}] {n['pano_id']} → {nbr.pano_id} 갈래가 사라졌다"

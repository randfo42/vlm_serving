"""캡처만 모으기 — explore 와 **같은 순서로 같은 화각**을 찍는가.

이 파일이 지키는 것은 하나다: `run_collect.walk` 가 내는 (지점, 방위) 순열이
같은 설정의 `explore` 가 서버로 보내는 것과 정확히 같다는 것.

그게 깨지면 모아 둔 이미지로 VLM 을 재도 explore 의 부하를 잰 것이 아니게
된다 — 다른 장면 다발을 잰 것이다. 그런데도 숫자는 그럴듯하게 나오므로
사람이 알아챌 방법이 없다. 그래서 테스트가 본다.

같을 수 있는 근거는 explore 쪽 성질이다: 판정값이 확장 여부도 큐 순서도
바꾸지 않는다 (→ explore.py "아님 판정도 확장한다", "큐는 하나다").
그 성질이 깨지면 이 테스트가 먼저 터진다.
"""
import importlib.util
import math
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import Client, Provider, nb
from trailwalk import geo, settings
from trailwalk.explore import ExploreConfig, explore

APP = Path(__file__).resolve().parent.parent

_M_PER_DEG = geo.R * math.pi / 180


def north(m: float, base: float = 37.5) -> float:
    return base + m / _M_PER_DEG


def _load():
    """run_collect.py 는 스크립트라 import 되지 않는다 (→ app/CLAUDE.md 파일 구조).
    run_eval 을 테스트하는 방식과 같다."""
    spec = importlib.util.spec_from_file_location("run_collect", APP / "run_collect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(**over):
    """정본에서 출발해 인자로 준 것만 덮는다 — 테스트가 자기 기본값을 갖지 않게."""
    return replace(ExploreConfig.from_settings(settings.SETTINGS), **over)


def _graph():
    """갈림길이 있고, 되돌아오는 마름모가 있고, 막다른 길이 있는 그래프."""
    return {
        "S": [nb("A", 0.0, north(10)), nb("B", 90.0, 37.5, 127.001),
              nb("C", 180.0, north(-10))],
        "A": [nb("S", 180.0), nb("D", 0.0, north(20)), nb("E", 45.0, north(20), 127.001)],
        "B": [nb("S", 270.0), nb("D", 350.0, north(20))],     # 마름모 — D 로 다시 온다
        "C": [nb("S", 0.0)],                                   # 막다른 길
        "D": [nb("A", 180.0), nb("F", 0.0, north(30))],
        "E": [nb("A", 225.0)],
        "F": [nb("D", 180.0)],
    }


def _collected(provider, cfg, bearing=0.0):
    """walk 가 밟는 (지점, 방위) 를 순서대로. 예산 없이 끝까지."""
    mod = _load()
    seen = []

    def on_view(pano, hdg, n, depth):
        seen.append((pano.pano_id, round(hdg, 1)))
        return True

    r = mod.walk(provider, cfg, provider.start, bearing,
                 on_view, deadline=float("inf"))
    return seen, r


def _collected_until(provider, cfg, allow: int, monkeypatch, bearing=0.0):
    """`allow` 장에서 **시간 예산**이 끊도록 시계를 조작한다.

    장수 상한을 없앤 뒤 예산 축은 시간과 거리 둘뿐이다. 실제 시계로는 시간
    쪽을 못 재고(테스트가 1초도 안 걸린다) 그러면 예산의 절반이 무검증으로
    남는다. 캡처 1건 = 1초로 놓고 deadline 을 그 사이에 둔다.
    """
    mod = _load()
    seen = []
    now = [0.0]

    def on_view(pano, hdg, n, depth):
        seen.append((pano.pano_id, round(hdg, 1)))
        now[0] += 1.0
        return True

    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    r = mod.walk(provider, cfg, provider.start, bearing,
                 on_view, deadline=allow - 0.5)
    return seen, r


def test_explore_와_같은_순서로_같은_화각을_찍는다():
    cfg = _cfg(max_distance_m=1000.0, max_inflight=1000)

    ex_prov = Provider(_graph())
    # 판정을 섞어 준다 — 판정값이 순서를 바꾸지 않는다는 것이 요점이라
    # 전부 True 나 전부 False 로 두면 그 사실을 확인하지 못한다
    verdicts = {("S", 0.0): True, ("S", 90.0): False, ("A", 0.0): True,
                ("A", 45.0): False, ("B", 350.0): True, ("D", 0.0): False}
    res = explore(ex_prov, Client(ex_prov, verdicts), (37.5, 127.0), 0.0, cfg)
    expected = [(p["from_pano"], p["heading"]) for p in res.probes]

    got, r = _collected(Provider(_graph()), cfg)

    assert got == expected
    assert r["views"] == len(expected)
    assert r["stop"] == "exhausted"


def test_판정이_전부_뒤집혀도_같은_것을_찍는다():
    """explore 를 두 번 돌린다 — 판정만 정반대로.

    둘의 probes 가 같아야 "판정은 탐색을 안 바꾼다" 가 사실이고, 그래야
    VLM 없이 찍은 것이 explore 와 같다고 말할 수 있다.
    """
    cfg = _cfg(max_distance_m=1000.0, max_inflight=1000)
    keys = [("S", 0.0), ("S", 90.0), ("S", 180.0), ("A", 0.0), ("A", 45.0),
            ("B", 350.0), ("D", 0.0)]

    def probes(verdicts):
        p = Provider(_graph())
        res = explore(p, Client(p, verdicts), (37.5, 127.0), 0.0, cfg)
        return [(x["from_pano"], x["heading"]) for x in res.probes]

    all_true = probes(dict.fromkeys(keys, True))
    all_false = probes(dict.fromkeys(keys, False))
    assert all_true == all_false

    got, _ = _collected(Provider(_graph()), cfg)
    assert got == all_true


def test_장수로는_안_멈춘다(monkeypatch):
    """⚠️ 한때 `max_views` 가 있었고 그것이 이 스크립트를 틀리게 만들었다.

    예산 축은 explore 와 같은 둘뿐이어야 한다 — 시간과 거리. 장수로 끊으면
    "explore 가 보냈을 바로 그 N 장" 이 아니게 된다: 2026-08-23 GS25 반경
    500m 수집이 1000장에서 끊겨 398m 에서 멈췄고, 반경 안 2,493개 pano 중
    35.4% 만 밟았다. 반경이 끊을 자리를 장수가 끊고 있었다.
    """
    cfg = _cfg(max_distance_m=1000.0)
    got, r = _collected(Provider(_graph()), cfg)
    assert r["stop"] == "exhausted", "예산이 없으면 지도가 끝나야 끝난다"
    assert len(got) == r["views"] > 4


def test_시간이_다하면_time_budget_으로_멈춘다(monkeypatch):
    cfg = _cfg(max_distance_m=1000.0)
    got, r = _collected_until(Provider(_graph()), cfg, 4, monkeypatch)
    assert len(got) == 4
    assert r["views"] == 4
    assert r["stop"] == "time_budget"


def test_예산은_노드_경계가_아니라_후보마다_걸린다(monkeypatch):
    """시작 지점의 후보가 3개인데 2장에서 끊으면 2장이어야 한다.

    노드 경계에서만 봤다면 3장이 나온다 — 한 지점이 최대 max_candidates
    장이라 그 차이가 12장까지 벌어진다.
    """
    cfg = _cfg(max_distance_m=1000.0)
    got, _ = _collected_until(Provider(_graph()), cfg, 2, monkeypatch)
    assert got == [("S", 0.0), ("S", 90.0)]


def test_반경은_찍는_지점을_자른다_목표가_아니라():
    """반경 밖 노드는 **확장하지 않는다** — 찍는 자리(source)가 잘리는 것이지
    목표(to_pano)가 걸리는 게 아니다. explore 와 같은 규칙이다.

    반경 15m 에서 확장되는 것은 S(0m)·A(10m)·C(10m) 뿐이다. B 는 동쪽 88m 라
    밖이고, A 에서 뻗은 D(20m)·E 도 밖이라 목표로만 찍히고 확장은 안 된다.
    """
    cfg = _cfg(max_distance_m=15.0)
    got, _ = _collected(Provider(_graph()), cfg)

    # 찍은 자리는 반경 안 노드뿐이다 (C 는 이웃이 온 길뿐이라 후보가 없다)
    assert {p for p, _ in got} == {"S", "A"}
    # 목표는 반경 밖도 들어온다 — B(88m)·D(20m) 는 to_pano 로 찍힌다
    assert ("S", 90.0) in got and ("A", 0.0) in got
    # 그러나 그 밖 노드에서 다시 뻗지는 않는다
    assert ("D", 0.0) not in got and ("B", 350.0) not in got


def test_이웃을_못_얻으면_지어내지_않는다():
    """빈 이웃 목록은 '길 끝' 이 아니라 '로드 실패' 다 — 세고 넘어간다."""
    cfg = _cfg(max_distance_m=1000.0)
    got, r = _collected(Provider({"S": []}), cfg)
    assert got == []
    assert r["neighbors_missing"] == 1
    assert r["views"] == 0


def test_캡처_실패한_갈래는_확장하지_않는다():
    """판정이 없으면 그 갈래를 밟은 것이 아니다 — explore 도 그렇게 한다."""
    cfg = _cfg(max_distance_m=1000.0)
    mod = _load()
    prov = Provider(_graph())
    seen = []

    def on_view(pano, hdg, n, depth):
        seen.append((pano.pano_id, round(hdg, 1)))
        return pano.pano_id != "S"      # 시작 지점 캡처가 전부 실패한다

    r = mod.walk(prov, cfg, prov.start, 0.0, on_view, deadline=float("inf"))
    # S 의 후보 3개를 시도하고 전부 실패했으므로 큐가 비어 끝난다
    assert seen == [("S", 0.0), ("S", 90.0), ("S", 180.0)]
    assert r["capture_failed"] == 3
    assert r["views"] == 0
    assert r["stop"] == "exhausted"


@pytest.mark.parametrize("allow", [1, 3, 5, 8])
def test_예산이_어디서_끊든_앞부분은_같다(allow, monkeypatch):
    """예산은 자르기만 한다 — 순서를 바꾸지 않는다."""
    cfg = _cfg(max_distance_m=1000.0)
    full, _ = _collected(Provider(_graph()), cfg)
    got, _ = _collected_until(Provider(_graph()), cfg, allow, monkeypatch)
    assert got == full[:len(got)]

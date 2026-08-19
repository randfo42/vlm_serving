"""탐색 루프 — 방향 결정과 종료 조건.

VLM 도 브라우저도 없이 돈다. 여기서 검증하는 것은 판정 **품질**이 아니라
판정을 받은 뒤의 **행동**이다. 그 둘은 다르고, 섞으면 둘 다 못 잰다.

특히 지키려는 것:

- 온 길로 되돌아가지 않는다 (그래프의 가장 큰 이점이 이거다)
- **이동을 지어내지 않는다.** 이웃 목록을 못 얻은 것과 길이 끝난 것은 다른 사실이고,
  전자를 후자처럼 다루면 로드뷰에 없는 길을 걸어간 것처럼 보인다
- 갈림길에서 하나만 따라가되 나머지를 버리지 않는다
"""
from dataclasses import replace

import pytest

from conftest import make_image
from trailwalk import settings
from trailwalk.providers.base import Neighbor, Pano, ProviderError
from trailwalk.walk import WalkConfig, walk


class Verdict:
    def __init__(self, is_trail):
        self.is_trail = is_trail
        self.confidence = None


class Provider:
    """이웃 그래프를 흉내낸다. 빈 목록은 **로드 실패**로 읽힌다 (갈래 없음이 아니라)."""

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
        # 시작점 스냅은 한 번뿐이다. 그 뒤로 불리면 이동이 그래프를 안 쓴 것이다
        return Pano(pano_id=f"p{self.nearest_calls}", lat=lat, lng=lng)

    def neighbors(self, pano):
        return list(self.graph.get(pano.pano_id, []))

    def capture(self, pano, heading):
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
    """정본 설정에서 출발해 인자로 준 것만 덮어쓴다.

    기본값을 여기 다시 적지 않는 것이 요점이다 — 테스트가 자기만의 기본값을
    들고 있으면 설정 파일을 바꿔도 테스트는 예전 값으로 계속 통과한다.
    """
    client = Client(provider, verdicts)
    base = WalkConfig.from_settings(settings.SETTINGS)
    return walk(provider, client, (37.5, 127.0), bearing, replace(base, **cfg))


# ── 그래프 순회 ─────────────────────────────────────────────────────────────

def test_직선_구간은_스텝당_한_번만_묻는다():
    """온 길을 빼면 후보가 1개다. '전부 물어보기' 가 공짜인 이유가 이것이다."""
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 90.0)],
                  "B": [nb("A", 270.0)]})
    run(p, {("S", 90.0): True, ("A", 90.0): True}, max_steps=2)
    assert p.probes == [("S", 90.0), ("A", 90.0)]


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


def test_막다른_길은_이동을_지어내지_않는다():
    """이웃은 있는데 전부 온 길이면 막다른 길이다. 거기서 멈춘다.

    ⚠️ 예전에는 여기서 좌표 밀기로 샜고, 그러면 로드뷰에 없는 길을 걸어간
    것처럼 보였다. '길이 끝났다' 와 '스냅에 실패했다' 는 완전히 다른 사실이다.
    """
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0)]})
    res = run(p, {("S", 90.0): True}, max_steps=10)
    assert res.stop_reason == "dead_end"
    assert p.nearest_calls == 1, "시작점 말고 또 스냅했다 — 이동을 지어냈다"


def test_그래프_이동은_다시_스냅하지_않는다():
    """이웃이 좌표를 들고 오므로 스냅이 필요 없다. 스냅은 오차를 더할 뿐이다."""
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("B", 90.0)], "B": []})
    run(p, {("S", 90.0): True, ("A", 90.0): True}, max_steps=3)
    assert p.nearest_calls == 1


def test_온_길은_각도가_아니라_pano_id로_빠진다():
    """U턴 방지로 max_turn_deg(120°)를 걸었었다. 지웠다.

    온 길은 came_from 이 **pano_id 로 정확히** 빼므로 각도 필터가 할 일이
    없었다. 게다가 `turnable or fresh` 폴백 때문에 하드 필터도 아니었다 —
    전멸하면 통째로 무시됐다. explore 는 이미 180(=필터 없음)으로 돌았다.

    그래서 크게 꺾이는 **다른** 이웃은 이제 후보에 남는다. 골목이 예각으로
    갈라지는 곳에서 멀쩡한 갈래를 각도만 보고 지우지 않는다.
    """
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 90.0), nb("SHARP", 220.0)]})
    run(p, {("S", 90.0): True, ("A", 90.0): True}, max_steps=2)
    assert ("A", 270.0) not in p.probes, "온 길(S)을 다시 물었다"
    assert ("A", 220.0) in p.probes, "130° 꺾이는 이웃이 각도만으로 사라졌다"


# ── 시작 노드 ───────────────────────────────────────────────────────────────
#
# "갈 수 있는 화살표를 전부 보고, 하나라도 산책로면 산책로." 호출 수 = 화살표 수.

def test_시작점은_모든_이웃을_묻는다():
    """⚠️ 실측 회귀. `--bearing` 이 필터로 작동해 방향을 지우던 자리다.

    청계천 이웃은 동 91.4°/서 267.8° 인데 `--bearing 45` 를 주면 서쪽이
    137° 로 max_turn_deg(120°)에 걸려 **아예 안 물어봤다.** frontier 에도
    안 남아서 흔적이 없었다. 그 각도 필터는 이제 없지만(→ 위 테스트), 시작
    노드가 bearing 과 무관하게 전부 묻는다는 불변식은 그대로 지킨다.
    """
    p = Provider({"S": [nb("E", 91.4), nb("W", 267.8)], "E": [], "W": []})
    run(p, {("S", 91.4): True}, bearing=45.0, max_steps=1)
    assert {h for _, h in p.probes} == {91.4, 267.8}, f"방향이 사라졌다: {p.probes}"


def test_시작점은_max_candidates로도_안_자른다():
    """호출 수 = 화살표 개수. 갈림길에서 시작할 수 있어야 한다."""
    p = Provider({"S": [nb("A", 0.0), nb("B", 90.0), nb("C", 180.0), nb("D", 270.0)],
                  "A": [], "B": [], "C": [], "D": []})
    run(p, {("S", 0.0): True}, bearing=0.0, max_steps=1, max_candidates=2)
    assert len(p.probes) == 4, f"화살표 4개인데 {len(p.probes)}번만 물었다"


def test_시작점은_하나라도_산책로면_산책로다():
    p = Provider({"S": [nb("E", 91.4), nb("W", 267.8)], "W": []})
    res = run(p, {("S", 267.8): True}, bearing=90.0, max_steps=1)
    assert res.path[0]["is_trail"] is True, "정면이 아니라는 이유로 아님이 됐다"
    assert res.path[0]["n_trails"] == 1


def test_시작점의_모든_방향이_아니면_산책로가_아니다():
    p = Provider({"S": [nb("E", 91.4), nb("W", 267.8)], "E": [], "W": []})
    res = run(p, {}, bearing=90.0, max_steps=1)
    assert res.path[0]["is_trail"] is False
    assert len(p.probes) == 2, "판정이 아님이어도 화살표는 전부 봐야 한다"


def test_시작점_이후에는_다시_상한이_걸린다():
    """전부 묻기는 시작점 한정이다. 매 스텝 그러면 호출이 갈림길 수만큼 곱해진다."""
    p = Provider({"S": [nb("A", 90.0)],
                  "A": [nb("S", 270.0), nb("B", 80.0), nb("C", 100.0), nb("D", 60.0)]})
    run(p, {("S", 90.0): True, ("A", 80.0): True}, max_steps=2, max_candidates=2)
    at_a = [h for pid, h in p.probes if pid == "A"]
    assert len(at_a) == 2, f"시작점 뒤에도 상한이 안 걸린다: {at_a}"


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


# ── 이웃을 못 얻었을 때 ─────────────────────────────────────────────────────
#
# 실측으로 나온 실패다: SDK 는 띄운 적 있는 pano 의 노드 응답만 준다. 청계천
# 실주행에서 22개 중 12개가 실패했고, 그때는 좌표 밀기로 새면서 런이 멀쩡해
# 보였다 (→ docs/23-open-questions.md §7).

def test_이웃_목록을_못_얻으면_멈춘다():
    """빈 목록은 '갈래 없음' 이 아니다. 추측으로 걸으면 없는 길을 만들어낸다."""
    p = Provider({})                       # S 의 이웃을 못 얻는다
    res = run(p, {("S", 90.0): True}, max_steps=5)
    assert res.stop_reason == "neighbors_missing"
    assert p.probes == [], "이웃도 모르는데 무언가를 물었다"
    assert p.nearest_calls == 1, "좌표를 밀어 다른 pano 를 만들었다"


def test_이웃_실패와_막다른_길은_다른_이름으로_멈춘다():
    """정확도를 볼 때 이 둘을 한 통에 넣으면 모델이 억울해진다."""
    missing = Provider({})
    dead = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0)]})
    r1 = run(missing, {}, max_steps=5)
    r2 = run(dead, {("S", 90.0): True}, max_steps=5)
    assert r1.stop_reason == "neighbors_missing"
    assert r2.stop_reason == "dead_end"


def test_provider가_예외를_던져도_지어내지_않는다():
    """neighbors() 가 터지는 것도 '갈래 없음' 이 아니다."""
    p = Provider({"S": [nb("A", 90.0)]})

    def boom(_pano):
        raise RuntimeError("스니핑 실패")

    p.neighbors = boom
    res = run(p, {}, max_steps=5)
    assert res.stop_reason == "neighbors_missing"
    assert p.nearest_calls == 1


def test_provider가_이름붙여_터뜨린_실패는_삼키지_않는다():
    """ProviderError 는 provider 가 원인을 규명해 던진 것이다 (노드 응답 형식
    변경 등). neighbors_missing 으로 뭉개면 그 원인이 통째로 사라진다."""
    p = Provider({"S": [nb("A", 90.0)]})

    def boom(_pano):
        raise ProviderError("이웃 응답 파싱 실패 — 형식이 바뀐 것으로 보인다")

    p.neighbors = boom
    with pytest.raises(ProviderError):
        run(p, {}, max_steps=5)


# ── 캡처 실패 ───────────────────────────────────────────────────────────────
#
# 판정 불가(캡처 실패)와 "산책로 아님" 은 다른 사실이다. 전자를 후자로 세면
# 렌더 장애가 dead_end 로 둔갑하고, 결과만 보면 모델이 거절한 것처럼 보인다.

def test_전_후보_캡처_실패는_dead_end가_아니라_capture_failed다():
    p = Provider({"S": [nb("A", 90.0)], "A": [nb("S", 270.0)]})

    def boom(_pano, _heading):
        raise RuntimeError("렌더 죽음")

    p.capture = boom
    res = run(p, {}, max_steps=5)
    assert res.stop_reason == "capture_failed"
    assert res.path[0]["n_failed"] == 1
    assert res.path[0]["n_trails"] == 0


def test_미스_전진은_판정받은_후보로만_간다():
    """⚠️ 리뷰가 잡은 회귀. 정면(A) 캡처 실패 + 옆(B) "아님" 인 스텝에서
    miss-tolerance 전진이 cands[0](=A, 판정 없음)을 고르면 한 번도 판정되지
    않은 pano 로 걸어 들어가면서 path 에는 정상 스텝처럼 남는다."""
    p = Provider({"S": [nb("A", 90.0), nb("B", 180.0)],
                  "B": [nb("S", 0.0), nb("C", 180.0)], "C": [nb("B", 0.0)]})
    orig = Provider.capture

    def flaky(pano, heading):
        if (pano.pano_id, round(heading, 1)) == ("S", 90.0):
            raise RuntimeError("정면만 렌더 실패")
        return orig(p, pano, heading)

    p.capture = flaky
    res = run(p, {}, max_steps=5, miss_tolerance=2)
    walked = [step["pano_id"] for step in res.path]
    assert "A" not in walked, "판정 없는 후보로 걸어 들어갔다"
    assert walked[:2] == ["S", "B"]


def test_일부_캡처_실패는_기록만_남기고_계속_간다():
    p = Provider({"S": [nb("A", 90.0), nb("B", 180.0)],
                  "B": [nb("S", 0.0)]})
    orig = Provider.capture

    def flaky(pano, heading):
        if round(heading, 1) == 90.0:
            raise RuntimeError("이 방향만 렌더 실패")
        return orig(p, pano, heading)

    p.capture = flaky
    res = run(p, {("S", 180.0): True}, max_steps=5)
    assert res.path[0]["n_failed"] == 1
    assert res.path[0]["is_trail"] is True, "판정된 산책로로는 계속 가야 한다"
    assert res.stop_reason != "capture_failed"


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


def test_같은_pano를_두_번_밟지_않는다():
    """재방문 감지가 있었는데 지웠다 — 이제 구조적으로 불가능하기 때문이다.

    좌표 밀기 시절에는 밀린 좌표가 방금 온 pano 로 다시 스냅돼 제자리를
    맴돌 수 있었다. 지금은 `_candidates` 가 visited 를 후보에서 빼므로
    다음 지점이 항상 처음 밟는 곳이다. 그 불변식을 여기서 못박는다.
    """
    # 되돌아가는 간선이 잔뜩인 삼각형. 어디로든 돌아갈 수 있게 열어둔다
    p = Provider({"S": [nb("A", 90.0), nb("B", 180.0)],
                  "A": [nb("S", 270.0), nb("B", 180.0)],
                  "B": [nb("S", 0.0), nb("A", 0.0)]})
    res = run(p, {("S", 90.0): True, ("A", 180.0): True, ("B", 0.0): True},
              max_steps=50)
    stepped = [row["pano_id"] for row in res.path]
    assert len(stepped) == len(set(stepped)), f"같은 pano 를 다시 밟았다: {stepped}"
    assert res.stop_reason == "dead_end"


def test_호출_예산은_후보마다_걸린다():
    """⚠️ 리뷰 회귀. 예산을 스텝 경계에서만 보면 하드 캡이 아니다.

    시작 노드는 max_candidates 슬라이싱도 안 받으므로(_candidates 의
    came_from is None 분기), 이웃이 4개면 max_vlm_calls=1 이어도 4번 부르고
    나서야 멈췄다. "실질 상한은 호출 수" 라는 주석이 사실이 아니었다.
    """
    p = Provider({"S": [nb("A", 0.0), nb("B", 90.0), nb("C", 180.0), nb("D", 270.0)]})
    res = run(p, {}, bearing=0.0, max_vlm_calls=1)     # 정면이 A 가 되게
    assert res.calls == 1
    assert p.probes == [("S", 0.0)]
    assert res.stop_reason == "call_budget"
    # 못 물은 후보는 버리지 않는다 — 판정을 안 받았을 뿐 갈래는 갈래다
    assert {f["pano_id"] for f in res.frontier} == {"B", "C", "D"}


def test_예산_컷에서_이미_확인한_산책로를_버리지_않는다():
    """⚠️ 리뷰 회귀. 호출을 써서 얻은 판정이 로그 어디에도 안 남던 자리다.

    후보 순회 중간에 예산이 끊기면, 아직 안 물어본 후보만 frontier 로 가고
    **이미 True 로 확인된 갈래는 증발**했다. path 에도 안 남는다(스텝을
    마무리하지 않으므로). frontier 의 계약이 "산책로인데 안 간 갈래" 다.
    """
    p = Provider({"S": [nb("A", 0.0), nb("B", 90.0), nb("C", 180.0)]})
    res = run(p, {("S", 0.0): True}, bearing=0.0, max_vlm_calls=1)
    assert res.calls == 1
    assert res.stop_reason == "call_budget"
    ids = {f["pano_id"] for f in res.frontier}
    assert "A" in ids, "산책로로 확인된 갈래가 사라졌다"
    assert {"B", "C"} <= ids, "안 물어본 갈래가 사라졌다"

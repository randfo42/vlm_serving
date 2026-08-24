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

### 캡처와 VLM 을 겹친다 (2026-08-22)

판정 하나 = 캡처 대기 + VLM 대기이고, 실측에서 그 둘이 반반이었다. 그래서
캡처한 즉시 서버로 띄우고 **답을 기다리지 않은 채 다음 캡처로 간다.**

겹칠 수 있는 이유는 바로 위의 두 성질이다 — 판정값은 확장 여부도 큐 순서도
바꾸지 않는다. 답이 필요한 곳은 기록할 때뿐이다. 그래서 기다리는 자리를
핫패스에서 전부 뺐다: `nodes[].is_trail` 조차 출력 전용이라 자리만 잡아 두고
런 끝에 채운다 (한 군데라도 남기면 겹치기가 통째로 사라진다 — 실제로 그렇게
짰다가 속도가 하나도 안 붙었다).

**줄을 세우는 것은 서버의 일이다.** 워커 수를 조여도 큐가 vLLM 에서 이
스레드풀로 옮겨올 뿐이고, 큐잉·동시성은 app 의 관심사가 아니다 (→ 루트
CLAUDE.md). 순서는 워커 수와 무관하게 지켜진다 — `resolve` 가 FIFO 로만
받으므로 probes·런로그는 보낸 순서 그대로다 (재현성).

실제로 띄워지는 개수는 상한이 아니라 **캡처 속도**가 정한다. 2026-08-22
약수역 500m 실측(원격 vLLM, `--max-num-seqs 1`)에서 서버 대기열은 초반
10분간 2~4 였다가 그 뒤로는 0~1 로 내려앉았다 — 그 시점부터 병목은 VLM 이
아니라 캡처다. 캡처만 따로 떠 두려면 `run_collect.py` 를 쓴다.

손잡이는 `vlm.max_inflight` 이고 0 이 옛 동작이다 (→ 20-app-design.md).

### 노드를 건너뛴다 (2026-08-23)

`run_steps` 개를 찍고 `skip_steps` 개를 건너뛴다. 건너뛴 노드도 **이웃은 묻고
확장한다** — 빠지는 것은 캡처와 판정뿐이라 그래프의 모양은 그대로다.

싼 이유가 실측에 있다: 노드 하나가 캡처까지 하면 2.07초인데 이웃만 물으면
0.10초다 (2026-08-23 약수역사거리). 즉 건너뛴 노드는 20분의 1 값이다.

**예산 축이 아니다.** 런을 멈추는 것은 여전히 시간과 거리 둘뿐이고(위 참조),
이건 노드 하나의 비용을 줄인다. 한때 있던 `max_views` 와 성격이 다르다 —
그건 "몇 장 모으면 그만둔다" 라 반경을 다 안 돌고 끝났다.

갈림길(갈래 ≥ 2)에서는 주기와 무관하게 전부 찍는다. 왜 그런지와 왜 끄는
손잡이가 없는지는 아래 `cadence` 에 있다.

⚠️ **대가는 지면 커버리지다.** pano 간격이 ~10m 이고 노면을 판별할 수 있는
거리가 4~20m 라(→ docs/23-open-questions.md §3 의 화각 실측에서 유도), 1/5 로
성글게 하면 표본 간격이 ~50m 가 되어 **안 본 지면이 생긴다.** 판정이 빽빽할
필요가 없는 용도(대략적 마킹·커버리지 조사)에서만 켤 것. 정확도를 재는
런에서는 `skip_steps: 0` 이다.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from . import geo, settings
from . import warn as warn_mod
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

    # 답을 안 받은 채 서버에 띄워 둘 판정 수. 캡처와 VLM 을 겹치게 하는 손잡이다
    max_inflight: int

    # 찍을 노드와 건너뛸 노드의 주기 (→ 아래 `cadence`). 건너뛴 노드도 이웃은
    # 묻고 확장한다 — 그래프는 그대로고 판정만 성글어진다
    run_steps: int
    skip_steps: int

    # 캡처한 바이트를 서버로 보낼 때의 규칙 (크기·품질). 루프가 imaging 에
    # 넘긴다 — 모듈 상수로 읽게 두면 --config 가 무시된다
    image: object

    @classmethod
    def from_settings(cls, s) -> ExploreConfig:
        # 타입은 settings 가 보지만 의미는 여기서 본다. run_steps 가 0 이면
        # 갈림길 말고는 아무것도 안 찍는 런이 조용히 돌고, 리포트에서는 그냥
        # 판정이 적은 런으로 보인다 — 이 레포가 막으려는 실패 방식이다.
        if s.skip.run_steps < 1:
            raise settings.SettingsError(
                f"skip.run_steps 는 1 이상이어야 한다 ({s.skip.run_steps}).\n"
                f"  0 이면 갈림길 외에는 아무 판정도 안 받는다. skip 을 끄려면\n"
                f"  skip_steps 를 0 으로 둘 것 — 그러면 전부 찍는다.")
        if s.skip.skip_steps < 0:
            raise settings.SettingsError(
                f"skip.skip_steps 는 0 이상이어야 한다 ({s.skip.skip_steps}). 0 = 안 건너뛴다.")
        return cls(
            max_seconds=s.budget.max_seconds,
            max_distance_m=s.budget.max_distance_m,
            max_candidates=s.candidates.max_candidates,
            snap_radius_m=s.geo.snap_radius_m,
            max_inflight=s.vlm.max_inflight,
            run_steps=s.skip.run_steps,
            skip_steps=s.skip.skip_steps,
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
    stop_reason: str = ""    # exhausted = 갈 곳을 다 갔다. canceled = 사람이 멈췄다.
    #                          나머지는 예산/오류
    # 런 도중 사람이 알아야 할 일 (→ trailwalk/warn.py). stop_reason 과 역할이
    # 다르다: 저쪽은 "완결된 결과인가", 이쪽은 "믿어도 되나 · 뭐라고 말하나".
    # log=None 으로 도는 호출(테스트·인프로세스 웹)에서 유일한 창구이기도 하다
    warnings: list[dict] = field(default_factory=list)
    calls: int = 0
    # 방향을 안 물어본 노드 수 (→ `cadence`). 이웃은 묻고 확장했으므로
    # 그래프에는 들어 있다 — nodes[].skipped 가 어느 것인지 말해준다
    skipped: int = 0
    wall_s: float = 0.0
    # 스냅된 시작점. 예산이 즉시 끊겨 nodes 가 비어도 지도는 원점을 그려야
    # 하므로 경계가 이 값을 필요로 한다 (→ docs/23-open-questions.md §9).
    # 스냅 전에 끝난 런(no_coverage·provider_error)은 None — 그것도 정확한
    # 정보다. 호출자가 요청 좌표를 대신 쓴다
    origin: tuple[float, float] | None = None
    origin_pano: str | None = None


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


def cadence(pos: int, fork: bool, cfg: ExploreConfig) -> tuple[bool, int]:
    """이 노드의 방향들을 찍을 것인가, 그리고 자식에게 물려줄 위치.

    반환: (찍는다, 자식의 pos)

    **그래프 모양만 본다 — 판정값을 읽지 않는다.** 그게 이 기능이 위의
    "캡처와 VLM 을 겹친다" 를 안 깨는 이유다. 판정이 skip 여부를 정하는
    설계였다면 답을 기다려야 하고 겹치기가 통째로 사라진다.

    직선 구간에서는 `run_steps` 개를 찍고 `skip_steps` 개를 건너뛰기를
    반복한다. pos 는 그 주기 안의 위치이고, 부모에서 자식으로 이어진다 —
    **전역 카운터가 아니다.** BFS 는 여러 갈래를 번갈아 소비하므로 전역으로
    세면 어느 갈래가 몇 번째인지가 큐 순서에 따라 달라지고, 그러면 같은
    설정의 두 런이 다른 곳을 찍는다.

    ### 갈림길은 언제나 찍는다 (끄는 손잡이 없음)

    갈래가 둘 이상인 지점에서는 주기와 무관하게 **모든 방향을 찍는다.**
    갈림길이야말로 "여기서 어디로 갈 수 있는가" 가 갈리는 곳이고, 거기서
    건너뛰면 두 갈래가 무엇이었는지 영영 모른 채 둘 다 확장하게 된다.

    끄는 손잡이를 두지 않는 이유는 `expand_non_trail` 을 지운 것과 같다 —
    끄면 "갈림길은 본다" 가 정책이 아니게 되고, 끈 런과 안 끈 런을 비교할 수
    없다. skip 을 통째로 끄려면 `skip.skip_steps: 0` 으로 두면 된다.

    갈림길은 주기도 리셋한다. 새 갈래는 자기 시작점부터 다시 센다.
    """
    eff = 0 if fork else pos
    return eff < cfg.run_steps, (eff + 1) % (cfg.run_steps + cfg.skip_steps)


@dataclass
class _Pending:
    """서버에 보내 놓고 아직 답을 안 받은 판정 하나.

    캡처는 이미 끝났고, 서버가 답하는 동안 **다음 캡처가 돈다.** 이게 가능한
    이유는 위 "아님 판정도 확장한다" 와 "큐는 하나다" 다 — 판정값은 큐의
    순서도 확장 여부도 바꾸지 않으므로 답을 기다릴 이유가 기록할 때밖에 없다.
    판정이 그래프 모양을 정하는 설계였다면 이 겹치기는 성립하지 않는다.
    """
    fut: Future
    pano: Pano             # 어디서 찍었나 (런로그용)
    from_pano: str
    heading: float
    nb: Neighbor
    depth: int
    raw: bytes             # 캡처 원본. 답이 와야 런로그에 함께 남길 수 있다
    src_format: str
    is_trail: bool | None = None
    resolved: bool = False


@dataclass
class _Node:
    depth: int
    bearing: float                 # 이 노드에 들어온 진행 방위. 후보 정렬의 기준
    came_from: str | None          # 부모 pano_id
    pano: Pano                     # 이웃이 곧 다음 지점이라 큐에 들어올 때 이미 안다
    # 이 노드를 발견한 판정. 아직 서버에서 안 왔을 수 있어 **결과가 아니라
    # 그 자리(_Pending)** 를 들고 있다가 큐에서 꺼낼 때 값을 받는다.
    # 시작 노드만 None (판정 없이 시작한다)
    found_by: _Pending | None = None
    # 찍고/건너뛰기 주기 안의 위치 (→ `cadence`). 부모에게서 물려받는다
    pos: int = 0


def explore(provider, client, start: tuple[float, float], start_bearing: float = 0.0,
            cfg: ExploreConfig | None = None, log=None,
            cancel: Callable[[], bool] | None = None) -> ExploreResult:
    """start 에서 모든 방향으로 산책로 그래프를 넓힌다.

    `cancel` 이 참을 돌려주면 `stop_reason="canceled"` 로 **정상 종료**한다 —
    부분 결과는 유효하고, 못 간 갈래는 frontier 에 남는다. 확인 시점은 예산
    검사와 같은 두 곳뿐이라, 취소 지연은 최대 max_inflight × VLM 지연이다
    (in-flight 판정은 이미 값을 치렀으므로 받아서 기록한다 — pump 안에서
    끊으면 FIFO 불변식이 깨진다).
    """
    # 기본 인자로 ExploreConfig(...) 를 두면 인스턴스 하나가 호출 간에 공유된다
    cfg = cfg or ExploreConfig.from_settings(settings.SETTINGS)
    res = ExploreResult()
    t0 = time.time()
    tallies: dict[str, dict] = {}

    def budget_stop() -> str | None:
        """멈출 이유, 없으면 None. 취소를 먼저 본다 — 예산이 남았어도
        사람이 그만두라고 했다. 둘 다 frontier 를 남기는 정상 종료다."""
        if cancel is not None and cancel():
            return "canceled"
        if time.time() - t0 > cfg.max_seconds:
            return "time_budget"
        return None

    # 워커 수 = max_inflight. **줄을 세우는 것은 서버의 일이다** — 여기서
    # 워커를 조이면 큐가 vLLM 이 아니라 이 스레드풀로 옮겨올 뿐이고, 그건
    # app 이 하면 안 되는 일이다 (→ 루트 CLAUDE.md: 큐잉·동시성은 서빙 관심사).
    #
    # 스레드는 필요한 만큼만 생긴다 (ThreadPoolExecutor 가 게으르게 만든다).
    # 실제 동시 요청 수는 캡처 속도가 정한다 — 캡처가 한 번에 하나씩 나오므로
    # 띄워 둔 개수는 "VLM 지연 ÷ 캡처 간격" 근처에서 저절로 멈춘다.
    #
    # 순서는 워커 수와 무관하게 지켜진다: `resolve` 가 FIFO 로만 받으므로
    # probes·런로그는 보낸 순서 그대로다 (→ 아래 resolve).
    pool = ThreadPoolExecutor(max_workers=max(1, cfg.max_inflight),
                              thread_name_prefix="vlm")
    pending: deque[_Pending] = deque()   # 띄워 두고 아직 안 받은 것들 (FIFO)
    abort: dict = {}                     # 첫 VLM 실패. 뒤따르는 것은 메아리다

    def warn(code: str, **detail) -> None:
        """1회성 경고. 결과와 런로그 **양쪽에** 넣는다 — 갈라지면 log=None 인
        호출에서만 신호가 사라지고, 그게 정확히 테스트가 도는 조건이다."""
        res.warnings.append(warn_mod.make(code, **detail))
        if log:
            log.warn(code, **detail)

    def tally(code: str, **detail) -> None:
        """집계형. 갈래마다 나는 것들은 런 끝에 한 줄로 모은다."""
        t = tallies.setdefault(code, {"count": 0})
        t["count"] += 1
        t.update({k: v for k, v in detail.items() if k != "count"})
        if log:
            log.tally(code, **detail)

    def done(reason: str) -> ExploreResult:
        """종료 처리 한 곳. 집계형을 결과에 flatten 하고 시계를 멈춘다."""
        res.stop_reason = reason
        for code, d in tallies.items():
            res.warnings.append(warn_mod.make(code, **d))
        res.wall_s = time.time() - t0
        # 띄워 둔 호출이 남은 채로 돌아가면 스레드가 프로세스를 붙잡는다
        pool.shutdown(wait=True)
        return res

    def submit(p: Pano, hdg: float, depth: int, nb: Neighbor) -> _Pending | None:
        """한 방향을 캡처해서 **서버에 띄운다.** None 은 캡처 실패.

        캡처는 여기서 끝내고 판정은 기다리지 않는다. 답을 기다리는 자리는
        `resolve()` 하나뿐이고, 그 사이에 다음 캡처가 돈다.
        """
        try:
            raw = provider.capture(p, hdg)
        except Exception as e:
            if log:
                log.event("capture_failed", step=depth, pano_id=p.pano_id,
                          heading=round(hdg, 1), error=f"{type(e).__name__}: {e}")
            # 판정이 아니라서 probes 에 못 넣고, 갈래가 사라진 것도 아니라서
            # frontier 에도 안 넣는다. 그래서 한때 **아무 데도 안 남았다** —
            # 후보가 전부 실패해도 런이 exhausted 로 정상 종료한 것처럼 보였다
            tally("capture_failed", pano_id=p.pano_id, heading=round(hdg, 1))
            return None
        uri, src_format = view_to_data_uri(raw, cfg.image)
        pd = _Pending(fut=pool.submit(client.assess, uri, heading=hdg),
                      pano=p, from_pano=p.pano_id, heading=hdg, nb=nb,
                      depth=depth, raw=raw, src_format=src_format)
        pending.append(pd)
        return pd

    def resolve(pd: _Pending) -> None:
        """답 하나를 받아 기록한다. **FIFO 로만 불린다** — 직렬로 돌 때와
        probes·런로그의 순서가 같아야 두 런을 비교할 수 있다 (재현성).
        """
        pd.resolved = True
        try:
            v = pd.fut.result()
        except (ImageIgnoredError, ServerDeadError, VlmError) as e:
            # 첫 실패만 남긴다. 뒤따라 온 것들은 같은 원인의 메아리다
            if not abort:
                abort["code"] = ("image_ignored" if isinstance(e, ImageIgnoredError)
                                 else "server_dead" if isinstance(e, ServerDeadError)
                                 else "vlm_error")
                abort["error"] = str(e)[:400]
            return
        res.calls += 1
        pd.is_trail = v.is_trail
        if log:
            log.probe(step=pd.depth, pano_id=pd.pano.pano_id, lat=pd.pano.lat,
                      lng=pd.pano.lng, heading=pd.heading, verdict=v,
                      src_format=pd.src_format, image=pd.raw)
        # to_* 는 그리기/UI 용이다. 후보가 곧 목표 pano 라 항상 채워진다.
        res.probes.append({"from_pano": pd.from_pano, "heading": round(pd.heading, 1),
                           "to_pano": pd.nb.pano_id, "to_lat": pd.nb.lat,
                           "to_lng": pd.nb.lng, "is_trail": v.is_trail,
                           "depth": pd.depth})

    def pump(keep: int) -> None:
        """받을 수 있는 것을 받는다. 띄워 둔 것이 keep 개 이하가 될 때까지는
        기다려서라도 받는다.

        **이미 도착한 답을 먼저 치우는 것이 중요하다.** 안 그러면 실패를
        max_inflight 개만큼 늦게 알아채고, 그 사이 루프가 한 노드를 더
        처리해서 없던 경고(neighbors_missing 등)를 만들어낸다 — 경고는
        사람이 읽는 것이라 그런 잡음이 끼면 안 된다.
        """
        while pending and pending[0].fut.done() and not abort:
            resolve(pending.popleft())
        while len(pending) > keep and not abort:
            resolve(pending.popleft())


    def note_abort(step: int) -> None:
        """첫 VLM 실패를 경고와 stop_reason 으로 승격한다.

        **두 자리에서 부른다** — 찍는 노드와 건너뛰는 노드. 건너뛰는 쪽에도
        있어야 하는 이유는 그쪽도 `pump` 로 실패를 알아채기 때문이다. 여기서
        안 끊으면 서버가 죽은 뒤에도 건너뛰기 구간이 끝날 때까지 큐를 계속
        넓힌다 (skip_steps 만큼).

        한때 이 실패들은 stop_reason 을 세팅한 **직후 raise** 했다. 그 res 는
        호출자에게 반환되지 않으므로 런로그에는 "aborted" 가 남았다 — 세팅한
        값이 아무도 못 읽는 객체 위에 있었다. 시끄럽게 죽는 역할은 러너가
        stop_reason 을 보고 맡는다.
        """
        if log:
            log.event(abort["code"], step=step, error=abort["error"])
        warn(abort["code"], error=abort["error"])
        res.stop_reason = abort["code"]

    def unwalked(node: _Node, rest, reason: str) -> list[dict]:
        """아직 큐에 안 넣은 후보들을 frontier 형태로.

        **판정을 안 받았을 뿐 갈래는 갈래다.** 여기서 버리면 그 갈래는 큐에도
        frontier 에도 없어 이어서 탐색할 때 입력으로 안 들어온다 — 파일 위
        "예산에 걸려 못 간 갈래는 버리지 않는다" 가 그 자리에서만 깨진다.
        예산·중단으로 노드를 중간에 접는 자리 **전부**가 이걸 부른다.
        """
        return [{"from_pano": node.pano.pano_id, "pano_id": n.pano_id,
                 "lat": n.lat, "lng": n.lng,
                 "depth": node.depth + 1, "reason": reason} for _h, n in rest]

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
        warn("provider_error", error=f"{type(e).__name__}: {str(e).splitlines()[0]}")
        return done("provider_error")
    if start_pano is None:
        # 시작점에 로드뷰가 없다. 갈래 하나가 아니라 탐색 전체가 성립하지 않는다
        if log:
            log.event("no_coverage", step=0, lat=start[0], lng=start[1])
        warn("no_coverage", radius_m=cfg.snap_radius_m, lat=start[0], lng=start[1])
        return done("no_coverage")

    # 거리 예산의 기준점. 요청 좌표가 아니라 **스냅된 pano** 다 (→ yaml 주석)
    origin = (start_pano.lat, start_pano.lng)
    res.origin = origin
    res.origin_pano = start_pano.pano_id

    # 큐는 하나. 발견 순서대로 FIFO 라 소비 순서가 곧 depth 순서다
    q: deque[_Node] = deque(
        [_Node(depth=0, bearing=geo.norm_deg(start_bearing), came_from=None, pano=start_pano)])
    # 큐에 들어갔거나 "아니오" 판정을 받은 pano. 어느 쪽이든 다시 묻지 않는다
    visited: set[str] = {start_pano.pano_id}

    def drain() -> list[dict]:
        return [leftover(n, res.stop_reason) for n in q]

    # (노드 행, 그 노드를 발견한 판정). 값은 런 끝에 채운다 — 위 ⚠️ 참조
    node_slots: list[tuple[dict, _Pending | None]] = []

    while q:
        if stop := budget_stop():
            res.stop_reason = stop
            res.frontier += drain()
            break

        node = q.popleft()

        # ⚠️ 여기서 판정을 **기다리지 않는다.** `is_trail` 은 출력 전용이라
        # 루프의 어떤 판단도 읽지 않는다 (확장 여부도 큐 순서도 안 바꾼다).
        # 기다리면 다음 캡처가 그만큼 늦어져 겹치기가 통째로 사라진다 —
        # 실제로 그렇게 짰다가 속도가 하나도 안 붙었다. 자리만 잡아 두고
        # 런 끝에 채운다.
        # skipped 와 is_trail 은 **다른 것**이다: is_trail 은 부모에서 이 노드로
        # 들어온 간선의 판정이고, skipped 는 이 노드에서 나가는 방향들을
        # 물어봤는가다. 건너뛴 부모에서 온 노드는 is_trail 이 None 이다
        row = {"pano_id": node.pano.pano_id, "lat": node.pano.lat,
               "lng": node.pano.lng, "depth": node.depth,
               "parent": node.came_from, "is_trail": None, "skipped": False}
        res.nodes.append(row)
        node_slots.append((row, node.found_by))

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
            tally("neighbors_missing", pano_id=node.pano.pano_id)
            res.frontier.append(leftover(node, "neighbors_missing"))
            continue

        # 이 노드의 방향들을 찍을 것인가. 갈래가 둘 이상이면 주기와 무관하게
        # 전부 찍는다 (→ `cadence`). 판정값은 여기에 안 들어온다
        shoot, child_pos = cadence(node.pos, len(cands) >= 2, cfg)
        if not shoot:
            # 후보가 없으면 건너뛴 것이 아니라 **갈 곳이 없는 것**이다. 둘을
            # 한 카운터에 섞으면 "판정이 적은 이유가 건너뛰기인가 막다른
            # 길인가" 를 결과만 보고 알 수 없다 — 이 카운터의 존재 이유가 그건데
            if cands:
                res.skipped += 1
                row["skipped"] = True
            # 띄워 둔 답을 놀리지 않는다. 건너뛰기가 길게 이어지면 실패를
            # 그만큼 늦게 알게 되고, 그 사이 경고가 더 쌓인다 (→ pump)
            pump(cfg.max_inflight)
            if abort:
                # 찍는 노드와 같은 처리를 여기서도 해야 한다. 안 그러면 서버가
                # 죽은 것을 알고도 건너뛰기 구간이 끝날 때까지 큐를 계속 넓힌다
                note_abort(node.depth)
                # 이 노드의 후보는 아직 큐에 안 들어갔다 — drain() 만으로는
                # 안 잡힌다. 넣지 않으면 이 갈래가 통째로 사라진다
                res.frontier += unwalked(node, cands, res.stop_reason)
                res.frontier += drain()
                break

        budget_hit = False
        for i, (hdg, nb) in enumerate(cands):
            # 시간은 노드 경계가 아니라 **후보마다** 본다. 한 노드의 후보가
            # 최대 max_candidates 개라, 노드 경계에서만 보면 캡처가 느릴 때
            # 그 노드 하나가 통째로 예산을 넘겨 실행된다. 취소도 같은 자리다.
            if stop := budget_stop():
                res.stop_reason = stop
                res.frontier += unwalked(node, cands[i:], res.stop_reason)
                budget_hit = True
                break

            pd = None
            if shoot:
                pd = submit(node.pano, hdg, node.depth, nb)
                if pd is None:
                    # 캡처 실패는 판정이 아니다. probes 에 넣으면 "아니오" 와 섞인다 —
                    # 이 레포의 사고는 전부 그런 혼동에서 났다. 로그(capture_failed)만 남긴다
                    continue

            # 판정을 기다리지 않고 그래프를 민다. 판정값은 이 둘 중 어느 것도
            # 바꾸지 않는다 — 그게 겹치기가 성립하는 유일한 이유다.
            # 첫 접근의 판정이 그 pano 의 판정이다 — 참이든 거짓이든 다시 묻지 않는다
            #
            # **건너뛴 노드도 여기까지는 똑같이 온다.** 이웃을 묻고 확장하는
            # 것은 그대로이고 캡처와 판정만 빠진다 — 그래서 그래프의 모양은
            # skip 설정과 무관하다 (→ tests/test_skip.py)
            visited.add(nb.pano_id)
            q.append(_Node(depth=node.depth + 1, bearing=hdg, pos=child_pos,
                           came_from=node.pano.pano_id, found_by=pd,
                           pano=Pano(pano_id=nb.pano_id, lat=nb.lat, lng=nb.lng)))

            if not shoot:
                continue
            # 띄워 둔 것이 상한을 넘으면 여기서 받는다. 다음 캡처는 그동안 돈다.
            pump(cfg.max_inflight)
            if abort:
                note_abort(node.depth)
                # i 번째는 위에서 큐에 들어갔다. 그 뒤는 아직 안 밟았다
                res.frontier += unwalked(node, cands[i + 1:], res.stop_reason)
                budget_hit = True
                break

        if budget_hit:
            res.frontier += drain()
            break

    # 큐를 다 비웠어도 띄워 둔 판정이 남아 있다 — 마지막 것들을 받는다
    if not abort:
        pump(0)
    # 미뤄 둔 노드 판정을 채운다. 실패로 끝난 런은 못 받은 것이 None 으로 남는다
    for row, pd in node_slots:
        if pd is not None:
            row["is_trail"] = pd.is_trail
    if abort and not res.stop_reason:
        note_abort(0)

    return done(res.stop_reason or "exhausted")

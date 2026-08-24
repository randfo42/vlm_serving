"""설정 로더 — app/config/trailwalk.yaml 하나가 기본값의 정본이다.

### 왜 이 모듈이 있나

같은 개념이 세 곳에 흩어져 있었다: dataclass 필드 기본값, argparse 의
`default=`, 모듈 상수. 그리고 값이 서로 어긋났다 — `max_candidates` 가
walk 3 / explore 4, `max_turn_deg` 가 walk 120 / explore 180(=무효),
`expand_non_trail` 은 기본을 뒤집어 놓고 되돌릴 CLI 수단이 없었다.
어느 것이 실제로 먹는 값인지 알려면 코드를 다 읽어야 했다.

이제 기본값은 YAML 한 곳이고, 코드에는 없다.

### 조용히 틀리지 않는다

설정 파일의 나쁜 실패는 둘 다 에러를 안 낸다:

- `max_candidate: 8` — 오타. 8이 먹었다고 믿지만 기본값이 돈다
- `resume: "no"` — YAML 에서 따옴표 친 no 는 **문자열**이고,
  `if not resume:` 은 비어 있지 않은 문자열을 참으로 읽는다.
  끄려던 옵션이 켜진 채로 런이 끝난다

그래서 **모르는 키도(`_build`), 안 맞는 타입도(`_coerce`) 즉시 터뜨린다.**
이 레포의 사고는 전부 "에러 없이 틀리는" 쪽에서 났다.

### 비밀값은 여기 없다

API 키는 `config.py`(.env 로더)가 따로 다룬다. 이 파일은 커밋되므로
비밀값을 적으면 그대로 저장소에 남는다.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "trailwalk.yaml"


class SettingsError(RuntimeError):
    """설정 파일이 없거나, 모르는 키가 있거나, 타입이 안 맞는다."""


@dataclass(frozen=True)
class RunSettings:
    provider: str
    start: tuple[float, float]
    bearing: float
    out: str | None
    dump: str | None
    save_images: bool
    headed: bool
    warmup: bool


@dataclass(frozen=True)
class BudgetSettings:
    max_seconds: float
    max_distance_m: float


@dataclass(frozen=True)
class CandidateSettings:
    max_candidates: int


@dataclass(frozen=True)
class SkipSettings:
    # 몇 노드를 찍고 몇 노드를 건너뛸지. **예산 축이 아니다** — 런을 멈추는
    # 것은 여전히 시간과 거리 둘뿐이고, 이건 노드 하나의 비용을 줄인다.
    run_steps: int
    skip_steps: int


@dataclass(frozen=True)
class GeoSettings:
    snap_radius_m: float


@dataclass(frozen=True)
class VlmSettings:
    url: str
    prompt_version: str
    schema: str
    max_tokens: int
    timeout_s: int
    fatal_500_streak: int
    max_inflight: int
    # camera_surface 범주 중 산책로로 셀 것. 범주형 스키마(surface·surface_eval)
    # 에서만 쓰인다 — 불리언 스키마(walk·eval)는 서버가 낸 is_trail 을 그대로 쓴다.
    trail_surfaces: list[str]
    # nature_level 이 이 값 이상이면 산책로로 센다. 자연 스키마
    # (nature·nature_eval)에서만 쓰인다. trail_surfaces 와 같은 역할이다 —
    # 경계를 프롬프트가 아니라 설정에 두어 재판정 없이 옮길 수 있게 한다.
    min_nature_level: int
    # footway 가 1 이어야 산책로로 세는가. v6(nature_footway 스키마)에서만
    # 쓰인다. false 로 두면 녹지 등급만 보는 v5 동작이 된다 — 그 한 줄로
    # "녹지만" 과 "녹지 AND 인도" 를 재판정 없이 A/B 할 수 있다.
    require_footway: bool


@dataclass(frozen=True)
class CollectSettings:
    # 장수 상한은 없다. 예산 축은 budget 의 둘(시간·거리)뿐이고, collect 는
    # explore 와 **같은 조건에서 멈춰야** 같은 것을 모은 것이 된다
    out_dir: str


@dataclass(frozen=True)
class ImageSettings:
    target_size: tuple[int, int]
    expected_image_tokens: int
    jpeg_quality: int

    @property
    def min_prompt_tokens(self) -> int:
        """prompt_tokens 하한 — 이미지가 무시됐는지 보는 유일한 신호.

        이미지가 무시되면(WEBP 사고) 텍스트 분량만 잡혀 수십 토큰이 된다.
        이미지 토큰의 3/4 만 넘겨도 "이미지가 들어갔다" 는 확실하다.

        상수로 따로 두지 않고 파생시킨다. 예전에는 200 이라는 숫자가 따로
        적혀 있었고 `EXPECTED_IMAGE_TOKENS` 는 아무도 안 읽는 죽은 상수였다 —
        토큰 수가 바뀌어도 하한이 안 따라오는 구조였다.
        """
        return self.expected_image_tokens * 3 // 4


@dataclass(frozen=True)
class KakaoSettings:
    host: str
    port: int
    hide_arrows: bool
    tile_quiet_ms: int
    tile_wait_max_ms: int
    render_settle_ms: int
    render_settle_stable: int
    render_settle_tries: int
    # pano 하나가 쓸 수 있게 되기까지 기다리는 최대 시간과 폴링 간격.
    # **두 자리를 함께 정한다** — `__show` 의 전환 데드라인과 `neighbors()` 의
    # 노드 JSON 대기다. 둘 다 "SDK 가 이 pano 를 받아왔는가" 를 기다린다.
    pano_wait_ms: int
    pano_poll_ms: int


@dataclass(frozen=True)
class SamplingSettings:
    interval_m: float
    head_m: float
    snap_radius_m: float
    max_panos_per_course: int
    provider_restart_every: int
    coverage_min_ratio: float


@dataclass(frozen=True)
class LabelsSettings:
    dataset: str


@dataclass(frozen=True)
class EvalSettings:
    labels: str
    out: str | None
    resume: bool


@dataclass(frozen=True)
class FixtureSettings:
    grid_m: float
    images_dir: str | None


@dataclass(frozen=True)
class Settings:
    run: RunSettings
    budget: BudgetSettings
    candidates: CandidateSettings
    skip: SkipSettings
    geo: GeoSettings
    vlm: VlmSettings
    collect: CollectSettings
    image: ImageSettings
    kakao: KakaoSettings
    fixture: FixtureSettings
    sampling: SamplingSettings
    labels: LabelsSettings
    eval: EvalSettings


def _coerce(v: Any, hint: Any, where: str, name: str):
    """값 하나를 선언된 타입으로. 안 맞으면 터뜨린다.

    타입을 안 보면 `resume: "no"` 가 조용히 통과한다 — YAML 에서 따옴표
    친 "no" 는 문자열이고, `if not resume:` 은 비어 있지 않은 문자열을
    참으로 읽는다. **끄려던 옵션이 켜진 채로 런이 돈다.** 에러도 안 난다.
    이 레포의 사고는 전부 이런 모양이었다.
    """
    def bad(want: str):
        return SettingsError(
            f"{where}.{name}: {want} 여야 하는데 {type(v).__name__} 이 왔다 ({v!r})\n"
            f"  YAML 에서 따옴표를 치면 문자열이 된다 — true/false, 숫자는 그냥 적을 것."
        )

    origin = get_origin(hint)
    if origin is UnionType or origin is Union:          # `str | None`
        args = [a for a in get_args(hint) if a is not type(None)]
        if v is None:
            return None
        return _coerce(v, args[0], where, name)

    if origin is tuple:                                  # `tuple[float, float]`
        want = get_args(hint)
        if not isinstance(v, list | tuple):
            raise bad(f"{len(want)}개짜리 리스트")
        if len(v) != len(want):
            raise SettingsError(
                f"{where}.{name}: 원소가 {len(want)}개여야 하는데 {len(v)}개다 ({v!r})")
        return tuple(_coerce(x, w, where, f"{name}[{i}]")
                     for i, (x, w) in enumerate(zip(v, want, strict=True)))

    if origin is list:                                   # `list[str]`
        (want,) = get_args(hint)
        if not isinstance(v, list):
            raise bad("리스트")
        return [_coerce(x, want, where, f"{name}[{i}]") for i, x in enumerate(v)]

    if hint is bool:
        # bool 을 먼저 본다. 파이썬에서 bool 은 int 의 하위형이라 순서가 뒤집히면
        # `warmup: 1` 같은 것이 int 검사에 먼저 걸린다.
        if not isinstance(v, bool):
            raise bad("true 또는 false")
        return v
    if hint is int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise bad("정수")
        return v
    if hint is float:
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise bad("숫자")
        return float(v)                                  # 900 을 900.0 으로
    if hint is str:
        if not isinstance(v, str):
            raise bad("문자열")
        return v
    return v


def _build(cls: type, raw: Any, where: str):
    """dict 하나를 dataclass 하나로. 모르는 키도, 안 맞는 타입도 터뜨린다."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError(f"{where}: 매핑이어야 하는데 {type(raw).__name__} 이 왔다")

    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise SettingsError(
            f"{where}: 모르는 키 {sorted(unknown)}\n"
            f"  쓸 수 있는 키: {sorted(known)}\n"
            f"  오타를 그냥 넘기면 기본값이 먹은 채로 런이 돈다 — 그래서 막는다."
        )

    # `from __future__ import annotations` 때문에 f.type 은 문자열이다.
    # 실제 타입 객체는 여기서 한 번에 푼다.
    missing = known - set(raw)
    if missing:
        # 정본 YAML 이 병합의 바닥이므로, 여기서 비었다는 것은 정본에 그 키가
        # 없다는 뜻이다. 코드에 기본값을 두면 조용히 메워지므로 두지 않는다.
        raise SettingsError(
            f"{where}: 값이 없다 {sorted(missing)}\n"
            f"  정본({DEFAULT_PATH})에 그 키가 빠졌다. 기본값은 코드가 아니라 거기 있다."
        )

    hints = get_type_hints(cls)
    kw = {f.name: _coerce(raw[f.name], hints[f.name], where, f.name) for f in fields(cls)}
    return cls(**kw)


def _read(p: Path) -> dict:
    """YAML 파일 하나를 dict 로. 구획 이름까지만 검사한다."""
    if not p.exists():
        raise SettingsError(
            f"설정 파일이 없다: {p}\n"
            f"  정본은 {DEFAULT_PATH} 다. 복사해서 고쳐 쓸 것."
        )
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SettingsError(f"{p}: YAML 파싱 실패\n  {e}") from e
    if not isinstance(raw, dict):
        raise SettingsError(f"{p}: 최상위가 매핑이어야 한다")

    known = {f.name for f in fields(Settings)}
    unknown = set(raw) - known
    if unknown:
        raise SettingsError(
            f"{p}: 모르는 최상위 구획 {sorted(unknown)}\n"
            f"  쓸 수 있는 구획: {sorted(known)}"
        )
    return raw


def load(path: str | Path | None = None) -> Settings:
    """정본을 읽고 그 위에 path 를 덮어쓴다.

    **병합의 바닥은 항상 정본 YAML 이다.** 코드에는 기본값이 없다 —
    두면 정본이 둘이 되어, 정본의 값을 고쳐도 그 키를 생략한 커스텀 설정은
    조용히 옛 값으로 돈다. 그래서 커스텀 파일에는 **바꿀 것만** 적으면 되고,
    적지 않은 것은 코드가 아니라 정본에서 온다.
    """
    raw = _read(DEFAULT_PATH)
    p = Path(path) if path else DEFAULT_PATH
    if p.resolve() != DEFAULT_PATH.resolve():
        # 구획 단위가 아니라 키 단위로 덮는다. 구획째 갈아치우면 `budget:` 아래
        # 한 줄만 바꾸려던 사용자가 나머지 예산을 통째로 잃는다.
        for section, body in _read(p).items():
            if body is None:
                continue
            raw[section] = {**(raw.get(section) or {}), **body}

    # `from __future__ import annotations` 때문에 f.type 은 문자열이다
    hints = get_type_hints(Settings)
    kw = {f.name: _build(hints[f.name], raw.get(f.name), f"{p}:{f.name}")
          for f in fields(Settings)}
    return Settings(**kw)


# 모듈 상수들(imaging.TARGET_SIZE 등)이 import 시점에 읽는 정본.
# 런마다 바꿔야 하는 값은 이걸 쓰지 말고 run_*.py 가 load(--config) 한 결과를
# 생성자 인자로 넘긴다 — import 순서에 의존하지 않게.
SETTINGS = load()

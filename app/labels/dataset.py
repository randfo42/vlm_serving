"""라벨 데이터셋의 경로 해석. `app/labels/<이름>/` 아래가 데이터셋 하나다.

경로를 각 스크립트에 상수로 박아두면 데이터셋이 둘이 되는 순간(종로 → 서울)
네 파일을 같이 고쳐야 하고, 하나를 빠뜨리면 **다른 데이터셋의 파일을 읽으면서
조용히 돈다**. 실제로 run_eval 이 그랬다 — labels 는 설정에서 받는데 이미지
경로만 jongno 로 박혀 있었다.

데이터셋을 나누는 이유(합치지 않는 이유)는 `docs/22-labels.md` §9 에 있다:
출처가 다르고, 샘플링 밀도가 다르고, 지리적으로 겹친다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LABELS_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DatasetPaths:
    name: str
    root: Path

    # 입력 → 산출 순서
    courses: Path          # 코스 대장 (경유지 텍스트)
    waypoints: Path        # 지오코딩 결과
    overrides: Path        # 사람이 넣는 좌표 보정
    routes_dir: Path       # walkset 응답 캐시 (구간별)
    geom: Path             # 구간 폴리라인
    coverage: Path         # 로드뷰 커버리지 프로브
    samples: Path          # 캡처 산출 대장
    pano_meta: Path        # pano id → shot_tool 등 (캡처 후 조회)
    images: Path           # 이미지 (gitignore)
    labels: Path           # 검수 확정본
    report: Path           # 코스 단위 점검표 (TSV)
    svg: Path              # 코스 시각화 (gitignore)


def at(name: str, root: Path) -> DatasetPaths:
    """임의의 루트에 데이터셋 경로를 만든다.

    파일명 규칙이 **여기 한 곳에만** 있게 하려고 `resolve()` 와 테스트가 같이
    쓴다. 테스트가 필드를 손으로 나열하면 필드가 늘 때마다 같이 고쳐야 하고,
    빠뜨리면 테스트가 실제 경로 규칙과 다른 것을 검증하게 된다.
    """
    return DatasetPaths(
        name=name, root=root,
        courses=root / "courses.json",
        waypoints=root / "waypoints.json",
        overrides=root / "overrides.json",
        routes_dir=root / "routes",
        geom=root / "courses_geom.json",
        coverage=root / "coverage.json",
        samples=root / "samples.jsonl",
        pano_meta=root / "pano_meta.json",
        images=root / "images",
        labels=root / "labels.jsonl",
        report=root / "courses_report.tsv",
        svg=root / "svg",
    )


def resolve(name: str) -> DatasetPaths:
    """데이터셋 이름 → 경로 묶음. 이름에 경로 구분자는 허용하지 않는다."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"데이터셋 이름이 이상하다: {name!r} — "
                         f"`app/labels/<이름>/` 의 <이름> 만 준다")
    return at(name, LABELS_ROOT / name)


def add_argument(ap) -> None:
    """스크립트 공통 `--dataset`.

    ⚠️ **기본값을 argparse 에 채우지 않는다.** 채우면 `--config` 오버레이를
    읽기 *전* 의 정본 설정으로 고정되어, 오버레이가 `labels.dataset` 을 바꿔도
    조용히 무시된다 (sampling 값은 오버레이를 쓰면서 경로만 정본을 쓰는
    상태가 된다 — 다른 데이터셋에 쓰거나 읽는다). 호출자가
    `a.dataset or st.labels.dataset` 으로 해석하고, 그 st 는 --config 를
    반영한 것이어야 한다.
    """
    ap.add_argument("--dataset", default=None,
                    help="app/labels/<이름>/ 의 이름 (기본: 설정의 labels.dataset)")

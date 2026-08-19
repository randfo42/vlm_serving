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
    images: Path           # 이미지 (gitignore)
    labels: Path           # 검수 확정본
    report: Path           # 코스 단위 점검표 (TSV)
    svg: Path              # 코스 시각화 (gitignore)


def resolve(name: str) -> DatasetPaths:
    """데이터셋 이름 → 경로 묶음. 이름에 경로 구분자는 허용하지 않는다."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"데이터셋 이름이 이상하다: {name!r} — "
                         f"`app/labels/<이름>/` 의 <이름> 만 준다")
    root = LABELS_ROOT / name
    return DatasetPaths(
        name=name, root=root,
        courses=root / "courses.json",
        waypoints=root / "waypoints.json",
        overrides=root / "overrides.json",
        routes_dir=root / "routes",
        geom=root / "courses_geom.json",
        coverage=root / "coverage.json",
        samples=root / "samples.jsonl",
        images=root / "images",
        labels=root / "labels.jsonl",
        report=root / "courses_report.tsv",
        svg=root / "svg",
    )


def add_argument(ap, settings=None) -> None:
    """스크립트 공통 `--dataset`. 기본값은 코드가 아니라 설정에서 온다."""
    default = settings.labels.dataset if settings is not None else None
    ap.add_argument("--dataset", default=default,
                    help=f"app/labels/<이름>/ 의 이름 (기본: {default})")

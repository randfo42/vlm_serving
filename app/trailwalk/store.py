"""저장소 — SQLite 한 파일이 판정의 유일한 정본이다.

런로그 JSONL(runlog.py)을 대체한다. 파일을 나누던 시절의 문제는 "어느 지점을
어떤 프롬프트로 어떻게 판정했나"가 런 파일 24개에 흩어져, 지도 한 장을 그리려면
파일을 손으로 골라 조인해야 했다는 것이다. 웹이 뷰포트 단위로 조회하려면
정본이 한곳이어야 한다.

원칙 셋:

  1. **판정은 불변이다.** verdict 에는 UPDATE 가 없다 — 같은 지점을 새 프롬프트로
     다시 판정하면 새 행이다. v5↔v6 비교(둘 다 남아 있어서 가능했다)가 근거다.
     UPDATE 되는 것은 사람이 붙인 label 뿐이고, 그래서 label 에만 updated_at 이 있다.
  2. **지도 위 한 점 = pano 한 행.** 좌표는 선착순 고정 — 같은 pano 가 두 좌표를
     갖는 상태를 스키마가 표현하지 못하게 한다.
  3. **판정 1건 = 커밋 1번.** 런로그가 줄마다 flush 하던 계약(runlog.py)과 같다 —
     6시간 런이 중간에 죽어도 거기까지는 남는다. 판정 간격이 1초를 넘어
     커밋 비용은 측정 밖이다.

⚠️ 이 파일(DB)은 커밋하지 않는다 — 수집한 pano 좌표와 vlm.url(LAN IP)이
들어가고 이 저장소는 public 이다. `.gitignore` 의 `app/runs/*.db*` 가 막는다.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import warn as warn_mod

SCHEMA_VERSION = 1

# 스키마 정본은 이 문자열 하나다. 마이그레이션 파일을 따로 두지 않는다 —
# 정본이 둘이 되는 순간 어느 쪽이 실제 DB 와 같은지 코드를 읽어야 알게 된다
# (prompts/system_v*.txt 를 바이트 고정하는 것과 같은 이유).
SCHEMA_SQL = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- 지도 위의 한 점. 좌표는 선착순 고정(upsert_pano 의 DO NOTHING) — SDK 가 주는
-- pano 좌표는 안 바뀌고, 바뀐다면 그건 기록할 사건이지 덮어쓸 값이 아니다.
CREATE TABLE pano (
  pano_id     TEXT PRIMARY KEY,
  lat         REAL NOT NULL,
  lng         REAL NOT NULL,
  captured_at TEXT,
  first_seen  TEXT NOT NULL
);
CREATE INDEX pano_bbox ON pano(lat, lng);

CREATE TABLE run (
  run_id        INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,  -- 옛 런로그 파일 stem 또는 새 런 타임스탬프
  kind          TEXT NOT NULL,         -- explore | walk | eval
  provider      TEXT NOT NULL,
  source        TEXT NOT NULL,         -- live | backfill
  source_sha256 TEXT,                  -- 백필 원본 JSONL 의 해시. 재실행 멱등성의 근거
  started_at    TEXT NOT NULL,
  finished_at   TEXT,                  -- NULL = 진행 중이거나 중간에 죽었다
  stop_reason   TEXT,
  wall_s        REAL,
  start_lat     REAL, start_lng REAL, start_bearing REAL,
  origin_pano   TEXT REFERENCES pano(pano_id),   -- 스냅된 시작점 (docs/23 §9)
  prompt_version TEXT, prompt_sha256 TEXT, schema_name TEXT,
  -- is_trail 을 유도한 경계. 원본이 없으면 임계를 옮겼을 때 옛 런을 다시 해석할
  -- 수 없다 (verdict 의 nature_level 을 남기는 것과 같은 근거). 백필분은 NULL —
  -- 옛 헤더에 없다.
  min_nature_level INTEGER, require_footway INTEGER, trail_surfaces_json TEXT,
  header_json   TEXT NOT NULL,         -- run_start 원문. 세대마다 키가 달라 열로 안 푼다
  summary_json  TEXT,                  -- run_end 의 나머지 (calls/retries/…)
  vlm_url       TEXT                   -- ⚠ LAN IP 가 들어온다. 내보내기에서 제외할 것
);
CREATE INDEX run_prompt ON run(prompt_version);

-- 판정. 불변 — 재판정은 새 행. 런 안 판정 순서는 verdict_id 오름차순이 정본이다
-- (런로그 파일명의 "이름순 = 호출순" 을 승계).
CREATE TABLE verdict (
  verdict_id  INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  pano_id     TEXT NOT NULL REFERENCES pano(pano_id),
  heading     REAL NOT NULL,
  step        INTEGER,
  is_trail    INTEGER NOT NULL,        -- **파생값** (run 의 경계 컬럼들로 유도)
  confidence  INTEGER,
  camera_surface TEXT,                 -- v4 에서만
  nature_level   INTEGER,              -- v5+ 에서만
  footway        INTEGER,              -- v6 에서만
  prompt_tokens INTEGER, cached_tokens INTEGER, completion_tokens INTEGER,
  latency_ms  REAL,
  src_format  TEXT,
  image_path  TEXT,                    -- app/runs/images/ 기준 상대경로. NULL = 안 저장
  sample_id   TEXT, label INTEGER,     -- eval 런에서만
  -- probe 시각. 옛 런로그의 probe 줄에는 시각이 없어 백필분은 run.started_at 로
  -- 채운다 — 근사라는 사실은 run.source='backfill' 이 이미 말해 준다.
  created_at  TEXT NOT NULL
);
CREATE INDEX verdict_agg ON verdict(pano_id, run_id, nature_level, is_trail);
CREATE INDEX verdict_run ON verdict(run_id, verdict_id);
CREATE INDEX verdict_sample ON verdict(run_id, sample_id) WHERE sample_id IS NOT NULL;

-- 탐색 그래프. "안 본 것" 과 "없는 것" 을 구분하는 근거를 버리지 않는다.
CREATE TABLE node (
  run_id      INTEGER NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  pano_id     TEXT NOT NULL REFERENCES pano(pano_id),
  depth       INTEGER NOT NULL,
  parent_pano TEXT,
  is_trail    INTEGER,                 -- NULL = 안 물어봤다 (건너뜀)
  skipped     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, pano_id)
) WITHOUT ROWID;

CREATE TABLE frontier (
  frontier_id INTEGER PRIMARY KEY,
  run_id    INTEGER NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  from_pano TEXT,
  pano_id   TEXT NOT NULL,   -- pano FK 를 안 건다 — 안 가 본 지점이라 pano 행이 없다
  lat       REAL, lng REAL,  -- 그래서 좌표를 여기 직접 든다 (이어탐색·지도 표시용)
  depth     INTEGER NOT NULL,
  reason    TEXT NOT NULL
);
CREATE INDEX frontier_run ON frontier(run_id);

-- 경고 — 런 도중 사람이 알아야 할 일 (trailwalk/warn.py 의 두 형태를 보존).
--   once  = 1회성. 발생 즉시 한 행.
--   tally = 집계형. (run_id, code) 당 한 행을 UPSERT — 런로그는 run_end 에서만
--           완성돼 중간에 죽으면 통째로 사라졌는데, 여기서는 런 도중에 내구화된다.
CREATE TABLE warning (
  warning_id  INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,           -- once | tally
  code        TEXT NOT NULL,
  count       INTEGER,                 -- 집계형만
  message     TEXT NOT NULL,
  detail_json TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX warning_run ON warning(run_id);
CREATE UNIQUE INDEX warning_tally ON warning(run_id, code) WHERE kind = 'tally';

-- 디버깅용 자유형 로그. 웹이 읽는 계약이 아니다 — 그건 warning 이다 (runlog 와 같다).
CREATE TABLE event (
  event_id     INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX event_run ON event(run_id, event_id);

-- 사람이 붙인 라벨. **이 스키마에서 UPDATE 되는 유일한 테이블** — 그래서 여기에만
-- updated_at 이 있다. DB 는 gitignore 되므로 백업은 export_labels 로 뺀다.
CREATE TABLE label (
  label_id   INTEGER PRIMARY KEY,
  pano_id    TEXT NOT NULL REFERENCES pano(pano_id),
  heading    REAL,                     -- NULL = 이 pano 전체에 대한 라벨 (기본)
  is_trail   INTEGER NOT NULL,
  note       TEXT,
  author     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX label_one ON label(pano_id, COALESCE(heading, -1.0));

-- 탐색 잡 큐. 웹이 넣고 워커 데몬이 집는다. 상태 전이와 claim 규칙은 워커 쪽에.
CREATE TABLE job (
  job_id    INTEGER PRIMARY KEY,
  state     TEXT NOT NULL,             -- queued|claimed|running|done|failed|canceled
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  start_lat REAL NOT NULL, start_lng REAL NOT NULL, bearing REAL NOT NULL,
  radius_m  REAL NOT NULL, max_seconds REAL NOT NULL,
  config_path TEXT,
  run_id    INTEGER REFERENCES run(run_id),
  worker_id TEXT,
  heartbeat_at TEXT,
  progress_json TEXT,
  created_at TEXT NOT NULL, claimed_at TEXT, finished_at TEXT,
  stop_reason TEXT, error TEXT
);
CREATE INDEX job_queue ON job(state, job_id);
"""


class StoreError(RuntimeError):
    """DB 를 열 수 없거나 스키마가 안 맞는다. 조용히 빈 DB 를 만들면 안 되는 상황."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str, *, read_only: bool = False,
            cross_thread: bool = False) -> sqlite3.Connection:
    """PRAGMA 까지 적용된 커넥션. 닫는 것은 호출자 몫이다.

    웹은 요청마다 열고 닫는다 — 리더가 커넥션을 오래 잡으면 스냅샷 때문에
    WAL 체크포인트가 못 돌아, 6시간 런 동안 WAL 파일이 계속 자란다.

    cross_thread: FastAPI 는 sync 의존성의 생성과 정리(close)를 서로 다른
    threadpool 스레드에서 돌릴 수 있다 — sqlite 기본값이면 close 가
    ProgrammingError 로 터진다 (요청이 동시에 들어올 때만 재현되므로 curl
    단건으로는 안 잡힌다. 실측 2026-08-25). 사용은 요청 안에서 순차라
    check_same_thread=False 가 안전하다. 웹(get_conn)만 켠다.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True,
                               check_same_thread=not cross_thread)
    else:
        conn = sqlite3.connect(str(path), check_same_thread=not cross_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not read_only:
        # WAL: 워커가 쓰는 동안 웹이 읽는다. NORMAL: 프로세스 크래시엔 안전하고,
        # 전원 차단에서만 마지막 커밋을 잃는데 그건 판정 1건(~2초)이다.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """빈 DB 면 스키마를 만들고, 버전이 다르면 거부한다.

    자동 업그레이드를 하지 않는 이유: 스키마가 바뀔 정도의 변경이면 무엇이
    어떻게 옮겨지는지 사람이 봐야 한다. 지금은 백필(backfill_runs.py)로 언제든
    처음부터 다시 만들 수 있는 DB 라, "지우고 다시" 가 정직한 업그레이드다.
    """
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
    if has is None:
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO schema_meta VALUES ('version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()
        return
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    found = row[0] if row else "(없음)"
    if found != str(SCHEMA_VERSION):
        raise StoreError(
            f"DB 스키마 버전이 다르다: 파일 {found}, 코드 {SCHEMA_VERSION}. "
            f"이 DB 는 백필로 재생성 가능하다 — 파일을 지우고 "
            f"backfill_runs.py 를 다시 돌릴 것")


# ── 낮은 수준 insert. RunWriter 와 백필이 같은 것을 쓴다 ─────────────────────

def upsert_pano(conn: sqlite3.Connection, pano_id: str, lat: float, lng: float,
                captured_at: str | None = None, now: str | None = None) -> None:
    """좌표 선착순 고정. 이미 있으면 아무것도 안 바꾼다 (모듈 독스트링 원칙 2)."""
    conn.execute(
        "INSERT INTO pano (pano_id, lat, lng, captured_at, first_seen) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(pano_id) DO NOTHING",
        (pano_id, round(lat, 7), round(lng, 7), captured_at, now or _now()))


def insert_run(conn: sqlite3.Connection, *, name: str, kind: str, provider: str,
               source: str, started_at: str, header_json: str,
               source_sha256: str | None = None,
               start_lat: float | None = None, start_lng: float | None = None,
               start_bearing: float | None = None,
               prompt_version: str | None = None, prompt_sha256: str | None = None,
               schema_name: str | None = None, vlm_url: str | None = None,
               min_nature_level: int | None = None, require_footway: int | None = None,
               trail_surfaces_json: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO run (name, kind, provider, source, source_sha256, started_at, "
        " start_lat, start_lng, start_bearing, prompt_version, prompt_sha256, "
        " schema_name, vlm_url, min_nature_level, require_footway, "
        " trail_surfaces_json, header_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, kind, provider, source, source_sha256, started_at,
         start_lat, start_lng, start_bearing, prompt_version, prompt_sha256,
         schema_name, vlm_url, min_nature_level, require_footway,
         trail_surfaces_json, header_json))
    return cur.lastrowid


def insert_verdict(conn: sqlite3.Connection, *, run_id: int, pano_id: str,
                   heading: float, is_trail: bool, created_at: str,
                   step: int | None = None, confidence: int | None = None,
                   camera_surface: str | None = None, nature_level: int | None = None,
                   footway: int | None = None, prompt_tokens: int | None = None,
                   cached_tokens: int | None = None, completion_tokens: int | None = None,
                   latency_ms: float | None = None, src_format: str | None = None,
                   image_path: str | None = None, sample_id: str | None = None,
                   label: bool | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO verdict (run_id, pano_id, heading, step, is_trail, confidence, "
        " camera_surface, nature_level, footway, prompt_tokens, cached_tokens, "
        " completion_tokens, latency_ms, src_format, image_path, sample_id, label, "
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, pano_id, round(heading, 1), step, int(is_trail), confidence,
         camera_surface, nature_level, footway, prompt_tokens, cached_tokens,
         completion_tokens, latency_ms, src_format, image_path, sample_id,
         None if label is None else int(label), created_at))
    return cur.lastrowid


def insert_warning(conn: sqlite3.Connection, *, run_id: int, kind: str, code: str,
                   message: str, created_at: str, count: int | None = None,
                   detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO warning (run_id, kind, code, count, message, detail_json, "
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        # 집계형은 (run_id, code) 당 한 행 — 부분 유니크 인덱스 warning_tally 가 잡는다
        "ON CONFLICT(run_id, code) WHERE kind = 'tally' DO UPDATE SET "
        " count = excluded.count, message = excluded.message, "
        " detail_json = excluded.detail_json",
        (run_id, kind, code, count, message,
         json.dumps(detail, ensure_ascii=False) if detail else None, created_at))


def insert_event(conn: sqlite3.Connection, *, run_id: int, kind: str,
                 payload: dict, created_at: str) -> None:
    conn.execute(
        "INSERT INTO event (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (run_id, kind, json.dumps(payload, ensure_ascii=False), created_at))


def write_result(conn: sqlite3.Connection, run_id: int, res) -> None:
    """ExploreResult 의 그래프(nodes/frontier)와 원점을 싣는다.

    판정(probe)은 RunWriter 가 실시간으로 썼다 — 그래프는 런이 끝나야
    완성되는 것이라 여기서 한 번에 넣는다. 경계층(runner)이 부른다.
    """
    now = _now()
    for n in res.nodes:
        upsert_pano(conn, n["pano_id"], n["lat"], n["lng"], now=now)
        conn.execute(
            "INSERT INTO node (run_id, pano_id, depth, parent_pano, is_trail, skipped) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, n["pano_id"], n["depth"], n["parent"],
             None if n["is_trail"] is None else int(n["is_trail"]),
             int(n.get("skipped", False))))
    for f in res.frontier:
        conn.execute(
            "INSERT INTO frontier (run_id, from_pano, pano_id, lat, lng, depth, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, f.get("from_pano"), f["pano_id"], f.get("lat"), f.get("lng"),
             f["depth"], f["reason"]))
    if res.origin_pano and res.origin:
        # 즉시 취소/예산 소진이면 nodes 가 비어 원점 pano 행이 아직 없다 —
        # run.origin_pano 가 pano 를 참조하므로 먼저 만들어 준다
        upsert_pano(conn, res.origin_pano, res.origin[0], res.origin[1], now=now)
        conn.execute("UPDATE run SET origin_pano = ? WHERE run_id = ?",
                     (res.origin_pano, run_id))
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: int, *, wall_s: float,
               finished_at: str | None = None, stop_reason: str | None = None,
               origin_pano: str | None = None, summary: dict | None = None) -> None:
    conn.execute(
        "UPDATE run SET finished_at = ?, stop_reason = ?, wall_s = ?, "
        " origin_pano = COALESCE(?, origin_pano), summary_json = ? WHERE run_id = ?",
        (finished_at or _now(), stop_reason, round(wall_s, 1), origin_pano,
         json.dumps(summary, ensure_ascii=False) if summary else None, run_id))


# ── RunWriter — RunLog 의 자리를 그대로 받는다 ───────────────────────────────

class RunWriter:
    """explore() 가 부르는 4개 메서드(probe/event/warn/tally)와 finish 를
    RunLog 와 같은 시그니처로 낸다 — explore.py 는 한 글자도 안 고친다.

    RunLog 에서 그대로 승계한 계약:
      - probe 1건 = 커밋 1번 (줄마다 flush 하던 것과 같다)
      - tally 의 code 검증은 **호출 시점** — finish 로 미루면 finally 안에서
        터져 런 요약이 통째로 날아간다
      - 집계형 문구 생성 실패는 finish 를 막지 않는다 (자리표시자로 격하)
      - 이미지 파일명은 번호 앞 — 이름순이 곧 호출 순서다

    conn 을 닫는 것은 호출자 몫이다 (경계층이 커넥션 수명을 관리한다).
    """

    def __init__(self, conn: sqlite3.Connection, header: dict, *, name: str,
                 image_dir: Path | None = None, source: str = "live",
                 job_id: int | None = None):
        self.conn = conn
        self.image_dir = Path(image_dir) if image_dir else None
        if self.image_dir:
            self.image_dir.mkdir(parents=True, exist_ok=True)
        self._n = 0
        self._t0 = time.time()
        self._tallies: dict[str, dict] = {}
        # 잡에서 돌면 probe 마다 진행률을 **같은 트랜잭션**에서 갱신한다 —
        # 진행률 배선이 explore() 를 안 지나가고, "판정은 남았는데 진행이
        # 안 맞는" 상태도 없다
        self.job_id = job_id
        self._finished = False

        prompt = header.get("prompt") or {}
        trail_surfaces = header.get("trail_surfaces")
        require_footway = header.get("require_footway")
        self.run_id = insert_run(
            conn,
            name=name,
            # 옛 walk 런로그에는 mode 가 없다 — 백필이 같은 규칙을 쓴다
            kind=header.get("mode") or ("eval" if "labels_path" in header else "walk"),
            provider=header.get("provider", "?"),
            source=source,
            started_at=header.get("ts") or _now(),
            start_lat=(header.get("start") or [None, None])[0],
            start_lng=(header.get("start") or [None, None])[1],
            start_bearing=header.get("start_bearing"),
            prompt_version=prompt.get("system_version"),
            prompt_sha256=prompt.get("system_sha256"),
            schema_name=header.get("schema"),
            vlm_url=header.get("url"),
            min_nature_level=header.get("min_nature_level"),
            require_footway=None if require_footway is None else int(require_footway),
            trail_surfaces_json=(json.dumps(trail_surfaces, ensure_ascii=False)
                                 if trail_surfaces is not None else None),
            header_json=json.dumps(header, ensure_ascii=False),
        )
        conn.commit()

    def probe(self, *, step: int, pano_id: str, lat: float, lng: float,
              heading: float, verdict, src_format: str,
              image: bytes | None = None,
              label: bool | None = None, sample_id: str | None = None) -> None:
        self._n += 1
        image_path = None
        if self.image_dir and image:
            # 확장자는 주장이 아니라 감지된 실제 포맷을 따른다 (runlog 와 같다)
            ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(src_format, "bin")
            fname = (f"{self._n:03d}_s{step:02d}_{pano_id}_{heading:05.1f}_"
                     f"{'T' if verdict.is_trail else 'F'}.{ext}")
            (self.image_dir / fname).write_bytes(image)
            # app/runs/images/ 기준 상대경로 — 웹이 이 밑에서만 서빙한다
            image_path = f"{self.image_dir.name}/{fname}"
        upsert_pano(self.conn, pano_id, lat, lng)
        insert_verdict(
            self.conn, run_id=self.run_id, pano_id=pano_id, heading=heading,
            step=step, is_trail=bool(verdict.is_trail), confidence=verdict.confidence,
            camera_surface=verdict.camera_surface, nature_level=verdict.nature_level,
            footway=verdict.footway, prompt_tokens=verdict.prompt_tokens,
            cached_tokens=verdict.cached_tokens,
            completion_tokens=verdict.completion_tokens,
            latency_ms=round(verdict.latency_ms, 1), src_format=src_format,
            image_path=image_path, sample_id=sample_id, label=label,
            created_at=_now())
        if self.job_id is not None:
            set_job_progress(self.conn, self.job_id,
                             {"verdicts": self._n,
                              "elapsed_s": round(time.time() - self._t0, 1)})
        self.conn.commit()   # 판정 1건 = 커밋 1번. 중간에 죽어도 거기까지는 남는다

    def event(self, kind: str, **kw) -> None:
        insert_event(self.conn, run_id=self.run_id, kind=kind, payload=kw,
                     created_at=_now())
        self.conn.commit()

    def warn(self, code: str, **detail) -> None:
        """1회성 경고. 즉시 한 행 — 런이 중간에 죽어도 남는다."""
        w = warn_mod.make(code, **detail)
        insert_warning(self.conn, run_id=self.run_id, kind="once", code=code,
                       message=w["message"], count=w.get("count"),
                       detail=w.get("detail"), created_at=_now())
        self.conn.commit()

    def tally(self, code: str, **detail) -> None:
        """집계형 경고. 같은 code 를 여러 번 불러 count 를 올린다.

        count 규칙은 RunLog.tally 와 같다: `count=` 를 주면 그만큼 더하고,
        안 주면 1이다. 호출할 때마다 UPSERT 하므로 런 도중에도 내구적이다 —
        런로그는 run_end 에서만 완성돼 중간에 죽으면 통째로 사라졌다.
        """
        if code not in warn_mod.TEXT:
            raise warn_mod.UnknownWarning(
                f"모르는 경고 code: {code!r}. trailwalk/warn.py 의 TEXT 에 추가할 것")
        t = self._tallies.setdefault(code, {"count": 0})
        t["count"] += int(detail.get("count", 1))
        t.update({k: v for k, v in detail.items() if k != "count"})
        try:
            w = warn_mod.make(code, **t)
        except warn_mod.UnknownWarning as e:
            # 문구에 필요한 필드가 아직 없다. 여기서 터뜨리면 explore 루프가 죽는다 —
            # 시끄러운 자리표시자로 격하한다 (RunLog._tallied 와 같은 처리)
            w = {"code": code, "count": t["count"],
                 "message": f"({code}) 경고 문구를 만들지 못했다: {e}"}
        insert_warning(self.conn, run_id=self.run_id, kind="tally", code=code,
                       message=w["message"], count=w.get("count", t["count"]),
                       detail=w.get("detail"), created_at=_now())
        self.conn.commit()

    def finish(self, **summary) -> None:
        """run 행을 닫는다. stop_reason 은 summary 에서 꺼내고 나머지는 통째로 남긴다."""
        stop_reason = summary.pop("stop_reason", None)
        finish_run(self.conn, self.run_id, wall_s=time.time() - self._t0,
                   stop_reason=stop_reason, summary=summary or None)
        self.conn.commit()
        self._finished = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # finish 없이 나가면(예외) run 은 finished_at IS NULL 로 남는다 — 그게
        # "중간에 죽은 런" 의 정직한 표현이다. 여기서 몰래 닫지 않는다.
        return False


# ── 질의 — 웹과 CLI 가 같은 것을 쓴다. 전부 dict 를 돌려준다 ─────────────────

def run_ids_for(conn: sqlite3.Connection, *, prompt_version: str | None = None,
                run_id: int | None = None) -> list[int]:
    """뷰포트 조회가 집계할 run 집합.

    기본 필터가 "프롬프트 버전 하나" 인 이유: MAX 집계는 한 pano 안의
    **방위들** 사이 규칙이다. 백필로 한 점에 v1~v6 이 섞였는데 버전을
    가로질러 MAX 를 걸면 폐기된 버전의 오탐 하나가 그 점을 영원히
    초록으로 만든다 — v3 의 골목 오탐이 정확히 그 모양이다.
    """
    if run_id is not None:
        # 존재 확인 없이 [run_id] 를 돌려주면 호출자의 "빈 집합 = 파라미터
        # 오류" 가드가 무효가 된다 — 없는 런이 조용히 빈 지도가 되는 그 실패
        row = conn.execute("SELECT 1 FROM run WHERE run_id = ?",
                           (run_id,)).fetchone()
        return [run_id] if row else []
    if prompt_version:
        return [r[0] for r in conn.execute(
            "SELECT run_id FROM run WHERE prompt_version = ?", (prompt_version,))]
    return [r[0] for r in conn.execute("SELECT run_id FROM run")]


def viewport(conn: sqlite3.Connection, *, s: float, w: float, n: float, e: float,
             run_ids: list[int], limit: int,
             with_headings: bool = False) -> tuple[list[dict], bool]:
    """bbox 안의 pano 를 판정 MAX 집계와 함께. (rows, truncated).

    GROUP BY 를 안 쓰는 이유: bbox 인덱스는 lat 순으로 행을 주는데 그룹
    키가 pano_id 라 SQLite 가 임시 B-tree 정렬을 붙인다. 상관 서브쿼리는
    그 정렬이 없고, verdict_agg 커버링 인덱스만 탄다 (테스트가 실행계획을
    고정한다). LIMIT+1 로 잘림을 **감지**해 truncated 로 알린다 — 조용히
    일부만 그리면 "안 본 것" 과 "없는 것" 이 섞인다.
    """
    ids = json.dumps(run_ids)
    q = """
    SELECT p.pano_id, p.lat, p.lng,
      (SELECT MAX(v.nature_level) FROM verdict v WHERE v.pano_id = p.pano_id
         AND v.run_id IN (SELECT value FROM json_each(:ids))) AS nature_level,
      (SELECT MAX(v.is_trail) FROM verdict v WHERE v.pano_id = p.pano_id
         AND v.run_id IN (SELECT value FROM json_each(:ids))) AS is_trail,
      (SELECT COUNT(*) FROM verdict v WHERE v.pano_id = p.pano_id
         AND v.run_id IN (SELECT value FROM json_each(:ids))) AS n,
      (SELECT l.is_trail FROM label l WHERE l.pano_id = p.pano_id
         AND l.heading IS NULL) AS label
    FROM pano p
    WHERE p.lat BETWEEN :s AND :n AND p.lng BETWEEN :w AND :e
      -- 선택된 런에 판정이 있는 pano 만. 이 필터는 **LIMIT 앞**(SQL 안)이어야
      -- 한다 — 뒤에서 파이썬으로 거르면 LIMIT 예산이 다른 버전의 행에
      -- 소진돼, 진짜 매칭 pano 가 빠졌는데 truncated 는 False 가 된다
      AND EXISTS (SELECT 1 FROM verdict v WHERE v.pano_id = p.pano_id
                    AND v.run_id IN (SELECT value FROM json_each(:ids)))
    LIMIT :lim
    """
    rows = [dict(r) for r in conn.execute(
        q, {"ids": ids, "s": s, "n": n, "w": w, "e": e, "lim": limit + 1})]
    truncated = len(rows) > limit
    rows = rows[:limit]
    if with_headings and rows:
        marks = ",".join("?" for _ in rows)
        hs: dict[str, list] = {}
        for v in conn.execute(
                f"SELECT pano_id, heading, is_trail, nature_level FROM verdict "
                f"WHERE pano_id IN ({marks}) AND run_id IN "
                f"(SELECT value FROM json_each(?)) ORDER BY heading",
                [r["pano_id"] for r in rows] + [ids]):
            hs.setdefault(v["pano_id"], []).append(
                {"heading": v["heading"], "is_trail": v["is_trail"],
                 "nature_level": v["nature_level"]})
        for r in rows:
            r["headings"] = hs.get(r["pano_id"], [])
    return rows, truncated


def pano_detail(conn: sqlite3.Connection, pano_id: str) -> dict | None:
    """그 pano 의 판정 이력 전부 — 버전 필터 없이. 호버/클릭 패널이 쓴다.
    같은 지점을 v4/v5/v6 가 다르게 봤다면 그 자리에서 보여야 한다."""
    p = conn.execute("SELECT * FROM pano WHERE pano_id = ?", (pano_id,)).fetchone()
    if p is None:
        return None
    verdicts = [dict(v) for v in conn.execute(
        "SELECT v.verdict_id, v.run_id, r.name AS run_name, r.prompt_version,"
        " v.heading, v.step, v.is_trail, v.confidence, v.camera_surface,"
        " v.nature_level, v.footway, v.latency_ms, v.created_at,"
        " v.image_path IS NOT NULL AS has_image"
        " FROM verdict v JOIN run r USING(run_id)"
        " WHERE v.pano_id = ? ORDER BY v.verdict_id", (pano_id,))]
    labels = [dict(r) for r in conn.execute(
        "SELECT * FROM label WHERE pano_id = ?", (pano_id,))]
    return {"pano": dict(p), "verdicts": verdicts, "labels": labels}


def runs_list(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT run_id, name, kind, provider, source, started_at, finished_at,"
        " stop_reason, wall_s, prompt_version, schema_name,"
        " (SELECT COUNT(*) FROM verdict v WHERE v.run_id = run.run_id) AS verdicts,"
        " (SELECT COUNT(*) FROM warning w WHERE w.run_id = run.run_id) AS warnings"
        " FROM run ORDER BY started_at DESC LIMIT ?", (limit,))]


def run_detail(conn: sqlite3.Connection, run_id: int) -> dict | None:
    r = conn.execute(
        "SELECT run_id, name, kind, provider, source, started_at, finished_at,"
        " stop_reason, wall_s, start_lat, start_lng, origin_pano, prompt_version,"
        " prompt_sha256, schema_name, min_nature_level, require_footway,"
        " trail_surfaces_json, summary_json"
        " FROM run WHERE run_id = ?", (run_id,)).fetchone()
    # vlm_url(LAN IP)과 header_json(그 안에 url)은 일부러 안 낸다 — 이 응답은
    # 브라우저로 나가고, 페이지를 캡처해 공유하는 순간 주소가 새는 자리다
    if r is None:
        return None
    d = dict(r)
    d["warnings"] = [dict(w) for w in conn.execute(
        "SELECT kind, code, count, message FROM warning WHERE run_id = ?"
        " ORDER BY warning_id", (run_id,))]
    return d


def image_path_of(conn: sqlite3.Connection, verdict_id: int) -> str | None:
    r = conn.execute("SELECT image_path FROM verdict WHERE verdict_id = ?",
                     (verdict_id,)).fetchone()
    return r["image_path"] if r else None


def prompt_versions(conn: sqlite3.Connection) -> list[dict]:
    """버전 드롭다운의 재료 — 버전마다 판정이 몇 건인지."""
    return [dict(r) for r in conn.execute(
        "SELECT r.prompt_version, COUNT(*) AS verdicts,"
        " COUNT(DISTINCT r.run_id) AS runs"
        " FROM verdict v JOIN run r USING(run_id)"
        " GROUP BY r.prompt_version ORDER BY r.prompt_version")]


def counts(conn: sqlite3.Connection) -> dict:
    """/api/health 용. pano 수는 R*Tree 로 갈아탈 때를 아는 계기판이기도 하다."""
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("pano", "verdict", "run", "label", "job")}


# ── 라벨 — 이 스키마에서 UPDATE 되는 유일한 것 ──────────────────────────────

def put_label(conn: sqlite3.Connection, *, pano_id: str, is_trail: bool,
              note: str | None = None, heading: float | None = None,
              author: str = "local", updated_at: str | None = None) -> dict:
    """만들거나 고친다. created_at 은 처음 값을 지키고 updated_at 만 움직인다 —
    "언제 이 판단을 마지막으로 손봤나" 가 라벨과 판정을 가르는 필드다.

    pano 가 DB 에 없으면 sqlite3.IntegrityError (FK) — 지도에 없는 점에
    라벨을 붙이는 것은 오타이지 데이터가 아니다. 호출자(API)가 404 로 바꾼다.
    """
    now = updated_at or _now()
    hkey = -1.0 if heading is None else heading
    row = conn.execute(
        "SELECT label_id FROM label WHERE pano_id = ? "
        "AND COALESCE(heading, -1.0) = ?", (pano_id, hkey)).fetchone()
    if row:
        conn.execute(
            "UPDATE label SET is_trail = ?, note = ?, author = ?, updated_at = ? "
            "WHERE label_id = ?",
            (int(is_trail), note, author, now, row["label_id"]))
        label_id = row["label_id"]
    else:
        label_id = conn.execute(
            "INSERT INTO label (pano_id, heading, is_trail, note, author,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pano_id, heading, int(is_trail), note, author, now, now)).lastrowid
    conn.commit()
    return dict(conn.execute("SELECT * FROM label WHERE label_id = ?",
                             (label_id,)).fetchone())


def delete_label(conn: sqlite3.Connection, label_id: int) -> bool:
    cur = conn.execute("DELETE FROM label WHERE label_id = ?", (label_id,))
    conn.commit()
    return cur.rowcount > 0


def iter_labels(conn: sqlite3.Connection):
    """내보내기 형식의 **정본**. 웹 라우트(/api/labels/export)와 CLI
    (export_labels.py)가 같은 dict 를 쓴다 — 형식이 두 곳에 적히면 갈라진다.

    - type 을 web_label 로 둔다: labels/ 파이프라인의 labels.jsonl 과 파일이
      섞여도 apply_review·report_eval 이 조용히 먹지 않게 (저쪽은 type 없는
      sample/label 행이다)
    - 좌표를 함께 싣는다: 복원할 DB 에 pano 행이 없어도 되살릴 수 있어야
      한다 — DB 는 gitignore 라 이 파일이 라벨의 유일한 백업이다
    """
    for r in conn.execute(
            "SELECT l.*, p.lat, p.lng FROM label l JOIN pano p USING(pano_id) "
            "ORDER BY l.label_id"):
        yield {"type": "web_label", "pano_id": r["pano_id"],
               "lat": r["lat"], "lng": r["lng"], "heading": r["heading"],
               "is_trail": bool(r["is_trail"]), "note": r["note"],
               "author": r["author"], "created_at": r["created_at"],
               "updated_at": r["updated_at"]}


def restore_label(conn: sqlite3.Connection, rec: dict) -> str:
    """내보낸 한 줄을 되살린다. 반환 ∈ {restored, kept}.

    이미 있는 라벨이 더 최신(updated_at)이면 건드리지 않는다 — 복원이
    그 사이의 새 판단을 덮어쓰면 백업이 데이터를 지우는 도구가 된다.
    """
    hkey = -1.0 if rec.get("heading") is None else rec["heading"]
    row = conn.execute(
        "SELECT updated_at FROM label WHERE pano_id = ? "
        "AND COALESCE(heading, -1.0) = ?", (rec["pano_id"], hkey)).fetchone()
    if row and row["updated_at"] >= rec["updated_at"]:
        return "kept"
    upsert_pano(conn, rec["pano_id"], rec["lat"], rec["lng"])
    put_label(conn, pano_id=rec["pano_id"], is_trail=rec["is_trail"],
              note=rec.get("note"), heading=rec.get("heading"),
              author=rec.get("author", "restore"), updated_at=rec["updated_at"])
    return "restored"


# ── 잡 큐 — 웹이 넣고 워커 데몬이 집는다 ────────────────────────────────────

def enqueue_job(conn: sqlite3.Connection, *, start_lat: float, start_lng: float,
                bearing: float, radius_m: float, max_seconds: float,
                config_path: str | None = None) -> dict:
    jid = conn.execute(
        "INSERT INTO job (state, start_lat, start_lng, bearing, radius_m,"
        " max_seconds, config_path, created_at) "
        "VALUES ('queued', ?, ?, ?, ?, ?, ?, ?)",
        (start_lat, start_lng, bearing, radius_m, max_seconds,
         config_path, _now())).lastrowid
    conn.commit()
    return job_row(conn, jid)


def claim_job(conn: sqlite3.Connection, worker_id: str) -> dict | None:
    """queued 하나를 원자적으로 집는다.

    단일 UPDATE 라 두 워커가 동시에 불러도 하나만 행을 얻는다. 데몬이
    하나라는 운영 전제를 믿지 않는 이유는 **재시작 중첩**이다 — 옛
    프로세스가 안 죽은 채 새것이 뜨는 일이 실제로 흔하다.
    """
    now = _now()
    row = conn.execute(
        "UPDATE job SET state = 'claimed', worker_id = ?, claimed_at = ?,"
        " heartbeat_at = ? "
        "WHERE job_id = (SELECT job_id FROM job WHERE state = 'queued'"
        "                ORDER BY job_id LIMIT 1) "
        "  AND state = 'queued' RETURNING *", (worker_id, now, now)).fetchone()
    conn.commit()
    return dict(row) if row else None


def job_row(conn: sqlite3.Connection, job_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
    return dict(r) if r else None


def jobs_list(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM job ORDER BY job_id DESC LIMIT ?", (limit,))]


def heartbeat_job(conn: sqlite3.Connection, job_id: int,
                  state: str | None = None) -> None:
    if state:
        conn.execute("UPDATE job SET heartbeat_at = ?, state = ? WHERE job_id = ?",
                     (_now(), state, job_id))
    else:
        conn.execute("UPDATE job SET heartbeat_at = ? WHERE job_id = ?",
                     (_now(), job_id))
    conn.commit()


def set_job_progress(conn: sqlite3.Connection, job_id: int, progress: dict) -> None:
    conn.execute("UPDATE job SET progress_json = ? WHERE job_id = ?",
                 (json.dumps(progress, ensure_ascii=False), job_id))
    # 커밋은 호출자(RunWriter.probe 의 트랜잭션)가 한다 — 판정과 진행이
    # 같은 커밋 경계 안에 있어야 "판정은 남았는데 진행이 안 맞는" 상태가 없다


def request_cancel(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """queued 면 즉시 canceled, 실행 중이면 cancel_requested 만 세운다 —
    실제 중단은 워커가 다음 후보 경계에서 한다 (explore 의 cancel 콜백)."""
    conn.execute("UPDATE job SET state = 'canceled', finished_at = ? "
                 "WHERE job_id = ? AND state = 'queued'", (_now(), job_id))
    conn.execute("UPDATE job SET cancel_requested = 1 "
                 "WHERE job_id = ? AND state IN ('claimed', 'running')", (job_id,))
    conn.commit()
    return job_row(conn, job_id)


def cancel_requested(conn: sqlite3.Connection, job_id: int) -> bool:
    r = conn.execute("SELECT cancel_requested FROM job WHERE job_id = ?",
                     (job_id,)).fetchone()
    return bool(r and r["cancel_requested"])


def finish_job(conn: sqlite3.Connection, job_id: int, *, state: str,
               run_id: int | None = None, stop_reason: str | None = None,
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE job SET state = ?, run_id = ?, stop_reason = ?, error = ?,"
        " finished_at = ? WHERE job_id = ?",
        (state, run_id, stop_reason, error, _now(), job_id))
    conn.commit()


def reap_stale_jobs(conn: sqlite3.Connection, lease_s: float) -> int:
    """하트비트가 lease 를 넘긴 잡을 failed 로 접는다. **자동 재큐잉은 안
    한다** — 6시간짜리가 절반에서 조용히 다시 시작되면 판정이 두 벌 쌓이고
    사용자는 왜 두 배 걸렸는지 모른다. 이미 기록된 판정은 그대로 유효하다
    (판정 불변). 사람이 "다시" 를 누르면 새 잡·새 런이다.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=lease_s)
              ).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE job SET state = 'failed', error = 'worker_lost', finished_at = ? "
        "WHERE state IN ('claimed', 'running') AND heartbeat_at < ?",
        (_now(), cutoff))
    conn.commit()
    return cur.rowcount

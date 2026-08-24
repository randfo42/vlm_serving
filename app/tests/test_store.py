"""store — SQLite 정본의 불변식.

이 테스트가 거는 것은 runlog 시절의 계약이 DB 로 넘어와서도 성립하는가다:
판정 1건 = 커밋 1번(중간에 죽어도 남는다), 이름순 = 호출순, tally 의 즉시 검증,
문구 실패가 finish 를 막지 않는 것. "바이트 동일" 만은 JSONL 고유라 "필드 동일" 로
바뀌었다 (test_review_and_eval 의 라벨 직렬화 테스트와 같은 전환).
"""
from dataclasses import dataclass

import pytest

from trailwalk import store, warn


@dataclass
class FakeVerdict:
    is_trail: bool = True
    confidence: int | None = 7
    prompt_tokens: int = 100
    cached_tokens: int = 90
    completion_tokens: int = 10
    latency_ms: float = 123.4
    camera_surface: str | None = None
    nature_level: int | None = None
    footway: int | None = None


HEADER = {"provider": "kakao", "mode": "explore", "schema": "nature_footway",
          "url": "http://127.0.0.1:8000/v1/chat/completions",
          "start": [37.55, 127.01], "start_bearing": 90.0,
          "ts": "2026-08-24T00:00:00+00:00",
          "prompt": {"system_version": "system_v6", "system_sha256": "abc"}}


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.migrate(c)
    yield c
    c.close()


def writer(conn, image_dir=None, header=HEADER):
    return store.RunWriter(conn, dict(header), name="testrun", image_dir=image_dir)


# ── 스키마 ──────────────────────────────────────────────────────────────────

def test_migrate_creates_schema(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pano", "run", "verdict", "node", "frontier",
            "warning", "event", "label", "job"} <= tables
    v = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    assert v[0] == str(store.SCHEMA_VERSION)


def test_migrate_idempotent(conn):
    store.migrate(conn)   # 두 번 불러도 터지지 않는다


def test_migrate_version_mismatch_raises(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.migrate(c)
    c.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
    c.commit()
    with pytest.raises(store.StoreError):
        store.migrate(c)
    c.close()


def test_wal_mode(tmp_path):
    # 워커가 쓰는 동안 웹이 읽어야 한다 — WAL 이 아니면 리더가 라이터를 막는다
    c = store.connect(tmp_path / "t.db")
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


# ── RunWriter.probe ─────────────────────────────────────────────────────────

def test_probe_inserts_verdict_and_pano(conn):
    w = writer(conn)
    w.probe(step=0, pano_id="P1", lat=37.5, lng=127.0, heading=90.0,
            verdict=FakeVerdict(nature_level=2, footway=1), src_format="PNG")
    v = conn.execute("SELECT * FROM verdict").fetchone()
    assert (v["pano_id"], v["is_trail"], v["nature_level"], v["footway"]) == ("P1", 1, 2, 1)
    p = conn.execute("SELECT * FROM pano").fetchone()
    assert (p["pano_id"], p["lat"], p["lng"]) == ("P1", 37.5, 127.0)


def test_probe_is_durable_without_finish(tmp_path):
    # 판정 1건 = 커밋 1번. finish 전에 프로세스가 죽어도 다른 커넥션에서 보인다
    c = store.connect(tmp_path / "t.db")
    store.migrate(c)
    w = writer(c)
    w.probe(step=0, pano_id="P1", lat=37.5, lng=127.0, heading=0.0,
            verdict=FakeVerdict(), src_format="PNG")
    c2 = store.connect(tmp_path / "t.db", read_only=True)
    assert c2.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 1
    # finish 를 안 불렀으니 run 은 "중간에 죽은 런" 모양이어야 한다
    assert c2.execute("SELECT finished_at FROM run").fetchone()[0] is None
    c2.close()
    c.close()


def test_pano_coords_first_wins(conn):
    # 같은 pano 가 두 좌표를 갖는 상태를 표현할 수 없어야 한다 — 선착순 고정
    w = writer(conn)
    w.probe(step=0, pano_id="P1", lat=37.5, lng=127.0, heading=0.0,
            verdict=FakeVerdict(), src_format="PNG")
    w.probe(step=1, pano_id="P1", lat=99.9, lng=99.9, heading=180.0,
            verdict=FakeVerdict(), src_format="PNG")
    rows = conn.execute("SELECT lat, lng FROM pano").fetchall()
    assert len(rows) == 1 and (rows[0]["lat"], rows[0]["lng"]) == (37.5, 127.0)
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 2


def test_image_filename_rule(conn, tmp_path):
    # 번호가 앞 — 이름순이 곧 호출 순서. 확장자는 감지된 실제 포맷을 따른다
    d = tmp_path / "imgs"
    w = writer(conn, image_dir=d)
    w.probe(step=0, pano_id="P1", lat=37.5, lng=127.0, heading=90.0,
            verdict=FakeVerdict(is_trail=True), src_format="PNG", image=b"x")
    w.probe(step=1, pano_id="P2", lat=37.5, lng=127.0, heading=7.5,
            verdict=FakeVerdict(is_trail=False), src_format="JPEG", image=b"y")
    names = sorted(p.name for p in d.iterdir())
    assert names == ["001_s00_P1_090.0_T.png", "002_s01_P2_007.5_F.jpg"]
    paths = [r[0] for r in conn.execute(
        "SELECT image_path FROM verdict ORDER BY verdict_id")]
    assert paths == [f"{d.name}/001_s00_P1_090.0_T.png",
                     f"{d.name}/002_s01_P2_007.5_F.jpg"]


def test_no_image_dir_no_image_path(conn):
    w = writer(conn)
    w.probe(step=0, pano_id="P1", lat=37.5, lng=127.0, heading=0.0,
            verdict=FakeVerdict(), src_format="PNG", image=b"ignored")
    assert conn.execute("SELECT image_path FROM verdict").fetchone()[0] is None


# ── 경고 채널 (warn.py 의 두 형태) ──────────────────────────────────────────

def test_warn_immediate_row(conn):
    w = writer(conn)
    w.warn("no_coverage", radius_m=250.0)
    r = conn.execute("SELECT * FROM warning").fetchone()
    assert r["kind"] == "once" and r["code"] == "no_coverage"
    assert "250" in r["message"]


def test_tally_upserts_single_row(conn):
    w = writer(conn)
    w.tally("neighbors_missing")
    w.tally("neighbors_missing")
    w.tally("neighbors_missing", count=3)
    rows = conn.execute("SELECT * FROM warning WHERE code='neighbors_missing'").fetchall()
    assert len(rows) == 1 and rows[0]["count"] == 5
    assert "5곳" in rows[0]["message"]


def test_tally_durable_before_finish(tmp_path):
    # 런로그는 집계형이 run_end 에서만 완성돼 중간에 죽으면 사라졌다.
    # 여기서는 tally 호출 시점에 이미 내구적이어야 한다
    c = store.connect(tmp_path / "t.db")
    store.migrate(c)
    w = writer(c)
    w.tally("parse_failure", count=2)
    c2 = store.connect(tmp_path / "t.db", read_only=True)
    assert c2.execute("SELECT count FROM warning WHERE code='parse_failure'"
                      ).fetchone()[0] == 2
    c2.close()
    c.close()


def test_tally_unknown_code_raises_at_call(conn):
    # finish 까지 미루면 finally 안에서 터져 런 요약이 날아간다 (runlog 와 같은 계약)
    w = writer(conn)
    with pytest.raises(warn.UnknownWarning):
        w.tally("nonexistent_code")


def test_tally_message_fallback(conn):
    # cache_miss 문구는 {count}/{calls} 를 요구한다. calls 없이 불러도 explore
    # 루프를 죽이면 안 된다 — 자리표시자 문구로 격하된다
    w = writer(conn)
    w.tally("cache_miss", count=3)
    r = conn.execute("SELECT * FROM warning WHERE code='cache_miss'").fetchone()
    assert r["count"] == 3 and "cache_miss" in r["message"]
    w.tally("cache_miss", count=1, calls=10)   # 필드가 채워지면 정식 문구로
    r = conn.execute("SELECT * FROM warning WHERE code='cache_miss'").fetchone()
    assert r["count"] == 4 and "4/10" in r["message"]


# ── run 행 ──────────────────────────────────────────────────────────────────

def test_run_row_from_header(conn):
    writer(conn)
    r = conn.execute("SELECT * FROM run").fetchone()
    assert (r["kind"], r["provider"], r["prompt_version"]) == \
        ("explore", "kakao", "system_v6")
    assert r["start_lat"] == 37.55 and r["vlm_url"].startswith("http://127.0.0.1")


def test_run_kind_fallbacks(conn):
    # 옛 walk 런로그에는 mode 가 없다. eval 은 labels_path 로 구분한다
    h = {k: v for k, v in HEADER.items() if k != "mode"}
    store.RunWriter(conn, h, name="r-walk")
    store.RunWriter(conn, {**h, "labels_path": "x.jsonl"}, name="r-eval")
    kinds = dict(conn.execute("SELECT name, kind FROM run").fetchall())
    assert kinds["r-walk"] == "walk" and kinds["r-eval"] == "eval"


def test_finish_sets_summary(conn):
    w = writer(conn)
    w.finish(stop_reason="exhausted", nodes=5, calls=12)
    r = conn.execute("SELECT * FROM run").fetchone()
    assert r["stop_reason"] == "exhausted" and r["finished_at"] is not None
    assert '"nodes": 5' in r["summary_json"]


def test_duplicate_run_name_rejected(conn):
    # name 은 멱등성의 축이다 — 같은 이름의 런이 둘이면 백필 재실행을 감지 못 한다
    writer(conn)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        writer(conn)

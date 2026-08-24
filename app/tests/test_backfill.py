"""백필 — 옛 런로그 3세대를 무손실로, 두 번 돌려도 한 벌로.

실측한 함정들이 그대로 테스트다: 헤더에 mode 가 없는 세대, run_end 없이
잘린 파일(실제 5건), fixture 런과 metrics 파일이 같은 폴더에 섞여 있는 것,
labels/ 의 JSONL 이 run_start 어휘를 흉내내는 것(경로 제한이 방어선).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from trailwalk import store

_spec = importlib.util.spec_from_file_location(
    "backfill_runs", Path(__file__).resolve().parent.parent / "backfill_runs.py")
backfill = importlib.util.module_from_spec(_spec)
sys.modules["backfill_runs"] = backfill
_spec.loader.exec_module(backfill)


def jsonl(path: Path, *rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")
    return path


def probe(pano="P1", heading=90.0, **kw):
    return {"type": "probe", "step": 0, "pano_id": pano, "lat": 37.5, "lng": 127.0,
            "heading": heading, "is_trail": True, "confidence": 7,
            "prompt_tokens": 100, "cached_tokens": 90, "completion_tokens": 10,
            "latency_ms": 1200.0, "src_format": "PNG", **kw}


GEN1_HEADER = {"type": "run_start", "ts": "2026-08-17T08:38:58+00:00",
               "provider": "kakao", "schema": "walk",
               "url": "http://192.168.0.15:8000/v1/chat/completions",
               "start": [37.55, 127.01], "start_bearing": 0.0,
               "config": {"step_m": 10, "max_steps": 30},
               "prompt": {"system_version": "system_v1", "system_sha256": "aaa"}}

GEN3_HEADER = {"type": "run_start", "ts": "2026-08-22T08:43:05+00:00",
               "provider": "kakao", "mode": "explore", "schema": "nature_footway",
               "url": "http://192.168.0.15:8000/v1/chat/completions",
               "start": [37.55, 127.01], "start_bearing": 0.0,
               "config": {"max_distance_m": 1000.0, "image": {"target": [896, 896]}},
               "config_path": "/x/y.yaml",
               "prompt": {"system_version": "system_v6", "system_sha256": "bbb"}}


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.migrate(c)
    yield c
    c.close()


def test_1세대_walk_헤더를_흡수한다(conn, tmp_path):
    # mode 가 없다 → kind=walk. config 는 해석하지 않고 원문 보관
    f = jsonl(tmp_path / "old-walk.jsonl", GEN1_HEADER, probe(),
              {"type": "run_end", "wall_s": 55.0, "stop_reason": "exhausted",
               "warnings": []})
    result, info = backfill.import_file(conn, f, force=False)
    assert result == "imported" and info["probes"] == 1
    r = conn.execute("SELECT * FROM run").fetchone()
    assert (r["kind"], r["prompt_version"], r["source"]) == \
        ("walk", "system_v1", "backfill")
    assert "step_m" in r["header_json"], "옛 config 원문이 유실됐다"
    assert r["stop_reason"] == "exhausted"
    # finished_at = ts + wall_s. run_end 자체엔 시각이 없어 이렇게 복원한다
    assert r["finished_at"] == "2026-08-17T08:39:53+00:00"


def test_3세대_explore와_경고_두_형태를_옮긴다(conn, tmp_path):
    f = jsonl(
        tmp_path / "v6-run.jsonl", GEN3_HEADER,
        probe(nature_level=2, footway=1),
        {"type": "warning", "code": "no_coverage",
         "message": "시작점 반경 250m 안에 로드뷰가 없다"},
        {"type": "event", "kind": "warmup", "ms": 123.0},
        {"type": "run_end", "wall_s": 10.0, "stop_reason": "exhausted",
         "warnings": [
             {"code": "no_coverage", "message": "…"},                # 즉시(중복)
             {"code": "cache_miss", "count": 3, "message": "미스"},  # 집계형
         ]})
    result, _ = backfill.import_file(conn, f, force=False)
    assert result == "imported"
    v = conn.execute("SELECT nature_level, footway FROM verdict").fetchone()
    assert (v["nature_level"], v["footway"]) == (2, 1)
    # 즉시 경고는 once 한 건, run_end 에만 있던 것은 tally — 이중으로 안 들어간다
    rows = conn.execute("SELECT kind, code, count FROM warning ORDER BY code").fetchall()
    assert [(r["kind"], r["code"], r["count"]) for r in rows] == \
        [("tally", "cache_miss", 3), ("once", "no_coverage", None)]
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1


def test_run_end_없는_파일은_실패가_아니다(conn, tmp_path):
    # 실측: kakao 런 24건 중 5건이 이렇다 (중간에 죽은 런)
    f = jsonl(tmp_path / "killed.jsonl", GEN3_HEADER, probe(), probe("P2", 270.0))
    result, info = backfill.import_file(conn, f, force=False)
    assert result == "imported" and info["truncated"]
    r = conn.execute("SELECT stop_reason, finished_at FROM run").fetchone()
    assert r["stop_reason"] is None and r["finished_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM warning WHERE code='truncated_runlog'"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 2


def test_대상이_아닌_파일들(conn, tmp_path):
    # metrics 파일(첫 줄이 run_start 아님) · fixture 런 · eval 런
    m = jsonl(tmp_path / "x.metrics.jsonl", {"latency_p50": 1.2})
    fx = jsonl(tmp_path / "fx.jsonl", {**GEN1_HEADER, "provider": "fixture"}, probe())
    ev = jsonl(tmp_path / "ev.jsonl", {**GEN3_HEADER, "labels_path": "l.jsonl"},
               probe(label=True, sample_id="s1"))
    for f in (m, fx, ev):
        assert backfill.import_file(conn, f, force=False)[0] == "not_runlog"
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0


def test_멱등성_같은_파일은_한_벌만(conn, tmp_path):
    f = jsonl(tmp_path / "r.jsonl", GEN3_HEADER, probe(),
              {"type": "run_end", "wall_s": 1.0, "stop_reason": "exhausted",
               "warnings": []})
    assert backfill.import_file(conn, f, force=False)[0] == "imported"
    conn.commit()
    assert backfill.import_file(conn, f, force=False)[0] == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 1


def test_내용이_바뀐_파일은_force_없이_거부(conn, tmp_path):
    f = jsonl(tmp_path / "r.jsonl", GEN3_HEADER, probe())
    backfill.import_file(conn, f, force=False)
    conn.commit()
    jsonl(f, GEN3_HEADER, probe(), probe("P2", 180.0))    # 파일이 이어 쓰였다
    assert backfill.import_file(conn, f, force=False)[0] == "refused"
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 1
    # --force 면 지우고 다시 — 두 벌이 되지 않는다
    assert backfill.import_file(conn, f, force=True)[0] == "imported"
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 1


def test_깨진_줄은_파일_통째로_롤백된다(conn, tmp_path):
    f = tmp_path / "broken.jsonl"
    f.write_text(json.dumps(GEN3_HEADER) + "\n"
                 + json.dumps(probe()) + "\n"
                 + '{"type": "probe", 깨짐', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        backfill.import_file(conn, f, force=False)
    conn.rollback()      # main 이 하는 일 — 부분 임포트가 남으면 안 된다
    assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 0


def test_이미지는_파일이_실제로_있을_때만_경로가_남는다(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "APP", tmp_path)
    d = tmp_path / "runs" / "images" / "r"
    d.mkdir(parents=True)
    (d / "001_s00_P1_090.0_T.png").write_bytes(b"x")
    f = jsonl(tmp_path / "r.jsonl", GEN3_HEADER,
              probe(image="001_s00_P1_090.0_T.png"),
              probe("P2", 180.0, image="없는파일.png"))
    backfill.import_file(conn, f, force=False)
    paths = [r[0] for r in conn.execute(
        "SELECT image_path FROM verdict ORDER BY verdict_id")]
    assert paths == ["r/001_s00_P1_090.0_T.png", None]

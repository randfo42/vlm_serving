"""사람 라벨 — 갱신 규칙과 백업 왕복.

라벨은 이 스키마에서 UPDATE 되는 유일한 것이고(updated_at 이 그 표시),
DB 가 gitignore 라 내보내기 파일이 유일한 백업이다. 지키는 것:

- created_at 은 처음 값 고정, updated_at 만 움직인다
- 지도에 없는 pano 에는 못 단다 (오타이지 데이터가 아니다)
- 내보내기 → DB 삭제 → 복원 이 무손실이다
- 복원은 더 최신인 로컬 라벨을 덮지 않는다 — 백업이 데이터를 지우는
  도구가 되면 안 된다
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web import api as web_api

from trailwalk import settings, store

NOW = "2026-08-24T00:00:00+00:00"

_spec = importlib.util.spec_from_file_location(
    "export_labels", Path(__file__).resolve().parent.parent / "export_labels.py")
export_labels = importlib.util.module_from_spec(_spec)
sys.modules["export_labels"] = export_labels
_spec.loader.exec_module(export_labels)


def seed(db):
    conn = store.connect(db)
    store.migrate(conn)
    store.upsert_pano(conn, "P1", 37.50, 127.00, now=NOW)
    store.upsert_pano(conn, "P2", 37.51, 127.01, now=NOW)
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "t.db"
    seed(db)
    app = web_api.create_app(settings.load(None), db)
    with TestClient(app) as c:
        c.db = db
        yield c


def test_라벨_생성과_갱신(client):
    r = client.put("/api/labels", json={"pano_id": "P1", "is_trail": True,
                                        "note": "하천 보행로"})
    assert r.status_code == 200
    first = r.json()
    assert first["is_trail"] == 1 and first["note"] == "하천 보행로"

    r = client.put("/api/labels", json={"pano_id": "P1", "is_trail": False})
    second = r.json()
    # 같은 pano 는 한 행 — 판단을 바꾼 것이지 판단이 둘이 된 게 아니다
    assert second["label_id"] == first["label_id"]
    assert second["created_at"] == first["created_at"], "created_at 이 움직였다"
    assert second["is_trail"] == 0


def test_모르는_pano에는_못_단다(client):
    r = client.put("/api/labels", json={"pano_id": "유령", "is_trail": True})
    assert r.status_code == 404


def test_라벨이_뷰포트에_실린다(client):
    conn = store.connect(client.db)
    r6 = store.insert_run(conn, name="r6", kind="explore", provider="kakao",
                          source="live", started_at=NOW, header_json="{}",
                          prompt_version="system_v6")
    store.insert_verdict(conn, run_id=r6, pano_id="P1", heading=0.0,
                         is_trail=False, nature_level=1, created_at=NOW)
    conn.commit()
    conn.close()
    client.put("/api/labels", json={"pano_id": "P1", "is_trail": True})
    r = client.get("/api/panos", params={"s": 37.49, "w": 126.99,
                                         "n": 37.52, "e": 127.02})
    p1 = next(p for p in r.json()["panos"] if p["pano_id"] == "P1")
    # 모델은 아니라는데(0) 사람은 맞다고 했다(1) — 둘 다 그대로 보여야 한다
    assert (p1["is_trail"], p1["label"]) == (0, 1)


def test_삭제(client):
    lid = client.put("/api/labels",
                     json={"pano_id": "P1", "is_trail": True}).json()["label_id"]
    assert client.delete(f"/api/labels/{lid}").status_code == 204
    assert client.delete(f"/api/labels/{lid}").status_code == 404


def test_내보내기_왕복이_무손실이다(client, tmp_path):
    client.put("/api/labels", json={"pano_id": "P1", "is_trail": True,
                                    "note": "메모"})
    client.put("/api/labels", json={"pano_id": "P2", "is_trail": False})
    # 웹 내보내기와 CLI 가 같은 형식(store.iter_labels)을 쓴다
    text = client.get("/api/labels/export").text
    recs = [json.loads(line) for line in text.splitlines()]
    assert len(recs) == 2 and all(r["type"] == "web_label" for r in recs)
    assert all("lat" in r for r in recs), "좌표가 없으면 빈 DB 에 복원 불가"

    out = tmp_path / "backup.jsonl"
    out.write_text(text, encoding="utf-8")
    fresh = tmp_path / "fresh.db"       # DB 를 잃은 상황
    conn = store.connect(fresh)
    store.migrate(conn)
    results = [store.restore_label(conn, json.loads(line))
               for line in out.read_text(encoding="utf-8").splitlines()]
    assert results == ["restored", "restored"]
    rows = conn.execute("SELECT pano_id, is_trail, note FROM label "
                        "ORDER BY pano_id").fetchall()
    assert [(r["pano_id"], r["is_trail"], r["note"]) for r in rows] == \
        [("P1", 1, "메모"), ("P2", 0, None)]
    conn.close()


def test_복원은_더_최신인_로컬을_덮지_않는다(tmp_path):
    db = tmp_path / "t.db"
    seed(db)
    conn = store.connect(db)
    store.put_label(conn, pano_id="P1", is_trail=True,
                    updated_at="2026-08-24T12:00:00+00:00")
    old_backup = {"type": "web_label", "pano_id": "P1", "lat": 37.5, "lng": 127.0,
                  "heading": None, "is_trail": False, "note": None,
                  "author": "web", "created_at": NOW,
                  "updated_at": "2026-08-20T00:00:00+00:00"}
    assert store.restore_label(conn, old_backup) == "kept"
    assert conn.execute("SELECT is_trail FROM label").fetchone()[0] == 1
    conn.close()


def test_restore는_web_label_아닌_줄을_거른다(tmp_path, monkeypatch, capsys):
    # labels/ 파이프라인의 labels.jsonl 을 잘못 넣어도 조용히 먹지 않는다
    db = tmp_path / "t.db"
    seed(db)
    f = tmp_path / "mixed.jsonl"
    f.write_text(json.dumps({"type": "sample", "sample_id": "s1"}) + "\n" +
                 json.dumps({"type": "web_label", "pano_id": "P1", "lat": 37.5,
                             "lng": 127.0, "heading": None, "is_trail": True,
                             "note": None, "author": "web", "created_at": NOW,
                             "updated_at": NOW}) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["export_labels.py", "--restore", str(f), "--db", str(db)])
    assert export_labels.main() == 0
    out = capsys.readouterr().out
    assert "되살림 1" in out and "web_label 아님 1" in out

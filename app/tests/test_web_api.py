"""웹 API — 조회 계약과 두 불변식.

여기서 고정하는 것:

- **웹 프로세스에 Playwright 가 안 들어온다** (서브프로세스로 확인). 탐색
  실행은 워커의 일이고, 이 선이 흐리면 "웹이 브라우저를 안 만진다" 는
  결정이 조용히 깨진다.
- **뷰포트 기본 필터 = 현재 프롬프트 버전.** MAX 는 한 pano 안 방위들
  사이의 규칙이라, 버전을 가로질러 걸면 폐기된 버전의 오탐 하나가 그 점을
  영원히 초록으로 만든다.
- 이미지는 verdict_id 로만 찾고 runs/images 밖은 404 — 경로를 URL 로 받지
  않는 것이 경로 순회 방어의 전부다.
- 실행계획: 뷰포트 쿼리가 pano_bbox + verdict_agg 커버링 인덱스를 탄다.
"""
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web import api as web_api

from trailwalk import settings, store

NOW = "2026-08-24T00:00:00+00:00"


def seed(db):
    conn = store.connect(db)
    store.migrate(conn)
    r6 = store.insert_run(conn, name="r6", kind="explore", provider="kakao",
                          source="live", started_at=NOW, header_json="{}",
                          prompt_version="system_v6")
    r3 = store.insert_run(conn, name="r3", kind="explore", provider="kakao",
                          source="backfill", started_at=NOW, header_json="{}",
                          prompt_version="system_v3",
                          vlm_url="http://192.168.0.15:8000/v1")
    store.upsert_pano(conn, "P1", 37.50, 127.00, now=NOW)
    store.upsert_pano(conn, "P2", 37.51, 127.01, now=NOW)
    v = dict(run_id=r6, pano_id="P1", created_at=NOW)
    store.insert_verdict(conn, heading=0.0, is_trail=False, nature_level=1,
                         footway=0, **v)
    store.insert_verdict(conn, heading=180.0, is_trail=True, nature_level=2,
                         footway=1, **v)
    store.insert_verdict(conn, run_id=r6, pano_id="P2", heading=90.0,
                         is_trail=False, nature_level=0, created_at=NOW)
    # 폐기된 v3 의 오탐 — 교차 버전 MAX 를 걸면 P2 가 초록이 되는 재료
    store.insert_verdict(conn, run_id=r3, pano_id="P2", heading=90.0,
                         is_trail=True, created_at=NOW)
    conn.execute("INSERT INTO label (pano_id, heading, is_trail, author,"
                 " created_at, updated_at) VALUES ('P1', NULL, 1, 'me', ?, ?)",
                 (NOW, NOW))
    conn.commit()
    conn.close()
    return {"r6": r6, "r3": r3}


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "t.db"
    seed(db)
    app = web_api.create_app(settings.load(None), db)
    with TestClient(app) as c:
        yield c


BBOX = {"s": 37.49, "w": 126.99, "n": 37.52, "e": 127.02}


def test_뷰포트_기본은_현재_버전만_MAX_집계(client):
    r = client.get("/api/panos", params=BBOX)
    assert r.status_code == 200
    body = r.json()
    assert not body["truncated"]
    by_id = {p["pano_id"]: p for p in body["panos"]}
    assert set(by_id) == {"P1", "P2"}
    # P1: 방위 둘 중 MAX — nature 2, is_trail OR → 1. 사람 라벨도 실린다
    assert (by_id["P1"]["nature_level"], by_id["P1"]["is_trail"],
            by_id["P1"]["n"], by_id["P1"]["label"]) == (2, 1, 2, 1)
    # P2: v3 의 오탐(is_trail=1)이 **안 섞여야** 한다 — 기본 필터는 v6
    assert (by_id["P2"]["is_trail"], by_id["P2"]["n"]) == (0, 1)


def test_버전을_고르면_그_버전만(client):
    r = client.get("/api/panos", params={**BBOX, "prompt_version": "system_v3"})
    panos = r.json()["panos"]
    assert [p["pano_id"] for p in panos] == ["P2"]
    assert panos[0]["is_trail"] == 1 and panos[0]["nature_level"] is None


def test_잘림은_조용히_지나가지_않는다(client):
    r = client.get("/api/panos", params={**BBOX, "limit": 1})
    body = r.json()
    assert body["truncated"] and len(body["panos"]) == 1


def test_뒤집힌_bbox는_422(client):
    r = client.get("/api/panos", params={"s": 37.52, "n": 37.49,
                                         "w": 126.99, "e": 127.02})
    assert r.status_code == 422


def test_headings는_요청할_때만(client):
    r = client.get("/api/panos", params=BBOX).json()
    assert "headings" not in r["panos"][0]
    r = client.get("/api/panos", params={**BBOX, "headings": True}).json()
    p1 = next(p for p in r["panos"] if p["pano_id"] == "P1")
    assert [h["heading"] for h in p1["headings"]] == [0.0, 180.0]


def test_pano_상세는_버전_필터_없이_이력_전부(client):
    d = client.get("/api/pano/P2").json()
    # v6 과 v3 판정이 같이 보여야 한다 — 같은 지점을 버전이 다르게 봤다면
    # 그 자리에서 보이는 것이 이 화면의 존재 이유다
    assert {v["prompt_version"] for v in d["verdicts"]} == \
        {"system_v6", "system_v3"}
    assert client.get("/api/pano/없는것").status_code == 404


def test_이미지_없는_판정은_404_no_image(client):
    r = client.get("/api/image/1")
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "no_image"


def test_이미지는_runs_images_밖을_가리키면_404(client, tmp_path, monkeypatch):
    # DB 행이 조작돼도 서빙 루트 밖은 못 나간다
    root = tmp_path / "images"
    (root / "r").mkdir(parents=True)
    (root / "r" / "ok.png").write_bytes(b"png")
    secret = tmp_path / "secret.txt"
    secret.write_text("leak")
    monkeypatch.setattr(web_api, "IMAGES_ROOT", root)
    conn = store.connect(client.app.state.db)
    conn.execute("UPDATE verdict SET image_path='r/ok.png' WHERE verdict_id=1")
    conn.execute("UPDATE verdict SET image_path='../secret.txt' WHERE verdict_id=2")
    conn.commit()
    conn.close()
    assert client.get("/api/image/1").status_code == 200
    assert client.get("/api/image/2").status_code == 404


def test_run_상세는_주소를_내보내지_않는다(client):
    runs = client.get("/api/runs").json()["runs"]
    r3 = next(r for r in runs if r["name"] == "r3")
    d = client.get(f"/api/runs/{r3['run_id']}")
    # vlm_url(LAN IP)·header_json 은 브라우저로 나가면 안 된다 — 페이지를
    # 캡처해 공유하는 순간 새는 자리다
    assert "192.168" not in d.text
    assert "vlm_url" not in d.json()


def test_health와_versions(client):
    h = client.get("/api/health").json()
    assert h["counts"]["verdict"] == 4 and h["counts"]["pano"] == 2
    vs = client.get("/api/versions").json()["versions"]
    assert {(v["prompt_version"], v["verdicts"]) for v in vs} == \
        {("system_v6", 3), ("system_v3", 1)}


def test_뷰포트_실행계획이_인덱스를_탄다(tmp_path):
    # 이 쿼리가 풀스캔으로 조용히 느려지면 지도가 "그냥 굼떠" 보일 뿐
    # 에러가 없다 — 실행계획 자체를 계약으로 고정한다
    db = tmp_path / "t.db"
    seed(db)
    conn = store.connect(db)
    plan = "\n".join(r[3] for r in conn.execute(
        """EXPLAIN QUERY PLAN
        SELECT p.pano_id,
          (SELECT MAX(v.nature_level) FROM verdict v WHERE v.pano_id = p.pano_id
             AND v.run_id IN (SELECT value FROM json_each('[1]')))
        FROM pano p WHERE p.lat BETWEEN 37 AND 38 AND p.lng BETWEEN 126 AND 128
          AND EXISTS (SELECT 1 FROM verdict v WHERE v.pano_id = p.pano_id
                        AND v.run_id IN (SELECT value FROM json_each('[1]')))
        LIMIT 100"""))
    conn.close()
    assert "USING INDEX pano_bbox" in plan, plan
    assert "USING COVERING INDEX verdict_agg" in plan, plan


def test_웹은_playwright를_임포트하지_않는다():
    """결정의 유일한 자동 방어선. sys.modules 는 이 프로세스에서 이미
    오염됐을 수 있어 새 인터프리터로 확인한다."""
    app_dir = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; import web.api; "
         "bad = [m for m in ('playwright', 'trailwalk.providers.kakao',"
         " 'trailwalk.runner') if m in sys.modules]; "
         "assert not bad, f'웹이 물어온 금지 모듈: {bad}'"],
        cwd=app_dir, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_다른_버전_행이_LIMIT_예산을_먹지_않는다(tmp_path):
    """리뷰가 잡은 버그의 회귀 방어: bbox 안에 v3 전용 pano 가 잔뜩이고
    limit 이 작을 때, v6 pano 가 조회조차 안 된 채 빠지면서 truncated 는
    False 로 오던 문제 — 버전 필터가 LIMIT **앞**(SQL)에 있어야 한다."""
    db = tmp_path / "t.db"
    conn = store.connect(db)
    store.migrate(conn)
    r6 = store.insert_run(conn, name="r6", kind="explore", provider="kakao",
                          source="live", started_at=NOW, header_json="{}",
                          prompt_version="system_v6")
    r3 = store.insert_run(conn, name="r3", kind="explore", provider="kakao",
                          source="backfill", started_at=NOW, header_json="{}",
                          prompt_version="system_v3")
    for i in range(20):                    # v3 전용 pano 20개가 먼저 깔린다
        store.upsert_pano(conn, f"O{i}", 37.500 + i * 1e-5, 127.0, now=NOW)
        store.insert_verdict(conn, run_id=r3, pano_id=f"O{i}", heading=0.0,
                             is_trail=False, created_at=NOW)
    store.upsert_pano(conn, "V6", 37.501, 127.0, now=NOW)
    store.insert_verdict(conn, run_id=r6, pano_id="V6", heading=0.0,
                         is_trail=True, nature_level=2, created_at=NOW)
    conn.commit()
    rows, truncated = store.viewport(conn, s=37.49, w=126.9, n=37.51, e=127.1,
                                     run_ids=[r6], limit=5)
    conn.close()
    assert [r["pano_id"] for r in rows] == ["V6"], "매칭 pano 가 빠졌다"
    assert not truncated


def test_모르는_버전은_404다(client):
    # 빈 지도(200)로 조용히 넘어가면 "이 지역엔 없다" 와 구분이 안 된다
    r = client.get("/api/panos", params={**BBOX, "prompt_version": "v6_typo"})
    assert r.status_code == 404
    assert "버전" in r.json()["detail"]
    # run_id 경로도 같은 가드를 타야 한다 — 존재 확인 없이 [run_id] 를
    # 돌려주면 정확히 같은 조용한 실패가 재현된다 (리뷰 지적)
    r = client.get("/api/panos", params={**BBOX, "run_id": 99999})
    assert r.status_code == 404


def test_정적_페이지가_뜬다(client):
    r = client.get("/")
    assert r.status_code == 200 and "trailwalk" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/map.js").status_code == 200

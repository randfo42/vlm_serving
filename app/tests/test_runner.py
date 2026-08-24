"""공개 경계층 — 예외 강등과 정리 보장 (docs/23 §9).

VLM 도 브라우저도 없이 돈다. 지키는 것:

- 결과는 실패해도 모양이 같다 — 어느 경로든 RunOutcome 의 같은 필드를 읽는다
- 예외가 밖으로 안 나온다. 단 원문이 warnings 에 **전문으로** 실린다
- 어느 경로로 끝나든 provider.close() 가 불린다 — 브라우저가 새는 자리
- 배선 실패(런이 서지 않음)는 run 행을 안 만든다. explore 도중 실패는 남는다
"""

from conftest import Client, Provider, nb
from trailwalk import runner, store
from trailwalk.prompt import PromptDriftError
from trailwalk.providers.base import ProviderError
from trailwalk.runner import RunRequest, run_explore

GRAPH = {"S": [nb("A", 90.0), nb("B", 270.0)],
         "A": [nb("S", 270.0)], "B": [nb("S", 90.0)]}
VERDICTS = {("S", 90.0): True, ("S", 270.0): False}


class TrackedProvider(Provider):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.closed = False

    def close(self):
        self.closed = True


class WebClient(Client):
    """runner 가 쓰는 두 가지(stats·warmup)를 conftest Client 에 보탠다."""

    def __init__(self, provider, verdicts):
        super().__init__(provider, verdicts)
        from trailwalk.vlm import Stats
        self.stats = Stats()

    def assess(self, uri, *, heading=None):
        self.stats.calls += 1
        return super().assess(uri, heading=heading)

    def warmup(self, uri):
        pass


def wire(monkeypatch, tmp_path, graph=GRAPH, verdicts=VERDICTS,
         client_cls=WebClient, warmup=False):
    """runner 의 배선 지점 두 곳(providers.make · VlmClient)만 갈아끼운다.

    warmup 을 기본으로 끄는 이유: 가짜 Provider.nearest 는 첫 호출에만 시작
    pano 를 준다 — warmup 이 그 한 번을 소비하면 explore 의 스냅이 어긋난다.
    """
    prov = TrackedProvider(graph)
    monkeypatch.setattr(runner.providers, "make",
                        lambda name, settings=None, **kw: prov)
    monkeypatch.setattr(runner, "VlmClient",
                        lambda **kw: client_cls(prov, dict(verdicts)))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"run:\n  warmup: {str(warmup).lower()}\n", encoding="utf-8")
    return prov, tmp_path / "t.db", str(cfg)


def test_성공_경로가_DB와_결과를_채운다(monkeypatch, tmp_path):
    prov, db, cfg = wire(monkeypatch, tmp_path)
    out = run_explore(RunRequest(start=(37.5, 127.0), config_path=cfg),
                      db=db, name="r1")
    assert out.ok and out.stop_reason == "exhausted"
    assert out.origin_pano == "S" and out.origin == (37.5, 127.0)
    assert out.verdicts == 2 and out.run_id is not None
    assert prov.closed, "성공 경로에서 provider 가 안 닫혔다"
    conn = store.connect(db, read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 2
    r = conn.execute("SELECT * FROM run").fetchone()
    assert r["stop_reason"] == "exhausted" and r["origin_pano"] == "S"
    assert r["finished_at"] is not None
    # 그래프도 실려야 한다 — "안 본 것" 과 "없는 것" 의 구분이 여기 산다
    assert conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 3
    conn.close()


def test_설정_실패는_run_행_없이_원문_전문으로_돌아온다(monkeypatch, tmp_path):
    _, db, _ = wire(monkeypatch, tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("run:\n  없는키: 1\n", encoding="utf-8")
    out = run_explore(RunRequest(start=(37.5, 127.0), config_path=str(bad)),
                      db=db)
    assert not out.ok and out.stop_reason == "settings_error"
    assert out.run_id is None, "런이 서지도 않았는데 run 행이 생겼다"
    w = next(w for w in out.warnings if w["code"] == "settings_error")
    assert "없는키" in w["message"], "예외 원문이 잘렸다 — 해결책이 사라진다"


def test_프롬프트_어긋남은_prompt_drift로_강등된다(monkeypatch, tmp_path):
    _prov, db, cfg = wire(monkeypatch, tmp_path)

    def boom(**kw):
        raise PromptDriftError("system_v6 sha 불일치\n프롬프트 파일을 되돌릴 것")

    monkeypatch.setattr(runner, "VlmClient", boom)
    out = run_explore(RunRequest(start=(37.5, 127.0), config_path=cfg), db=db)
    assert not out.ok and out.stop_reason == "prompt_drift"
    assert out.run_id is None
    assert "되돌릴 것" in out.warnings[0]["message"]


def test_provider_생성_실패도_모양이_같다(monkeypatch, tmp_path):
    _, db, cfg = wire(monkeypatch, tmp_path)

    def no_make(name, settings=None, **kw):
        raise ProviderError("앱키가 없다.\nKAKAO_JS_KEY 를 .env 에 추가할 것")

    monkeypatch.setattr(runner.providers, "make", no_make)
    out = run_explore(RunRequest(start=(37.5, 127.0), config_path=cfg), db=db)
    assert not out.ok and out.stop_reason == "provider_error"
    assert out.run_id is None
    assert "추가할 것" in out.warnings[0]["message"]


def test_explore_도중_ProviderError는_run_행에_남고_provider가_닫힌다(
        monkeypatch, tmp_path):
    prov, db, cfg = wire(monkeypatch, tmp_path)

    def boom(_pano):
        raise ProviderError("이웃 응답 파싱 실패 — 형식이 바뀐 것으로 보인다")

    prov.neighbors = boom
    out = run_explore(RunRequest(start=(37.5, 127.0), config_path=cfg),
                      db=db, name="r-pe")
    assert not out.ok and out.stop_reason == "provider_error"
    assert out.run_id is not None, "explore 까지 간 실패는 run 행이 남아야 한다"
    assert prov.closed
    conn = store.connect(db, read_only=True)
    assert conn.execute("SELECT stop_reason FROM run").fetchone()[0] == "provider_error"
    conn.close()


def test_모르는_예외는_internal_error와_트레이스백으로_남는다(monkeypatch, tmp_path):
    prov, db, cfg = wire(monkeypatch, tmp_path)

    class Broken(WebClient):
        def assess(self, uri, *, heading=None):
            raise KeyError("스키마에 없는 필드")

    monkeypatch.setattr(runner, "VlmClient", lambda **kw: Broken(prov, {}))
    out = run_explore(RunRequest(start=(37.5, 127.0), config_path=cfg),
                      db=db, name="r-ie")
    assert not out.ok and out.stop_reason == "internal_error"
    assert prov.closed, "버그 경로에서 provider 가 샜다"
    conn = store.connect(db, read_only=True)
    tb = conn.execute("SELECT payload_json FROM event WHERE kind='internal_error'"
                      ).fetchone()
    assert tb is not None and "KeyError" in tb[0], "트레이스백이 안 남았다"
    conn.close()


def test_취소는_실패가_아니다(monkeypatch, tmp_path):
    _prov, db, cfg = wire(monkeypatch, tmp_path)
    out = run_explore(RunRequest(start=(37.5, 127.0), config_path=cfg),
                      db=db, name="r-c", cancel=lambda: True)
    assert out.ok, "canceled 가 FATAL 로 취급됐다 — 부분 결과는 유효하다"
    assert out.stop_reason == "canceled"
    assert any(w["code"] == "canceled" for w in out.warnings)
    # 즉시 취소라 nodes 는 비어도 원점은 있어야 한다 — 지도가 그릴 원점
    assert out.origin_pano == "S"
    conn = store.connect(db, read_only=True)
    assert conn.execute("SELECT origin_pano FROM run").fetchone()[0] == "S"
    conn.close()


def test_요청의_반경과_시간이_설정을_덮는다(monkeypatch, tmp_path):
    # 사용자가 정하는 것은 "어디서, 몇 미터" 다. yaml 은 튜닝 기본값이다
    _prov, db, cfg = wire(monkeypatch, tmp_path)
    seen = {}
    real = runner.explore

    def spy(provider, client, start, bearing, cfg_, log, cancel=None):
        seen["radius"] = cfg_.max_distance_m
        seen["seconds"] = cfg_.max_seconds
        return real(provider, client, start, bearing, cfg_, log, cancel=cancel)

    monkeypatch.setattr(runner, "explore", spy)
    run_explore(RunRequest(start=(37.5, 127.0), config_path=cfg,
                           radius_m=123.0, max_seconds=45.0), db=db, name="r-o")
    assert seen == {"radius": 123.0, "seconds": 45.0}

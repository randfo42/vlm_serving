"""경고 채널 — 웹이 분기할 code 목록이 한곳이라는 것.

여기서 지키는 것은 문구가 아니라 **계약**이다: 모르는 code 는 못 나가고,
run_end 는 호출자가 뭘 넘기든 모은 경고를 싣는다.
"""
import json

import pytest

from trailwalk import warn
from trailwalk.runlog import RunLog


def _rows(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


# ── 표가 계약이다 ───────────────────────────────────────────────────────────

def test_모르는_code는_거부한다():
    """조용히 통과시키면 웹이 못 읽는 경고가 생긴다."""
    with pytest.raises(warn.UnknownWarning):
        warn.make("아무거나")


def test_문구에_필요한_값을_빠뜨리면_터진다():
    """조용히 원문을 내보내면 사용자 화면에 `{count}` 가 그대로 뜬다."""
    with pytest.raises(warn.UnknownWarning):
        warn.make("neighbors_missing")          # count 없음


def test_모든_code가_문구를_갖는다():
    """새 경고를 코드에만 추가하고 표에 안 넣는 것을 막는다."""
    assert all(isinstance(t, str) and t for t in warn.TEXT.values())


def test_경계층_강등_문구는_원문_전문을_담는다():
    """runner 가 예외를 stop_reason 으로 강등할 때 쓰는 code 들 (→ docs/23 §9).
    이 레포의 설정 예외는 여러 줄에 해결 방법을 적는다 — 첫 줄만 남기면
    증상만 남고 해결책이 사라지므로, 여러 줄이 그대로 실려야 한다."""
    multi = "REST 키만 있다.\nJS 키를 .env 에 추가할 것:\n  KAKAO_JS_KEY=..."
    w = warn.make("settings_error", error=multi)
    assert multi in w["message"], "원문이 잘렸다"
    w = warn.make("prompt_drift", error="sha 불일치\n프롬프트 파일을 되돌릴 것")
    assert "되돌릴 것" in w["message"]
    w = warn.make("internal_error", error="KeyError: 'x'")
    assert "KeyError" in w["message"]


def test_canceled는_판정_수를_요구한다():
    """"몇 건까지 하고 멈췄나" 없이는 부분 결과를 쓸지 판단할 수 없다."""
    assert "3건" in warn.make("canceled", verdicts=3)["message"]
    with pytest.raises(warn.UnknownWarning):
        warn.make("canceled")


def test_count는_최상위로_올린다():
    """웹이 detail 스키마를 몰라도 "몇 건인가" 는 읽을 수 있어야 한다."""
    w = warn.make("capture_failed", count=3, pano_id="P1")
    assert w["count"] == 3
    assert w["detail"] == {"pano_id": "P1"}
    assert "3건" in w["message"]


# ── 런로그 ─────────────────────────────────────────────────────────────────

def test_경고는_즉시_한_줄_나가고_run_end에도_실린다(tmp_path):
    """즉시 나가야 런이 중간에 죽어도 남고, run_end 에도 있어야 마지막 한 줄만
    읽는 소비자가 놓치지 않는다."""
    out = tmp_path / "r.jsonl"
    with RunLog(out, {}) as log:
        log.warn("no_coverage", radius_m=25.0)
        log.finish(stop_reason="no_coverage")
    rows = _rows(out)
    line = next(r for r in rows if r["type"] == "warning")
    assert line["code"] == "no_coverage" and "25m" in line["message"]
    end = next(r for r in rows if r["type"] == "run_end")
    assert [w["code"] for w in end["warnings"]] == ["no_coverage"]


def test_집계형은_한_줄로_모인다(tmp_path):
    """neighbors_missing 은 갈래마다 난다 — 실주행에서 22노드 중 12개까지
    나왔다. 한 건씩 올리면 진짜 신호가 묻힌다."""
    out = tmp_path / "r.jsonl"
    with RunLog(out, {}) as log:
        for pid in ("A", "B", "C"):
            log.tally("neighbors_missing", pano_id=pid)
        log.finish(stop_reason="exhausted")
    rows = _rows(out)
    assert not [r for r in rows if r["type"] == "warning"], "집계형이 매번 나갔다"
    end = next(r for r in rows if r["type"] == "run_end")
    assert end["warnings"] == [{"code": "neighbors_missing", "count": 3,
                                "message": end["warnings"][0]["message"],
                                "detail": {"pano_id": "C"}}]
    assert "3곳" in end["warnings"][0]["message"]


def test_finish는_호출자가_안_넘겨도_경고를_싣는다(tmp_path):
    """빠뜨릴 수 없어야 하는 계약이다 — 인자로 받으면 언젠가 빠진다."""
    out = tmp_path / "r.jsonl"
    with RunLog(out, {}) as log:
        log.warn("server_dead")
        log.finish(stop_reason="server_dead")
    end = next(r for r in _rows(out) if r["type"] == "run_end")
    assert end["stop_reason"] == "server_dead"
    assert [w["code"] for w in end["warnings"]] == ["server_dead"]


def test_경고가_없으면_빈_배열이다(tmp_path):
    """필드 자체가 없으면 소비자가 `.get("warnings", [])` 를 써야 한다 —
    있는 편이 계약이 명확하다."""
    out = tmp_path / "r.jsonl"
    with RunLog(out, {}) as log:
        log.finish(stop_reason="exhausted")
    end = next(r for r in _rows(out) if r["type"] == "run_end")
    assert end["warnings"] == []


def test_모르는_code는_tally_시점에_터진다(tmp_path):
    """finish 까지 미루면 런이 다 끝난 뒤에 터진다 — 그리고 finish 는 finally
    에서 불리므로 run_end 가 통째로 날아간다."""
    with RunLog(tmp_path / "r.jsonl", {}) as log, pytest.raises(warn.UnknownWarning):
        log.tally("아무거나")


def test_문구를_못_만들어도_run_end는_남는다(tmp_path):
    """stop_reason 은 항상 존재한다는 계약이 경고 하나 때문에 깨지면 안 된다."""
    out = tmp_path / "r.jsonl"
    with RunLog(out, {}) as log:
        log.tally("cache_miss")                 # calls 를 안 넘겼다
        log.finish(stop_reason="exhausted")
    end = next(r for r in _rows(out) if r["type"] == "run_end")
    assert end["stop_reason"] == "exhausted"
    w = end["warnings"][0]
    assert w["code"] == "cache_miss" and "만들지 못했다" in w["message"]


def test_이미_합산된_총계는_그대로_더한다(tmp_path):
    """⚠️ 무조건 +1 하면 client.stats 의 캐시 미스 30건이 런로그에 count:1 로
    남는다. stdout 은 따로 계산해 맞게 찍히므로 사람은 못 알아채고, JSONL 만
    읽는 소비자(웹)에게만 조용히 축소된 수치가 간다."""
    out = tmp_path / "r.jsonl"
    with RunLog(out, {}) as log:
        log.tally("cache_miss", count=30, calls=200)
        log.finish(stop_reason="exhausted")
    w = next(r for r in _rows(out) if r["type"] == "run_end")["warnings"][0]
    assert w["count"] == 30
    assert "30/200" in w["message"]

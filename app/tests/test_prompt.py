"""프롬프트 로딩과 핀.

### 무엇을 지키려는 테스트인가

1. **프리픽스 캐시.** system turn 이 1바이트만 달라져도 캐시 적중률이 0 이 된다.
   에러는 안 나고 그냥 느려진다 — 아무도 모르는 채로 런이 2배 느려진다.
2. **런로그의 진실성.** 어느 프롬프트로 난 결과인지 모르면 그 런은 쓸모가 없다.
   PINS 의 기존 항목이 바뀌면 과거 런로그의 fingerprint 가 통째로 거짓이 된다.

실제 사고: v1 이 "산책로가 보이는가" 인지 "위에 서 있는가" 인지 정하지 않아
같은 지점 판정이 갈렸다. v2 로 고쳤더니 진짜 산책로 4건을 거부했다. v3 가 현재.
셋을 다 남겨둔 이유가 여기 테스트로 굳어 있다.
"""
import hashlib

import pytest

from trailwalk import prompt as P


def test_모든_핀이_실제_파일과_일치한다():
    """핀이 틀어지면 조용히 캐시가 죽는 게 아니라 여기서 터져야 한다."""
    for version, want in P.PINS.items():
        raw = (P.PROMPT_DIR / f"{version}.txt").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == want, f"{version} 의 핀이 파일과 다르다"


def test_기본_버전은_핀에_있다():
    assert P.DEFAULT_VERSION in P.PINS


def test_모르는_버전은_거부한다():
    with pytest.raises(P.PromptDriftError) as e:
        P.load("system_v99")
    # 안내에 아는 버전 목록이 있어야 한다. 없으면 오타를 찾는 데 시간이 든다.
    assert "system_v1" in str(e.value)


def test_같은_버전은_매번_같은_객체다():
    """호출마다 파일을 다시 읽으면 런 도중 파일이 바뀌었을 때 조용히 갈라진다."""
    assert P.load("system_v1") is P.load("system_v1")


def test_버전마다_내용이_다르다():
    """세 버전이 실수로 같은 파일을 가리키면 비교 실험이 무의미해진다."""
    bodies = {v: P.load(v) for v in P.PINS}
    assert len(set(bodies.values())) == len(bodies)


def test_공백을_보존한다():
    """strip() 하나가 캐시를 죽인다. 파일 바이트 그대로여야 한다."""
    for version in P.PINS:
        raw = (P.PROMPT_DIR / f"{version}.txt").read_bytes().decode()
        assert P.load(version) == raw


def test_핀이_바뀌면_적재가_실패한다(monkeypatch):
    monkeypatch.setitem(P.PINS, "system_v1", "0" * 64)
    monkeypatch.setattr(P, "_cache", {})          # 캐시를 비워야 실제로 다시 읽는다
    with pytest.raises(P.PromptDriftError) as e:
        P.load("system_v1")
    msg = str(e.value)
    # 안내가 "다르다" 로 끝나면 사람이 다음에 뭘 해야 할지 모른다.
    assert "PINS" in msg and "새 버전 파일" in msg


# ── 출력 스키마 ────────────────────────────────────────────────────────────
# 출력 토큰 하나가 ~37ms 다. 스키마에 필드가 늘면 그대로 지연이 된다.

def test_walk_스키마는_필드가_하나뿐이다():
    """탐색 루프는 스텝마다 여러 번 부른다. 여기에 필드를 더하면 런 전체가 느려진다."""
    assert list(P.SCHEMAS["walk"]["properties"]) == ["is_trail"]


@pytest.mark.parametrize("name", sorted(P.SCHEMAS))
def test_스키마는_추가_필드를_막는다(name):
    """열어두면 모델이 제멋대로 필드를 붙이고, 그만큼 느려진다."""
    assert P.SCHEMAS[name]["additionalProperties"] is False


def test_스키마의_모든_필드가_프롬프트에_정의돼_있다():
    """⚠️ 실제 사고: confidence 를 스키마에만 넣고 프롬프트에서 설명하지 않았더니
    `is_trail=true` 인데 `confidence=0` 이 나왔다. 모델이 "산책로다움" 점수로
    해석한 것이다. **스키마는 형식을 강제할 뿐 의미를 주지 않는다.**
    """
    for version in P.PINS:
        body = P.load(version).lower()
        for name in P.SCHEMAS:
            for field in P.SCHEMAS[name]["properties"]:
                assert field.replace("_", " ") in body or field in body, \
                    f"{version} 이 {field!r} 를 정의하지 않았다"


# ── 이미지 뒤에 붙는 가변 텍스트 ────────────────────────────────────────────

def test_user_text에_좌표가_없다():
    """좌표가 새면 캐시가 죽고, 모델은 좌표로 할 수 있는 일도 없다."""
    txt = P.user_text(heading=91.36)
    assert "91" in txt                       # heading 은 남긴다
    assert "37." not in txt and "127." not in txt


def test_user_text의_heading은_0에서_359():
    assert "359 degrees" in P.user_text(heading=-1.0)
    assert "0 degrees" in P.user_text(heading=360.0)


def test_fingerprint가_버전을_따라간다():
    fp = P.fingerprint("system_v1")
    assert fp["system_version"] == "system_v1"
    assert fp["system_sha256"] == P.PINS["system_v1"]

"""런로그 — 특히 이미지 저장(`--save-images`).

이미지는 **기본적으로 안 남긴다** (지도 사업자 약관이 회색지대다 →
docs/23-open-questions.md §2). 그래서 "켜야만 저장된다" 는 것 자체가
테스트할 값어치가 있는 성질이다. 기본값이 조용히 뒤집히면 약관 판단이
깨지는데 증상이 안 보인다.
"""
import json

from trailwalk.runlog import RunLog
from trailwalk.vlm import Verdict


def _verdict(is_trail=True):
    return Verdict(is_trail=is_trail, confidence=None, prompt_tokens=300,
                   cached_tokens=66, completion_tokens=12, latency_ms=2200.0)


def _probe(log, **kw):
    log.probe(step=kw.pop("step", 0), pano_id=kw.pop("pano_id", "P1"),
              lat=37.5, lng=127.0, heading=kw.pop("heading", 90.0),
              verdict=kw.pop("verdict", _verdict()),
              src_format=kw.pop("src_format", "PNG"), **kw)


def _rows(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_기본은_이미지를_남기지_않는다(tmp_path):
    """약관 판단이 이 기본값에 걸려 있다 (§2)."""
    out = tmp_path / "r.jsonl"
    with RunLog(out, {}) as log:
        _probe(log, image=b"\x89PNG-fake")
    assert not list(tmp_path.glob("**/*.png"))
    probe = next(r for r in _rows(out) if r["type"] == "probe")
    assert "image" not in probe


def test_켜면_저장하고_런로그에서_가리킨다(tmp_path):
    out, imgs = tmp_path / "r.jsonl", tmp_path / "imgs"
    with RunLog(out, {}, image_dir=imgs) as log:
        _probe(log, image=b"\x89PNG-fake")
    probe = next(r for r in _rows(out) if r["type"] == "probe")
    assert probe["image"], "런로그가 파일명을 안 남기면 판정과 그림을 못 잇는다"
    assert (imgs / probe["image"]).read_bytes() == b"\x89PNG-fake"


def test_파일명이_호출순서_판정_방위를_담는다(tmp_path):
    """이름순 정렬이 곧 호출순이어야 판정을 따라가며 볼 수 있다."""
    out, imgs = tmp_path / "r.jsonl", tmp_path / "imgs"
    with RunLog(out, {}, image_dir=imgs) as log:
        _probe(log, heading=90.0, verdict=_verdict(True), image=b"a")
        _probe(log, heading=270.0, verdict=_verdict(False), image=b"b")
    names = sorted(p.name for p in imgs.glob("*.png"))
    assert names[0].startswith("001_") and names[1].startswith("002_")
    assert names[0].endswith("_T.png") and names[1].endswith("_F.png")
    assert "090.0" in names[0] and "270.0" in names[1]


def test_같은_pano_를_여러_방위로_찍어도_안_덮어쓴다(tmp_path):
    """방위마다 판정이 갈리는지 보는 것이 이 기능의 주 용도다 — 덮어쓰면 못 본다."""
    out, imgs = tmp_path / "r.jsonl", tmp_path / "imgs"
    with RunLog(out, {}, image_dir=imgs) as log:
        for h in (30.0, 90.0, 150.0):
            _probe(log, pano_id="SAME", heading=h, image=f"img{h}".encode())
    assert len(list(imgs.glob("*.png"))) == 3


def test_확장자는_주장이_아니라_감지된_포맷을_따른다(tmp_path):
    """fixture 는 JPEG 원본을 그대로 준다. .png 로 찍으면 이름과 바이트가
    어긋난 파일이 생긴다 — 이 레포가 경계하는 실패 유형 그대로다."""
    out, imgs = tmp_path / "r.jsonl", tmp_path / "imgs"
    with RunLog(out, {}, image_dir=imgs) as log:
        _probe(log, image=b"\xff\xd8-fake", src_format="JPEG")
    probe = next(r for r in _rows(out) if r["type"] == "probe")
    assert probe["image"].endswith(".jpg")


def test_이미지_없이_켜져_있어도_깨지지_않는다(tmp_path):
    """캡처가 실패해 raw 가 없을 수 있다. 그때도 런로그 줄은 남아야 한다."""
    out, imgs = tmp_path / "r.jsonl", tmp_path / "imgs"
    with RunLog(out, {}, image_dir=imgs) as log:
        _probe(log, image=None)
    probe = next(r for r in _rows(out) if r["type"] == "probe")
    assert "image" not in probe
    assert probe["is_trail"] is True


def test_런_헤더에_설정을_통째로_넣어도_직렬화된다(tmp_path):
    """⚠️ 실측 회귀. `vars(cfg)` 로 넣었더니 런이 **첫 줄에서** 죽었다.

    ExploreConfig.image 는 중첩 dataclass(ImageSettings)라 vars() 는 객체를
    그대로 담고, RunLog 는 헤더를 쓰는 순간 TypeError 를 낸다 — 즉 진입점이
    아예 안 떴다. 헤더는 "런로그만 보고 재현할 수 있어야 한다" 는 계약을
    지는 자리이므로, 설정을 통째로 담는 것 자체는 유지하고 asdict 로 넣는다.
    """
    from dataclasses import asdict

    from trailwalk import settings
    from trailwalk.explore import ExploreConfig

    cfg = ExploreConfig.from_settings(settings.load())
    out = tmp_path / "r.jsonl"
    with RunLog(out, {"config": asdict(cfg)}) as log:
        _probe(log)
    head = _rows(out)[0]
    assert head["config"]["image"]["target_size"] == list(cfg.image.target_size)

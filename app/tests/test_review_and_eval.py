"""검수 반영(apply_review)·runlog 라벨 필드·eval 재개의 불변식 — 오프라인.

검수는 사람이 파일을 옮기는 행위라 실수 여지가 크다 (엉뚱한 폴더, 이름 변경,
삭제). 집계가 그 실수를 조용히 넘기면 라벨 수가 어긋난 채 평가가 돈다.
"""
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from trailwalk.runlog import RunLog

APP = Path(__file__).resolve().parent.parent


def _load_apply(tmp_path: Path):
    """apply_review 를 임시 데이터셋에 바인딩해서 로드한다.

    경로는 monkeypatch 가 아니라 DatasetPaths 주입으로 받는다 — 모듈 상수를
    갈아끼우는 방식은 상수 하나가 늘 때마다 조용히 어긋난다.
    """
    spec = importlib.util.spec_from_file_location(
        "apply_review", APP / "labels" / "apply_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    paths = mod.ds.at("t", tmp_path)
    mod.main = lambda: mod.run(paths)
    return mod


def _make_sample(tmp_path: Path, sid: str, cid: str, label: bool, folder: str):
    sub = tmp_path / "images" / cid / folder
    sub.mkdir(parents=True, exist_ok=True)
    name = f"{sid}_12345_090.0_{'T' if label else 'F'}.png"
    (sub / name).write_bytes(b"png")
    return {"type": "sample", "sample_id": sid, "course_id": cid,
            "label": label, "label_source": {"p": "route", "o": "orth",
                                             "r": "rev", "x": "offroute"}[sid[-1]],
            "pano_id": "12345", "lat": 37.5, "lng": 127.0, "heading": 90.0,
            "image": f"{cid}/{folder}/{name}"}


def _write_samples(tmp_path: Path, rows):
    (tmp_path / "samples.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def _read_labels(tmp_path: Path):
    lines = [json.loads(x) for x
             in (tmp_path / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    return lines[0], {d["sample_id"]: d for d in lines[1:]}


def test_kept_flipped_discarded(tmp_path):
    rows = [
        _make_sample(tmp_path, "c-000p", "c", True, "pos"),      # 유지
        _make_sample(tmp_path, "c-001p", "c", True, "neg"),      # true→false 뒤집힘
        _make_sample(tmp_path, "c-001r", "c", False, "pos"),     # false→true 뒤집힘 (rev!)
        _make_sample(tmp_path, "c-002p", "c", True, "discard"),  # 폐기
    ]
    _write_samples(tmp_path, rows)
    mod = _load_apply(tmp_path)
    assert mod.main() == 0
    hdr, by_id = _read_labels(tmp_path)
    assert by_id["c-000p"]["review"] == "kept" and by_id["c-000p"]["final_label"] is True
    assert by_id["c-001p"]["review"] == "flipped" and by_id["c-001p"]["final_label"] is False
    assert by_id["c-001r"]["review"] == "flipped" and by_id["c-001r"]["final_label"] is True
    assert by_id["c-002p"]["review"] == "discarded" and by_id["c-002p"]["final_label"] is None
    assert hdr["stats"]["rev"]["flipped"] == 1     # rev 뒤집힘이 source 별로 집계된다


def test_missing_image_is_error(tmp_path):
    rows = [_make_sample(tmp_path, "c-000p", "c", True, "pos")]
    rows.append({**rows[0], "sample_id": "c-001p",
                 "image": "c/pos/c-001p_1_090.0_T.png"})   # 파일은 안 만든다
    _write_samples(tmp_path, rows)
    assert _load_apply(tmp_path).main() == 1


def test_unknown_file_is_error(tmp_path):
    rows = [_make_sample(tmp_path, "c-000p", "c", True, "pos")]
    _write_samples(tmp_path, rows)
    _make_sample(tmp_path, "c-999p", "c", True, "pos")     # 대장에 없는 파일
    assert _load_apply(tmp_path).main() == 1


def test_unknown_folder_fatal(tmp_path):
    rows = [_make_sample(tmp_path, "c-000p", "c", True, "maybe")]
    _write_samples(tmp_path, rows)
    with pytest.raises(SystemExit):
        _load_apply(tmp_path).main()


def test_renamed_file_fatal(tmp_path):
    rows = [_make_sample(tmp_path, "c-000p", "c", True, "pos")]
    _write_samples(tmp_path, rows)
    d = tmp_path / "images" / "c" / "pos"
    old = next(d.iterdir())
    old.rename(d / "renamed.png")
    with pytest.raises(SystemExit):
        _load_apply(tmp_path).main()


# ── runlog: label 필드 ───────────────────────────────────────────────────

@dataclass
class FakeVerdict:
    is_trail: bool = True
    camera_surface: str | None = None
    nature_level: int | None = None
    footway: int | None = None
    confidence: int | None = 9
    prompt_tokens: int = 276
    cached_tokens: int = 66
    completion_tokens: int = 17
    latency_ms: float = 2200.0


def _probe_line(tmp_path, **kw):
    out = tmp_path / "r.jsonl"
    with RunLog(out, {"h": 1}) as log:
        log.probe(step=1, pano_id="p", lat=37.5, lng=127.0, heading=90.0,
                  verdict=FakeVerdict(), src_format="PNG", **kw)
    return out.read_text(encoding="utf-8").splitlines()[1]


def test_probe_without_label_is_byte_identical(tmp_path):
    # label=None 이면 필드 자체가 없어야 한다 — walk/explore 런로그와 호환
    assert _probe_line(tmp_path) == _probe_line(tmp_path, label=None, sample_id=None)
    assert '"label"' not in _probe_line(tmp_path)


def test_probe_with_label(tmp_path):
    line = _probe_line(tmp_path, label=False, sample_id="c-000o")
    d = json.loads(line)
    assert d["label"] is False and d["sample_id"] == "c-000o"


def test_append_mode_no_duplicate_header(tmp_path):
    out = tmp_path / "r.jsonl"
    with RunLog(out, {"h": 1}):
        pass
    with RunLog(out, {"h": 1}, append=True) as log:
        log.probe(step=1, pano_id="p", lat=0, lng=0, heading=0,
                  verdict=FakeVerdict(), src_format="PNG")
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert sum(1 for d in lines if d["type"] == "run_start") == 1


# ── run_eval: 재개용 done_ids ────────────────────────────────────────────

def _load_run_eval():
    spec = importlib.util.spec_from_file_location("run_eval", APP / "run_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_done_ids_reads_only_probes_with_sample_id(tmp_path):
    mod = _load_run_eval()
    out = tmp_path / "e.jsonl"
    out.write_text("\n".join([
        json.dumps({"type": "run_start"}),
        json.dumps({"type": "probe", "sample_id": "a-000p"}),
        json.dumps({"type": "probe"}),                      # walk 런의 줄 — 무시
        json.dumps({"type": "event", "sample_id": "zzz"}),  # probe 아님 — 무시
    ]) + "\n")
    assert mod.done_ids(out) == {"a-000p"}


def test_done_ids_missing_file_empty(tmp_path):
    assert _load_run_eval().done_ids(tmp_path / "none.jsonl") == set()


def test_load_labels_excludes_discarded(tmp_path):
    mod = _load_run_eval()
    p = tmp_path / "labels.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "review_header"}),
        json.dumps({"type": "sample", "sample_id": "a", "final_label": True}),
        json.dumps({"type": "sample", "sample_id": "b", "final_label": None}),
        json.dumps({"type": "sample", "sample_id": "c", "final_label": False}),
    ]) + "\n")
    assert [s["sample_id"] for s in mod.load_labels(p)] == ["a", "c"]


def test_resume_conflict_detects_prompt_labels_url_change():
    mod = _load_run_eval()
    old = {"prompt": {"system_sha256": "aaa"}, "labels_sha256": "L1", "url": "u1"}
    same = dict(old)
    assert mod.resume_conflict(old, same) is None
    assert mod.resume_conflict(None, same) is None          # 새 파일 — 재개 아님
    assert "prompt" in mod.resume_conflict(
        old, {**old, "prompt": {"system_sha256": "bbb"}})
    assert "labels" in mod.resume_conflict(old, {**old, "labels_sha256": "L2"})
    # 다른 서버(url)의 probe 를 한 파일에 합산하지 않는다
    assert "url" in mod.resume_conflict(old, {**old, "url": "u2"})


def test_resume_header_reads_first_line(tmp_path):
    mod = _load_run_eval()
    out = tmp_path / "e.jsonl"
    out.write_text(json.dumps({"type": "run_start", "labels_sha256": "X"}) + "\n")
    assert mod.resume_header(out)["labels_sha256"] == "X"
    assert mod.resume_header(tmp_path / "none.jsonl") is None

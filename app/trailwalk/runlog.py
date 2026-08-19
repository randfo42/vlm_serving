"""런로그 — JSONL 한 줄 = VLM 호출 한 번.

나중에 답해야 할 질문을 먼저 정하고,
그 질문에 답할 수 있는 필드만 남긴다. 여기서 답해야 할 질문은 네 가지다.

  1. 어느 프롬프트/스키마로 난 결과인가        → 헤더의 prompt fingerprint
  2. 어느 장면이었나 (다시 찾아갈 수 있는가)    → pano_id, lat, lng, heading
  3. 모델이 뭐라 했나                          → is_trail, confidence
  4. 조용히 깨지고 있지는 않은가                → prompt_tokens, cached_tokens, latency

이미지 자체는 기본적으로 저장하지 않는다. 저장하려면 설정에서
`run.save_images: true` 를 켠다. 지도 사업자 약관상 이미지 캐싱이 회색지대이기
때문이다 → app/docs/23-open-questions.md §2. 켜면 `app/runs/images/<런이름>/` 에
쌓이고(gitignore), 각 probe 줄에 `image` 필드로 파일명이 남는다.
"""
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from . import warn as warn_mod


class RunLog:
    def __init__(self, path: Path, header: dict, image_dir: Path | None = None,
                 append: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 판정을 눈으로 감사할 때만 켠다. 기본은 끔 (약관 → §2)
        self.image_dir = Path(image_dir) if image_dir else None
        if self.image_dir:
            self.image_dir.mkdir(parents=True, exist_ok=True)
        self._n = 0
        # append 는 재개용(run_eval resume): run_start 를 다시 쓰지 않고 이어 쓴다.
        # 헤더가 기존 파일과 같은지는 호출자(재개 로직)가 확인할 일이다.
        self._f = self.path.open("a" if append else "w", encoding="utf-8")
        self._t0 = time.time()
        # 경고는 두 형태로 나간다 (→ trailwalk/warn.py):
        #   ① 즉시 {"type": "warning"} 한 줄 — 런이 중간에 죽어도 남고,
        #      probe 사이에 끼어 "언제" 일어났는지가 보인다
        #   ② run_end 의 warnings[] — 마지막 한 줄만 읽는 소비자(웹)를 위해.
        #      집계형(count)은 여기서만 완성된다
        self._warnings: list[dict] = []
        self._tallies: dict[str, dict] = {}
        if not append:
            self._write({"type": "run_start",
                         "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                         **header})

    def _write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._f.flush()   # 중간에 죽어도 거기까지는 남아야 한다

    def probe(self, *, step: int, pano_id: str, lat: float, lng: float,
              heading: float, verdict, src_format: str,
              image: bytes | None = None,
              label: bool | None = None, sample_id: str | None = None) -> None:
        self._n += 1
        name = None
        if self.image_dir and image:
            # 확장자는 주장이 아니라 감지된 실제 포맷(src_format)을 따른다.
            # kakao 는 PNG 스크린샷이지만 fixture 는 JPEG 원본을 그대로 준다 —
            # 전부 .png 로 찍으면 이름과 바이트가 어긋난 파일이 생긴다.
            ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(src_format, "bin")
            # 번호를 앞에 둔다 — 파일 이름순이 곧 호출 순서라 판정을 따라가며 볼 수 있다.
            name = f"{self._n:03d}_s{step:02d}_{pano_id}_{heading:05.1f}_" \
                   f"{'T' if verdict.is_trail else 'F'}.{ext}"
            (self.image_dir / name).write_bytes(image)
        self._write({
            "type": "probe", "step": step,
            "pano_id": pano_id, "lat": round(lat, 7), "lng": round(lng, 7),
            "heading": round(heading, 1),
            "is_trail": verdict.is_trail, "confidence": verdict.confidence,
            "prompt_tokens": verdict.prompt_tokens,
            "cached_tokens": verdict.cached_tokens,
            "completion_tokens": verdict.completion_tokens,
            "latency_ms": round(verdict.latency_ms, 1),
            "src_format": src_format,
            **({"image": name} if name else {}),
            # 평가 런에서만 존재한다. None 이면 필드 자체가 없어 기존 줄과
            # 바이트가 같다 — walk/explore 런로그와 스키마 호환.
            **({"label": label} if label is not None else {}),
            **({"sample_id": sample_id} if sample_id is not None else {}),
        })

    def event(self, kind: str, **kw) -> None:
        """디버깅용 자유형 로그. 웹이 읽는 계약이 아니다 — 그건 warn/tally 다."""
        self._write({"type": "event", "kind": kind, **kw})

    def warn(self, code: str, **detail) -> None:
        """1회성 경고. 즉시 한 줄 나가고 run_end 에도 실린다."""
        w = warn_mod.make(code, **detail)
        self._warnings.append(w)
        self._write({"type": "warning", **w})

    def tally(self, code: str, **detail) -> None:
        """집계형 경고. 같은 code 를 여러 번 불러 `count` 를 올린다.

        한 건씩 즉시 내보내지 않는 이유: neighbors_missing 은 갈래마다 나므로
        실주행에서 노드 22개 중 12개까지 나온다. 한 줄씩 올리면 진짜 신호가
        묻힌다. 대신 상세는 event 로 이미 남아 있다.

        `count=` 를 주면 그만큼 더한다. 호출자가 이미 합산해 들고 있는 것들
        (client.stats 의 캐시 미스 등)을 한 번에 넘기기 위한 것이고, 안 주면
        1이다 — 무조건 +1 하면 그런 총계가 조용히 1로 줄어든다.
        """
        # code 검증은 **여기서** 한다. finish() 까지 미루면 런이 다 끝난 뒤에
        # 터지고, finish 는 finally 에서 불리므로 run_end 가 통째로 날아간다.
        if code not in warn_mod.TEXT:
            raise warn_mod.UnknownWarning(
                f"모르는 경고 code: {code!r}. trailwalk/warn.py 의 TEXT 에 추가할 것")
        t = self._tallies.setdefault(code, {"count": 0})
        t["count"] += int(detail.get("count", 1))
        t.update({k: v for k, v in detail.items() if k != "count"})

    def _tallied(self) -> list[dict]:
        """집계형을 warning 형태로. **여기서 예외를 밖으로 내지 않는다.**

        문구에 필요한 필드가 빠졌는지는 값이 다 모인 지금에야 알 수 있는데,
        여기서 터지면 run_end 가 안 써진다 — finish 는 finally 에서 불리므로
        런 요약이 통째로 날아가고 원래 예외까지 가린다. 그래서 그 한 건만
        시끄러운 자리표시자로 바꾼다.
        """
        out = []
        for code, d in self._tallies.items():
            try:
                out.append(warn_mod.make(code, **d))
            except warn_mod.UnknownWarning as e:
                out.append({"code": code, "count": d["count"],
                            "message": f"({code}) 경고 문구를 만들지 못했다: {e}"})
        return out

    def finish(self, **summary) -> None:
        """run_end 한 줄. warnings 는 인자로 받지 않고 **모은 것을 넣는다** —
        호출자가 빠뜨릴 수 없어야 하는 계약이다."""
        warnings = self._warnings + self._tallied()
        self._write({"type": "run_end", "wall_s": round(time.time() - self._t0, 1),
                     **summary, "warnings": warnings})
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self._f.closed:
            self._f.close()

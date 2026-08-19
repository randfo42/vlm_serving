"""런로그 — JSONL 한 줄 = VLM 호출 한 번.

서빙 쪽 docs/06-logging.md 와 같은 원칙이다: 나중에 답해야 할 질문을 먼저 정하고,
그 질문에 답할 수 있는 필드만 남긴다. 여기서 답해야 할 질문은 네 가지다.

  1. 어느 프롬프트/스키마로 난 결과인가        → 헤더의 prompt fingerprint
  2. 어느 장면이었나 (다시 찾아갈 수 있는가)    → pano_id, lat, lng, heading
  3. 모델이 뭐라 했나                          → is_trail, confidence
  4. 조용히 깨지고 있지는 않은가                → prompt_tokens, cached_tokens, latency

이미지 자체는 기본적으로 저장하지 않는다. 저장하려면 --save-images 를 켠다.
지도 사업자 약관상 이미지 캐싱이 회색지대이기 때문이다 → docs/23-open-questions.md §2.
켜면 런로그 옆 `<런이름>-images/` 에 쌓이고, 각 probe 줄에 `image` 필드로 파일명이
남는다. 저장 위치는 `app/runs/images/` 아래라 gitignore 된다.
"""
import json
import time
from datetime import UTC, datetime
from pathlib import Path


class RunLog:
    def __init__(self, path: Path, header: dict, image_dir: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 판정을 눈으로 감사할 때만 켠다. 기본은 끔 (약관 → §2)
        self.image_dir = Path(image_dir) if image_dir else None
        if self.image_dir:
            self.image_dir.mkdir(parents=True, exist_ok=True)
        self._n = 0
        self._f = self.path.open("w", encoding="utf-8")
        self._t0 = time.time()
        self._write({"type": "run_start",
                     "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                     **header})

    def _write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._f.flush()   # 중간에 죽어도 거기까지는 남아야 한다

    def probe(self, *, step: int, pano_id: str, lat: float, lng: float,
              heading: float, verdict, src_format: str,
              image: bytes | None = None) -> None:
        self._n += 1
        name = None
        if self.image_dir and image:
            # 번호를 앞에 둔다 — 파일 이름순이 곧 호출 순서라 판정을 따라가며 볼 수 있다.
            name = f"{self._n:03d}_s{step:02d}_{pano_id}_{heading:05.1f}_" \
                   f"{'T' if verdict.is_trail else 'F'}.png"
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
        })

    def event(self, kind: str, **kw) -> None:
        self._write({"type": "event", "kind": kind, **kw})

    def finish(self, **summary) -> None:
        self._write({"type": "run_end", "wall_s": round(time.time() - self._t0, 1), **summary})
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self._f.closed:
            self._f.close()

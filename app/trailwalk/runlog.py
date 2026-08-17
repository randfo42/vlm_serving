"""런로그 — JSONL 한 줄 = VLM 호출 한 번.

서빙 쪽 docs/06-logging.md 와 같은 원칙이다: 나중에 답해야 할 질문을 먼저 정하고,
그 질문에 답할 수 있는 필드만 남긴다. 여기서 답해야 할 질문은 네 가지다.

  1. 어느 프롬프트/스키마로 난 결과인가        → 헤더의 prompt fingerprint
  2. 어느 장면이었나 (다시 찾아갈 수 있는가)    → pano_id, lat, lng, heading
  3. 모델이 뭐라 했나                          → is_trail, confidence
  4. 조용히 깨지고 있지는 않은가                → prompt_tokens, cached_tokens, latency

이미지 자체는 기본적으로 저장하지 않는다. 저장하려면 --save-images 를 켠다.
지도 사업자 약관상 이미지 캐싱이 회색지대이기 때문이다 → docs/23-open-questions.md §2.
"""
import json
import time
from datetime import UTC, datetime
from pathlib import Path


class RunLog:
    def __init__(self, path: Path, header: dict):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("w", encoding="utf-8")
        self._t0 = time.time()
        self._write({"type": "run_start",
                     "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                     **header})

    def _write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._f.flush()   # 중간에 죽어도 거기까지는 남아야 한다

    def probe(self, *, step: int, pano_id: str, lat: float, lng: float,
              heading: float, verdict, src_format: str) -> None:
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

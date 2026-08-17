"""벤치 스크립트 공용 경로. configs/env.sh 의 파이썬 판.

바이너리는 vendor/llama.cpp 의 고정 커밋 빌드를 쓴다. PATH 의 brew
llama-server 를 쓰면 `brew upgrade` 한 번에 docs/04-b1-results.md 의
수치가 조용히 무효가 된다. → docs/11-server-ops.md §2
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "llama.cpp"
LLAMA_BIN = Path(os.environ.get("LLAMA_BIN", VENDOR / "build" / "bin" / "llama-server"))

MODEL_DIR = ROOT / "models" / "gemma-4-E4B-qat"
MODEL = MODEL_DIR / "gemma-4-E4B_q4_0-it.gguf"
MMPROJ = MODEL_DIR / "gemma-4-E4B-it-mmproj.gguf"


def _pin():
    d = {}
    for line in (ROOT / "configs" / "llama.pin").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


def build_id():
    """결과 파일에 박을 빌드 식별자. 핀과 실제 HEAD 가 다르면 그 사실이 남는다."""
    pin = _pin()
    try:
        head = subprocess.check_output(
            ["git", "-C", str(VENDOR), "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        head = "unknown"
    tag, commit = pin.get("TAG", "?"), pin.get("COMMIT", "")
    return f"{tag}-{head}" if commit.startswith(head) else f"UNPINNED-{head}(pin={tag})"


def check():
    if not LLAMA_BIN.exists():
        raise SystemExit(f"llama-server 없음 — {LLAMA_BIN}\n  ./scripts/build-llama.sh 를 먼저 실행한다.")
    for f in (MODEL, MMPROJ):
        if not f.exists():
            raise SystemExit(f"모델 없음 — {f}")

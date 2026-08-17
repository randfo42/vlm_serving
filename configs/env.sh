# 공용 경로 해석. 실행하지 말고 source 한다.
#
#   source "$(dirname "$0")/env.sh"
#
# 내보내는 것: REPO_ROOT, LLAMA_BIN, MODEL, MMPROJ, LLAMA_TAG, LLAMA_COMMIT
#
# 왜 이 파일이 있나: 예전엔 brew 의 llama-server 를 PATH 에서 집어
# 썼다. brew 는 버전을 핀할 수 없어서 `brew upgrade` 한 번에
# docs/04-b1-results.md 의 모든 수치가 조용히 무효가 된다.
# 이제 vendor/ 안의 고정 커밋 빌드만 쓴다. → docs/11-server-ops.md §2

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT

# shellcheck source=./llama.pin
source "$REPO_ROOT/configs/llama.pin"
export LLAMA_TAG="$TAG" LLAMA_COMMIT="$COMMIT"

export LLAMA_BIN="${LLAMA_BIN:-$REPO_ROOT/vendor/llama.cpp/build/bin/llama-server}"

MODEL_DIR="$REPO_ROOT/models/gemma-4-E4B-qat"
export MODEL="${MODEL:-$MODEL_DIR/gemma-4-E4B_q4_0-it.gguf}"
export MMPROJ="${MMPROJ:-$MODEL_DIR/gemma-4-E4B-it-mmproj.gguf}"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "FATAL: llama-server 없음 — $LLAMA_BIN" >&2
  echo "  ./scripts/build-llama.sh 를 먼저 실행한다." >&2
  exit 1
fi

# 빌드된 소스가 핀과 다르면 경고한다. 막지는 않는다 (일부러 다른
# 커밋을 시험하는 경우가 있다). 다만 벤치 결과에 이 사실이
# 남지 않으면 나중에 수치를 신뢰할 수 없다.
_head="$(git -C "$REPO_ROOT/vendor/llama.cpp" rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ "$_head" != "$LLAMA_COMMIT" ]]; then
  echo "WARN: vendor/llama.cpp HEAD=$_head 가 핀($LLAMA_COMMIT)과 다름" >&2
  export LLAMA_COMMIT="$_head"
fi

for f in "$MODEL" "$MMPROJ"; do
  [[ -f "$f" ]] || { echo "FATAL: 모델 없음 — $f" >&2; exit 1; }
done

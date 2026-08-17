#!/usr/bin/env bash
# llama.cpp 를 configs/llama.pin 에 고정된 커밋으로 체크아웃하고 빌드한다.
#
# 멱등하다. 이미 같은 커밋으로 빌드돼 있으면 cmake 증분 빌드만 돈다.
# 커밋을 바꾸려면 configs/llama.pin 을 수정하고 다시 실행한다.
#
#   ./scripts/build-llama.sh          # 핀 대로 빌드
#   ./scripts/build-llama.sh --clean  # build/ 지우고 처음부터
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/vendor/llama.cpp"
BUILD="$SRC/build"
REPO=https://github.com/ggml-org/llama.cpp.git

# shellcheck source=../configs/llama.pin
source "$ROOT/configs/llama.pin"

[[ "${1:-}" == "--clean" ]] && rm -rf "$BUILD"

# ── 1. 소스 확보 ────────────────────────────────────────────
# --depth 1 --branch <tag> 로 태그 스냅샷만 받는다 (전체 히스토리 1GB+).
if [[ ! -d "$SRC/.git" ]]; then
  echo "==> clone $TAG"
  git clone --depth 1 --branch "$TAG" "$REPO" "$SRC"
fi

HAVE="$(git -C "$SRC" rev-parse HEAD)"
if [[ "$HAVE" != "$COMMIT" ]]; then
  echo "==> fetch $TAG ($COMMIT)"
  git -C "$SRC" fetch --depth 1 origin "$COMMIT" || git -C "$SRC" fetch --depth 1 origin "tag" "$TAG"
  git -C "$SRC" checkout --detach "$COMMIT"
fi

# 핀과 실제 체크아웃이 다르면 여기서 멈춘다. 측정치와 바이너리가
# 어긋난 채로 벤치를 돌리는 게 최악이다.
ACTUAL="$(git -C "$SRC" rev-parse HEAD)"
if [[ "$ACTUAL" != "$COMMIT" ]]; then
  echo "FATAL: checkout $ACTUAL != pin $COMMIT" >&2
  exit 1
fi

# ── 2. 빌드 ────────────────────────────────────────────────
# GGML_METAL_EMBED_LIBRARY=ON
#   Metal 셰이더를 바이너리에 박는다. 안 하면 실행 시 default.metallib 를
#   cwd 기준으로 찾다가 조용히 CPU 폴백한다 (느려지지만 에러는 안 남).
# LLAMA_CURL=OFF
#   -hf 원격 모델 다운로드와 원격 이미지 URL 을 끈다. 우리는 로컬
#   GGUF + base64 data URI 만 쓴다 (docs/10-client-guide.md §2.2).
#   의존성을 줄이고, 서버가 외부로 나가는 경로를 아예 없앤다.
# LLAMA_BUILD_NUMBER 를 태그에서 직접 준다.
#   build-info.cmake 는 `git rev-list --count HEAD` 로 빌드 번호를 만드는데
#   shallow clone 은 커밋이 1개뿐이라 항상 "build 1" 이 된다.
#   그러면 시작 배너와 /props 가 b10450 이 아니라 1 을 보고해서
#   운영 중 버전 확인(docs/11-server-ops.md §4)이 무의미해진다.
BUILD_NUMBER="${TAG#b}"

echo "==> configure"
cmake -S "$SRC" -B "$BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_BUILD_NUMBER="$BUILD_NUMBER" \
  -DGGML_METAL=ON \
  -DGGML_METAL_EMBED_LIBRARY=ON \
  -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  >/dev/null

echo "==> build ($(sysctl -n hw.ncpu) jobs)"
cmake --build "$BUILD" --config Release -j "$(sysctl -n hw.ncpu)" --target llama-server llama-mtmd-cli

BIN="$BUILD/bin/llama-server"
[[ -x "$BIN" ]] || { echo "FATAL: $BIN not built" >&2; exit 1; }

echo
echo "==> ok  $TAG ($ACTUAL)"
"$BIN" --version 2>&1 | head -3
echo "    $BIN"

#!/usr/bin/env bash
# B1 스모크 테스트 — "모델이 뜨는가"만 확인하는 최소 설정.
# 성능 프로파일이 아니다. 벤치는 별도 config로.
#
# 검증 대상: docs/02-open-questions.md §0 의 B1~B4
#
# 인자는 llama-server 로 그대로 넘어간다:  ./configs/smoke.sh -lv 10
set -euo pipefail

# LLAMA_BIN / MODEL / MMPROJ 를 해석하고 존재를 검증한다.
# 바이너리는 vendor/llama.cpp 의 고정 커밋 빌드다 (PATH 의 brew 가 아니다).
source "$(dirname "$0")/env.sh"

# ── 토큰 예산 ────────────────────────────────────────────────
# 280 = 모델 기본값 (config.json vision_soft_tokens_per_image).
# 기술보고서상 장면판단 태스크는 1120→280에서 거의 무손실
# (MMMU Pro -1.2, MATH-Vision -0.3). 밀집 텍스트 OCR만 크게 하락.
#
# min 은 1 로 둔다 (max 와 같게 두지 않는다).
#   min=max 로 두면 리사이즈 목표보다 작은 이미지를 강제 업스케일해
#   토큰 수를 채운다. 정보량은 안 늘고 비용만 정상가가 되어
#   "클라이언트가 작은 이미지를 보냈다"는 사실이 은폐된다.
#   min=1 이면 image_tokens 가 급감해 게이트웨이가 탐지할 수 있다.
#   정상 크기 입력에는 아무 차이가 없다. → docs/11-server-ops.md §3.3
IMAGE_TOKENS=280

# ── !!! 하드 크래시 방지 !!! ─────────────────────────────────
# 비전 인코더는 non-causal attention이라 이미지 토큰 전체가
# 단일 ubatch에 들어가야 한다. -ub < 이미지 토큰이면
#   GGML_ASSERT "non-causal attention requires n_ubatch >= n_tokens" → SIGABRT
# 사전 검증 없음 (issue #21461, #21550 둘 다 "not planned").
#
# 주의: 예산은 상한일 뿐, 실제 토큰 수는 종횡비에 따라 달라진다.
#       예산 280 + 4:3 이미지 → 실제 266토큰 (912x672로 리사이즈).
#       따라서 예산보다 넉넉히 잡아야 한다.
#
# 실측(docs/05-observability.md §5): -ub 를 이미지 토큰 바로 위로 낮추면
#   2048 → 320 에서 prefill 1669ms → 1508ms (처리량 +7%). 공짜 이득.
UBATCH=320

echo "llama.cpp $LLAMA_TAG ($LLAMA_COMMIT)" >&2
echo "$LLAMA_BIN" >&2

"$LLAMA_BIN" \
  --model   "$MODEL" \
  --mmproj  "$MMPROJ" \
  --jinja \
  -ngl 99 \
  --ctx-size 8192 \
  --parallel 1 \
  --ubatch-size "$UBATCH" \
  --batch-size  "$UBATCH" \
  --image-min-tokens 1 \
  --image-max-tokens "$IMAGE_TOKENS" \
  --cache-ram 0 \
  --reasoning off \
  --reasoning-budget 0 \
  --metrics \
  "$@" \
  --host 127.0.0.1 \
  --port 8080

# 의도적으로 설정한 값들:
#
# --parallel 1   SWA KV 캐시가 슬롯 수만큼 startup에 사전 할당된다.
#                stateless 1요청 워크로드라 슬롯이 필요 없다.
#                동시성 스케일링 측정(§1c)에서만 올린다.
#
# --cache-ram 0  기본값이 8192 MiB로 켜져 있다. stateless 워크로드엔
#                무용하고, 이미지 프롬프트 캐싱이 메모리를 먹는
#                알려진 이슈가 있다 (#22629, Linux 확인/macOS 미확인).
#                프리픽스 캐시 측정(§3)에서만 켠다.
#
# --swa-full     쓰지 않음. 8K에서 0.25GB → 0.77GB로 0.5GB밖에
#                차이나지 않아 비용은 싸지만, B1은 "뜨는가"만 본다.
#                프리픽스 캐시 측정(§3)에서 켜고 비교한다.
#
# -fa            지정하지 않음 → 기본 'auto'. Metal에서 -ctk/-ctv를
#                섞으면 실패하는 이슈가 있어(#21450) KV 양자화는
#                하지 않는다. KV가 0.25GB뿐이라 아낄 이유도 없다.

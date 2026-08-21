# Gemma4 E-시리즈를 6GB GPU에서 서빙하기 — vLLM(1660) vs llama.cpp(M4) 실측

대상: 소형 GPU에 gemma4 멀티모달을 올리려는 서빙 담당자
관련: `11-server-ops.md`(운영), `03-vision-encoding-constraint.md`(비전 제약)
측정일: 2026-08-21

> 요지: **gemma-4 E-시리즈(PLE/MatFormer)는 vLLM 문서가 "PLE swapping 미지원"이라 명시할 만큼 6GB
> 카드에 안 올라가는 모델이다.** PLE 임베딩 테이블(E2B 4.7GB / E4B 5.64GB)을 CPU로 빼는 vLLM 소스
> 패치로 GTX 1660 SUPER(6GB, 2019)에서 **E2B와 E4B 둘 다** 실제 서빙에 성공했다. 다만 **같은 E4B를
> Apple M4 + llama.cpp가 별도 패치 없이 ~3배 빠르게** 돌린다. 결론은 아래 §6.

---

## 1. TL;DR

| | **vLLM @ 1660 (E2B)** | **vLLM @ 1660 (E4B)** | **llama.cpp @ M4 (E4B)** |
|---|---|---|---|
| 하드웨어 | Turing 6GB VRAM (2019) + host 15GB RAM | 〃 | M4 16GB 통합메모리 (2024) |
| 모델 | gemma-4-E2B, bnb int4 | gemma-4-E4B, bnb int4 | gemma-4-E4B-qat, q4_0 GGUF |
| 텍스트 생성 | **12.7 tok/s** | **9.2 tok/s** | **27.6 tok/s** |
| 이미지 생성 | **9.7 tok/s** | **8.2 tok/s** | **26.5 tok/s** |
| 로드 시간 | ~231초 | ~540초 (다운로드 16GB 포함) | 즉시(서버 상주) |
| GPU weights / KV 여유 | 2.34 / 2.42 GiB | 4.0+ / 1.04 GiB | (Metal 통합, mmap) |
| 되게 하는 데 든 것 | **vLLM 소스 패치 + 스왑** | 〃 | 없음 (QAT GGUF 그대로) |
| 정답 품질 | 정확 | 정확 | 정확 |

**같은 모델(E4B)로 직접 비교하면:** 1660+vLLM 9.2/8.2 tok/s vs M4+llama.cpp 27.6/26.5 tok/s —
**M4가 ~3배 빠르다.** 단 하드웨어 세대 차(2019 예산형 GPU vs 2024 플래그십 SoC)가 크므로 이는
"엔진 우열"이 아니라 "이 두 조합의 실측"이다. **1660에서 E2B·E4B 둘 다 뜬다**는 것 자체가 핵심.

---

## 2. 벤치마크 — 같은 프롬프트

### 텍스트: "What is the capital of France? Answer in one sentence."

| 엔진 | 출력 | 디코딩 |
|---|---|---|
| vLLM @1660 (E2B) | `The capital of France is Paris.` | 8 tok @ 12.7 tok/s |
| vLLM @1660 (E4B) | `The capital of France is Paris.` | 8 tok @ 9.2 tok/s |
| llama.cpp @M4 (E4B) | `The capital of France is Paris.` | 8 tok @ 27.6 tok/s |

### 이미지: "Describe this image in one sentence." + 합성 테스트 이미지

테스트 이미지(합성, `assets/13-test-house.png` — 빨간 집·갈색 지붕·초록 들판·파란 하늘·노란 해):

![test image](assets/13-test-house.png)

| 엔진 | 출력 | 디코딩 |
|---|---|---|
| vLLM @1660 (E2B) | `A simple, stylized image depicts a red house with a brown roof set against a light blue sky and a green field with a yellow sun.` | 29 tok @ 9.7 tok/s |
| vLLM @1660 (E4B) | `A simple, stylized illustration depicts a red house with a brown roof against a light blue sky featuring a bright yellow sun, set against a green landscape.` | 31 tok @ 8.2 tok/s |
| llama.cpp @M4 (E4B) | `A simple, blocky red house sits on a green lawn under a bright yellow sun in a light blue sky.` | 24 tok @ 26.5 tok/s |

세 경우 모두 이미지를 정확히 묘사한다. **소형 하드웨어에서도 gemma4 멀티모달 판정 품질 자체는
살아있다** — 병목은 품질이 아니라 속도와, 올리기까지의 노력이다.

---

## 3. 왜 안 들어가는가 — PLE가 범인

gemma-4 E2B는 "유효 2B"지만 MatFormer라 **실제 저장 파라미터 5.10B = fp16 10.21GB**. 6GB에 절대 안 맞는다.
단일 최대 덩어리는 **레이어별 임베딩 테이블(PLE, `embed_tokens_per_layer`) = 2.35B = fp16 4.70GB**.

| 컴포넌트 | fp16 | int4 |
|---|--:|--:|
| **PLE (embed_tokens_per_layer)** | **4.70 GB** | 1.40 GB |
| 트랜스포머 레이어 | 3.67 GB | 0.92 GB |
| 오디오 인코더 | 0.61 GB | 0.15 GB |
| 비전 인코더 | 0.34 GB | 0.08 GB |
| 일반 입력 임베딩 등 | 0.89 GB | — |
| **합계** | **10.21 GB** | ~2.6 GB |

**설계 의도상 PLE는 VRAM이 아니라 플래시/CPU에 두고 필요한 행만 조회하는 물건**이다(폰용 설계).
그런데 vLLM은 이 설계를 구현하지 않는다 — `gemma4.py`는 PLE를 일반 `VocabParallelEmbedding`으로
GPU에 통째로 올린다. 그래서 6GB OOM은 "설계상 필연"이고, vLLM 문서도 못 박아 두었다:

> "There's no PLE caching or out-of-memory swapping support, as described in Google's blog."
> — vLLM supported-models 문서(gemma3n 주석)

**bitsandbytes로도 안 된다:** bnb는 `nn.Linear`만 양자화하고 임베딩은 fp16으로 둔다. 즉 4.70GB PLE가
그대로 남아 OOM.

---

## 4. 1660에서 vLLM으로 되게 한 레시피 (재현용)

PLE를 CPU로 빼는 것이 핵심. vLLM(0.27.1)의 UVA 오프로더는 파라미터를 **핀 메모리에 두고
zero-copy로 GPU가 필요한 행만 읽게** 할 수 있는데, `make_layers`가 디코더 레이어만 오프로더에
넘기고 최상위 PLE는 안 넘긴다. 그래서 소스를 직접 패치한다.

벗겨낸 벽 4개(각각 독립 문제였다):

| # | 벽 | 원인 | 해결 |
|---|---|---|---|
| 1 | **VRAM 6GB** | PLE 4.7GB가 GPU 점유 | `gemma4.py` — PLE를 처음부터 CPU에 생성 + UVA 뷰 |
| 2 | **RAM 부족** | 핀메모리 4.7GB + 체크포인트 9.5GB > 15GB | 스왑 16GB 추가 |
| 3 | **로딩 shape 불일치** | full-attn 레이어 head_dim=512인데 config에 `global_head_dim` 속성 없음 → 256 폴백 | `hf_overrides.text_config.global_head_dim = 512` 주입 |
| 4 | **Turing 공유메모리 64KB** | 어텐션 커널이 head_dim=512에서 96KB 요구 (Ampere+ 전용) | Triton 커널 — pre-Ampere+head512에서 KV TILE=16 |

### 4.1 패치 — `vllm/model_executor/models/gemma4.py`

PLE(`embed_tokens_per_layer`) 생성부(약 999행)를 CPU 생성 + UVA로 교체. 처음부터 CPU에 만들어야
GPU 생성 피크(6GB 카드에서 아슬아슬)를 아예 없앤다:

```python
# PLE(~4.7GB)를 처음부터 CPU에 생성해 GPU 피크를 없앤 뒤 핀메모리 + zero-copy UVA 뷰로.
import torch as _torch
from vllm.utils.platform_utils import is_uva_available as _uva
from vllm.utils.torch_utils import get_accelerator_view_from_cpu_tensor as _uva_view
_ple_on_cpu = _uva()
_ctx = _torch.device("cpu") if _ple_on_cpu else _torch.device("cuda")
with _ctx:
    self.embed_tokens_per_layer = VocabParallelEmbedding(
        self.vocab_size_per_layer_input, total_ple_dim,
        quant_config=quant_config, prefix=f"{prefix}.embed_tokens_per_layer")
if _ple_on_cpu:
    _w = self.embed_tokens_per_layer.weight
    _cpu = _w.data.to("cpu").pin_memory()
    _w.data = _uva_view(_cpu)
    _w._vllm_is_uva_offloaded = True
```

### 4.2 패치 — `vllm/v1/attention/ops/triton_unified_attention.py`

TILE 계산(`if use_td:` 블록 직후)에 pre-Ampere 가드 삽입. head_size=512에서 KV 타일을 16으로:

```python
if head_size >= 512:
    import torch as _torch
    if _torch.cuda.is_available() and _torch.cuda.get_device_capability()[0] < 8:
        TILE_SIZE_PREFILL = min(TILE_SIZE_PREFILL, 16)
        TILE_SIZE_DECODE = min(TILE_SIZE_DECODE, 16)
```

### 4.3 실행 인자

```python
LLM(model="google/gemma-4-E2B-it", dtype="float16", quantization="bitsandbytes",
    max_model_len=2048, gpu_memory_utilization=0.90, max_num_seqs=1,
    enforce_eager=True, limit_mm_per_prompt={"image": 1}, trust_remote_code=True,
    hf_overrides={
        "allow_global_per_layer_attribute_access": True,   # get_head_size(KV 사이징)용
        "text_config": {"allow_global_per_layer_attribute_access": True,
                        "global_head_dim": 512},           # full-attn head_dim
        "vision_config": {"allow_global_per_layer_attribute_access": True}})
```
환경: `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. 프롬프트는 반드시 chat 템플릿(`llm.chat`)으로
줄 것 — raw 문자열은 즉시 EOS로 빈 출력이 난다.

### 4.4 로드 후 GPU 배치 (6GB 안)

```
weights(bnb int4 등)   2.34 GiB
peak activation        0.29 GiB
KV 캐시 풀             2.42 GiB (139,353 토큰, 동시성 68배)
─────────────────────────────
PLE 4.70 GB            → CPU 핀메모리 (UVA zero-copy), GPU엔 0
```

### 4.5 E4B도 같은 패치로 뜬다 (더 빡빡)

`model="google/gemma-4-E4B-it"`, `gpu_memory_utilization=0.95`로 올리면 **패치 변경 없이** 뜬다.
E4B는 PLE가 5.64GB(→CPU), 비-PLE weights가 ~4GB라 KV 여유가 **1.04GiB**로 줄어든다(E2B 2.42GiB).
단일 스트림엔 충분하지만 동시성 여유는 거의 없다. 로드는 다운로드(16GB) 포함 ~540초.
결과: 텍스트 9.2 / 이미지 8.2 tok/s (§2). **6GB 카드가 E-시리즈 상한을 E4B까지 소화한다.**

**주의(재현성):** 이 패치들은 원격 GPU 박스의 vLLM 설치본을 직접 고친 것이다(각 파일 `.bak` 백업 존재).
upstream이 아니며 vLLM 버전 올리면 다시 적용해야 한다. vLLM 문서가 "미지원"이라 적어둔 지점을 뚫은
것이라 upstream 이슈/PR 거리이기도 하다.

---

## 5. llama.cpp @ M4 쪽 설정 (비교 기준)

이미 앱 프로덕션에서 도는 서버가 그대로 기준이 된다:

```
llama-server \
  --model  models/gemma-4-E4B-qat/gemma-4-E4B_q4_0-it.gguf   \  # 4.8GB
  --mmproj models/gemma-4-E4B-qat/gemma-4-E4B-it-mmproj.gguf  \  # 946MB (멀티모달)
  --jinja -ngl 99 --ctx-size 8192 --parallel 1 --host 127.0.0.1 --port 8080
```

- **QAT q4_0 GGUF는 임베딩까지 4bit로 양자화**한다 → PLE 문제가 애초에 없다(전체 4.8GB).
- `-ngl 99`로 전 레이어가 Metal(통합메모리)에 올라간다. 모델은 mmap이라 실제 상주 메모리 부담이 낮다
  (측정 phys_footprint ~1.3GB, 나머지는 페이지캐시로 재활용 가능).
- 별도 패치·양자화 작업 없이 GGUF 하나로 끝. head_dim 이질성·어텐션 커널 문제도 llama.cpp가 내부에서
  알아서 처리한다.

---

## 6. 결론 / 권장

1. **소형 GPU에서 gemma4 E-시리즈를 굳이 vLLM으로 올릴 이유는 약하다.** E2B·E4B 둘 다 되긴 되지만
   (위 레시피), 소스 패치 4곳 + 스왑 + 231~540초 로드 + Turing 저효율 어텐션의 대가를 치른다.
2. **같은 목적이면 llama.cpp + QAT GGUF가 정답에 가깝다.** 동일 E4B로 직접 비교 시 M4+llama.cpp가
   1660+vLLM보다 ~3배 빠르고(27.6 vs 9.2 tok/s), 패치도 0이다. PLE를 임베딩까지 양자화해 문제를
   처음부터 안 만든다 — 폰/노트북용 설계(PLE off-VRAM)를 그대로 구현한 것.
3. **vLLM의 가치는 이 케이스가 아니다.** vLLM은 큰 GPU에서 동시성·처리량(연속 배칭, paged KV)으로
   값한다. 6GB 단일 카드 + 저동시성에서는 그 강점이 안 산다.
4. **판정 품질은 하드웨어를 안 탄다** — E2B/6GB에서도 이미지 묘사가 정확했다. 즉 소형 장비는
   "품질"이 아니라 "throughput/운영 편의"에서만 손해다.

**한 줄:** 6GB에 gemma4를 올리는 것은 vLLM 소스를 뚫으면 가능하다는 것을 확인했고(연구 가치 있음),
운영 선택지로는 llama.cpp가 낫다.

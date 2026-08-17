#!/usr/bin/env python
"""토큰 예산 스윕 — docs/02-open-questions.md §1, §2 측정.

서버를 예산별로 재기동하며 구간 분해(t_prefill / t_decode)를 잰다.
캐시 오염을 피하려고 매 요청 서로 다른 이미지를 쓴다.
"""
import base64
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

from _paths import LLAMA_BIN, MMPROJ, MODEL, ROOT, build_id, check

IMAGES = sorted((ROOT / "bench" / "images").glob("gen_*.jpg"))
URL = "http://127.0.0.1:8080"

# ⚠️ 바이트 고정. 줄바꿈을 바꾸면 문자열이 바뀌고 예전 벤치 수치와 비교가 끊긴다.
# 그래서 E501 을 고치지 않고 예외로 둔다 — 재배치가 곧 데이터 손상이다.
SYSTEM = ("You are a walking-trail assessor. Given a single street-level photo, judge whether the "
          "scene is a pleasant walking trail suitable for a leisurely stroll. Consider: presence of a "  # noqa: E501
          "walkable path, greenery, separation from vehicle traffic, and overall pedestrian comfort. "  # noqa: E501
          "Answer only with the requested JSON. Be decisive.")

SCHEMAS = {
    "tiny": {"type": "object", "properties": {"is_trail": {"type": "boolean"}},
             "required": ["is_trail"], "additionalProperties": False},
    "min":  {"type": "object", "properties": {
                "is_trail": {"type": "boolean"}, "confidence": {"type": "number"},
                "surface": {"type": "string",
                            "enum": ["paved", "dirt", "gravel", "mixed", "unknown"]}},
             "required": ["is_trail", "confidence", "surface"], "additionalProperties": False},
}


def start_server(budget, ubatch=2048):
    # 여기서만 --image-min-tokens = budget 으로 둔다 (운영은 min=1).
    # 예산별 토큰 수를 정확히 고정해야 스윕 축이 깨끗해지기 때문이다.
    # 운영 설정과 다르다는 점 주의. → docs/11-server-ops.md §3.3
    # 이 핸들은 Popen 의 stdout 이라 with 로 감쌀 수 없다 — 블록을 나가며 닫히면
    # 서버 로그가 통째로 사라진다. 프로세스가 끝날 때 같이 닫힌다.
    log = open(f"/tmp/llama_sweep_{budget}.log", "w")  # noqa: SIM115
    p = subprocess.Popen([
        str(LLAMA_BIN), "--model", str(MODEL), "--mmproj", str(MMPROJ), "--jinja",
        "-ngl", "99", "--ctx-size", "8192", "--parallel", "1",
        "--ubatch-size", str(ubatch), "--batch-size", str(ubatch),
        "--image-min-tokens", str(budget), "--image-max-tokens", str(budget),
        "--cache-ram", "0", "--reasoning", "off", "--reasoning-budget", "0",
        "--host", "127.0.0.1", "--port", "8080",
    ], stdout=log, stderr=subprocess.STDOUT)
    for _ in range(180):
        if p.poll() is not None:
            raise RuntimeError(f"server died (budget={budget}, ub={ubatch}) — "
                               f"see /tmp/llama_sweep_{budget}.log")
        try:
            urllib.request.urlopen(f"{URL}/health", timeout=1).read()
            return p
        except Exception:
            time.sleep(1)
    p.kill()
    raise RuntimeError("server did not become healthy")


def ask(img_path, schema):
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    body = {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": "Assess this scene."}]}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "t", "schema": schema, "strict": True}},
        "max_tokens": 200, "temperature": 0}
    r = urllib.request.urlopen(urllib.request.Request(
        f"{URL}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}), timeout=300)
    return json.load(r)


def main():
    check()
    budgets = [int(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1
                                else [70, 140, 280, 560, 1120])]
    schema_name = sys.argv[2] if len(sys.argv) > 2 else "min"
    schema = SCHEMAS[schema_name]
    build = build_id()
    print(f"build={build}  schema={schema_name}  images={len(IMAGES)}\n")
    hdr = (f"{'budget':>7} {'prompt_tok':>11} {'prefill_ms':>11} {'out_tok':>8} "
           f"{'decode_ms':>10} {'total_s':>8} {'img/s':>7}")
    print(hdr); print("-" * len(hdr))
    results = []
    for b in budgets:
        srv = start_server(b)
        try:
            ask(IMAGES[0], schema)  # warmup, discarded
            rows = []
            for img in IMAGES[1:]:
                d = ask(img, schema)
                t = d["timings"]
                rows.append((t["prompt_n"], t["prompt_ms"], t["predicted_n"], t["predicted_ms"]))
            pn = statistics.median(r[0] for r in rows)
            pm = statistics.median(r[1] for r in rows)
            dn = statistics.median(r[2] for r in rows)
            dm = statistics.median(r[3] for r in rows)
            tot = (pm + dm) / 1000
            print(f"{b:>7} {pn:>11.0f} {pm:>11.0f} {dn:>8.0f} {dm:>10.0f} "
                  f"{tot:>8.2f} {1/tot:>7.2f}")
            results.append(dict(budget=b, prompt_tok=pn, prefill_ms=pm, out_tok=dn, decode_ms=dm))
        finally:
            srv.terminate(); srv.wait(timeout=30)
            time.sleep(2)
    out = ROOT / "bench" / "results"; out.mkdir(exist_ok=True)
    # build 를 같이 남긴다. 어떤 llama.cpp 로 잰 수치인지 모르는 결과는 쓸모없다.
    (out / f"sweep_{schema_name}.json").write_text(
        json.dumps({"build": build, "schema": schema_name, "rows": results}, indent=2))
    print(f"\nwrote {out / f'sweep_{schema_name}.json'}")


if __name__ == "__main__":
    main()

"""공개 경계층 — 웹·워커·CLI 가 explore 를 부르는 유일한 배선 (docs/23 §9).

이게 없을 때의 문제: provider·VlmClient·기록 계층을 만들고 잇는 코드가
run_explore.py(CLI) 안에만 있어서, 웹을 붙이는 순간 같은 배선을 또 쓰게
되고 — 두 곳이 되는 순간 갈라진다. §9 가 경계에 요구한 것 셋을 여기서 진다:

  1. **배선을 감춘다.** 호출자가 정하는 것은 "어디서, 몇 미터" 둘뿐이다.
     설정 파일은 튜닝 기본값이지 사용자 인터페이스가 아니다.
  2. **예외를 밖으로 내보내지 않는다.** 단 조용히 삼키지도 않는다 — 원문
     전문을 warnings 의 detail/message 로 나르고, 트레이스백은 event 로
     DB 에 남는다. 이 레포의 사고는 전부 에러 없이 일어났다.
  3. **결과는 실패해도 모양이 같다.** RunOutcome 의 같은 필드를 분기 없이
     읽으면 된다. stop_reason 은 닫힌 집합이다.

어느 경로로 끝나든 provider 는 닫힌다 — Playwright 브라우저가 안 닫히면
샌다. ExitStack 에 make 직후 등록하는 것이 그 보장이다.
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
import time
import traceback
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from . import prompt as P
from . import providers, settings, store
from . import warn as warn_mod
from .explore import ExploreConfig, explore
from .imaging import view_to_data_uri
from .providers.base import ProviderError
from .settings import SettingsError
from .vlm import VlmClient

APP = Path(__file__).resolve().parent.parent

# 이 stop_reason 이면 결과를 완결로 읽으면 안 된다. CLI 는 exit 2, 워커는
# failed 로 접는다. canceled 는 여기 없다 — 부분 결과는 유효하다.
FATAL = {"image_ignored", "server_dead", "vlm_error", "provider_error",
         "no_coverage", "settings_error", "prompt_drift", "internal_error"}

# provider 렌더 품질 신호 중 결과 품질에 영향을 줘서 경고로도 올릴 것
WARN_KINDS = {"tiles_timeout", "render_unsettled"}


@dataclass(frozen=True)
class RunRequest:
    """사용자가 정하는 것 전부. 나머지는 설정 파일(튜닝 기본값)이다."""
    start: tuple[float, float]
    bearing: float = 0.0
    radius_m: float | None = None       # None = 설정값
    max_seconds: float | None = None
    config_path: str | None = None      # None = 정본 trailwalk.yaml
    save_images: bool | None = None


@dataclass(frozen=True)
class RunOutcome:
    """실패해도 모양이 같다 — 호출자는 분기 없이 같은 필드를 읽는다."""
    ok: bool                            # stop_reason not in FATAL
    stop_reason: str
    warnings: list[dict] = field(repr=False, default_factory=list)
    run_id: int | None = None           # 배선 실패(런이 서지 않음)면 None
    origin: tuple[float, float] | None = None    # 스냅된 시작점. 못 스냅했으면 None
    origin_pano: str | None = None
    nodes: int = 0
    verdicts: int = 0
    calls: int = 0
    skipped: int = 0
    frontier: int = 0
    wall_s: float = 0.0
    # ExploreResult 원본. CLI 의 dump(plot_explore 입력)와 frontier 목록
    # 출력용이다 — 웹·워커는 이걸 읽지 않고 DB 를 읽는다. 배선 실패면 None
    result: object | None = field(repr=False, default=None)


def run_explore(req: RunRequest, *, db: Path | str, name: str | None = None,
                cancel: Callable[[], bool] | None = None) -> RunOutcome:
    """탐색 1건. 배선 → explore → 기록 → 정리까지 전부.

    배선 단계에서 실패하면(설정·프롬프트·provider 생성) 런이 서지 않은
    것이므로 run 행을 만들지 않는다 — run_id=None 이 그 표시다. 원문은
    warnings 로 나간다. explore 도중의 실패는 run 행에 남는다.
    """
    t0 = time.time()
    collected: list[dict] = []     # runner 가 직접 모은 경고 (res.warnings 밖)

    def collect(code: str, **detail) -> None:
        """같은 code 는 count 를 합쳐 한 건으로. RunLog 시절 CLI 의 로컬
        tally 와 같은 규칙 — stdout 과 DB 가 같은 수를 보게 한다."""
        n = int(detail.get("count", 1))
        for w in collected:
            if w["code"] == code:
                merged = {**detail, "count": w.get("count", 0) + n}
                w.clear()
                w.update(warn_mod.make(code, **merged))
                return
        collected.append(warn_mod.make(code, **{**detail, "count": n}))

    def prefail(code: str, error: str) -> RunOutcome:
        collected.append(warn_mod.make(code, error=error))
        return RunOutcome(ok=False, stop_reason=code, warnings=collected,
                          wall_s=time.time() - t0)

    # ── 배선. 여기의 예외는 §9 강등 표 그대로 stop_reason 이 된다 ──────────
    try:
        st = settings.load(req.config_path)
        if req.radius_m is not None:
            st = replace(st, budget=replace(st.budget, max_distance_m=float(req.radius_m)))
        if req.max_seconds is not None:
            st = replace(st, budget=replace(st.budget, max_seconds=float(req.max_seconds)))
        if req.save_images is not None:
            st = replace(st, run=replace(st.run, save_images=bool(req.save_images)))
        cfg = ExploreConfig.from_settings(st)
        client = VlmClient(url=st.vlm.url, schema_name=st.vlm.schema,
                           system_version=st.vlm.prompt_version, settings=st)
    except SettingsError as e:
        return prefail("settings_error", str(e))
    except P.PromptDriftError as e:
        return prefail("prompt_drift", str(e))

    run_name = name or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{st.run.provider}-explore"
    header = {
        "provider": st.run.provider, "mode": "explore", "schema": st.vlm.schema,
        "url": st.vlm.url, "start": list(req.start), "start_bearing": req.bearing,
        "config": asdict(cfg),
        "config_path": str(Path(req.config_path).resolve() if req.config_path
                           else settings.DEFAULT_PATH),
        "prompt": P.fingerprint(st.vlm.prompt_version),
        # is_trail 을 유도한 경계. run 행에 남아야 임계를 옮겼을 때 옛 런을
        # 다시 해석할 수 있다 (store 의 run 컬럼 주석)
        "min_nature_level": st.vlm.min_nature_level,
        "require_footway": st.vlm.require_footway,
        "trail_surfaces": st.vlm.trail_surfaces,
    }

    res = None
    stop = None
    run_id = None
    with ExitStack() as stack:
        conn = store.connect(db)
        store.migrate(conn)
        stack.callback(conn.close)     # 맨 먼저 등록 = 맨 나중에 닫힘 —
        #                                provider 정리 이벤트까지 기록해야 한다
        try:
            prov = providers.make(st.run.provider, settings=st)
        except (ProviderError, RuntimeError) as e:
            return prefail("provider_error", str(e))
        # make **직후**, RunWriter 보다 먼저 등록한다 — RunWriter 가 던져도
        # (아래 이름 중복 등) 브라우저는 닫혀야 한다. writer 는 지연 바인딩:
        # 아직 없으면 close 만 하고, 있으면 close 실패를 event 로 남긴다
        writer = None
        stack.callback(lambda: _safe_close(prov, writer))
        try:
            writer = store.RunWriter(
                conn, header, name=run_name,
                image_dir=(APP / "runs" / "images" / run_name)
                if st.run.save_images else None)
        except sqlite3.IntegrityError:
            # run.name 은 UNIQUE 다 (백필 멱등성의 축). 설정이 고정 이름을
            # 들고 있으면 재실행에서 여기로 온다 — 판정을 덮어쓰지 않는 것이
            # 원칙이므로(판정 불변) 거부가 맞고, 고치는 법을 말해 준다
            return prefail("settings_error",
                           f"run 이름 {run_name!r} 가 이미 DB 에 있다. "
                           f"판정은 덮어쓰지 않는다 — run.out 을 바꾸거나 "
                           f"비워 둘 것 (비우면 시각으로 이름을 만든다)")
        run_id = writer.run_id

        if hasattr(prov, "on_event"):
            # provider 쪽 신호도 기록하되, 결과 품질에 영향을 주는 것은
            # 경고로도 올린다 — 루프는 provider 내부를 모른다
            def on_event(kind, **kw):
                writer.event(kind, **kw)
                if kind in WARN_KINDS:
                    writer.tally(kind)
                    collect(kind)
            prov.on_event = on_event

        try:
            if st.run.warmup:
                pano = prov.nearest(req.start[0], req.start[1], cfg.snap_radius_m)
                if pano:
                    uri, _ = view_to_data_uri(prov.capture(pano, req.bearing))
                    t = time.perf_counter()
                    client.warmup(uri)
                    writer.event("warmup",
                                 ms=round((time.perf_counter() - t) * 1000, 1))
            res = explore(prov, client, req.start, req.bearing, cfg, writer,
                          cancel=cancel)
            stop = res.stop_reason
        except ProviderError as e:
            # explore 는 이름 붙은 provider 실패를 의도적으로 위로 던진다
            # (형식 변경 등 — 원인 규명이 이미 문구에 들어 있다)
            collected.append(warn_mod.make("provider_error", error=str(e)))
            writer.warn("provider_error", error=str(e))
            stop = "provider_error"
        except Exception as e:
            # 여기 오면 버그다. 조용히 삼키면 같은 함정을 하나 더 파는 것 —
            # 트레이스백 전문을 DB 에 남기고 사유로도 구분한다
            writer.event("internal_error", traceback=traceback.format_exc())
            msg = f"{type(e).__name__}: {e}"
            collected.append(warn_mod.make("internal_error", error=msg))
            writer.warn("internal_error", error=msg)
            stop = "internal_error"
        finally:
            s = client.stats
            if s.cache_misses:
                writer.tally("cache_miss", count=s.cache_misses, calls=s.calls)
                collect("cache_miss", count=s.cache_misses, calls=s.calls)
            if s.parse_failures:
                writer.tally("parse_failure", count=s.parse_failures)
                collect("parse_failure", count=s.parse_failures)
            if res is not None and stop == "canceled":
                w = warn_mod.make("canceled", verdicts=res.calls)
                writer.warn("canceled", verdicts=res.calls)
                collected.append(w)
            writer.finish(
                stop_reason=stop or "internal_error",
                nodes=len(res.nodes) if res else 0,
                skipped=res.skipped if res else 0,
                frontier=len(res.frontier) if res else 0,
                calls=s.calls, retries=s.retries,
                cache_misses=s.cache_misses, parse_failures=s.parse_failures,
                mean_latency_ms=round(s.total_ms / s.calls, 1) if s.calls else None)
            if res is not None:
                store.write_result(conn, run_id, res)

    return RunOutcome(
        ok=stop not in FATAL, stop_reason=stop,
        warnings=(list(res.warnings) if res else []) + collected,
        run_id=run_id,
        origin=res.origin if res else None,
        origin_pano=res.origin_pano if res else None,
        nodes=len(res.nodes) if res else 0,
        verdicts=len(res.probes) if res else 0,
        calls=res.calls if res else 0,
        skipped=res.skipped if res else 0,
        frontier=len(res.frontier) if res else 0,
        wall_s=time.time() - t0,
        result=res)


def _safe_close(prov, writer) -> None:
    try:
        prov.close()
    except Exception as e:
        # close 실패가 런 결과를 가리면 안 된다. 하지만 조용히도 안 된다 —
        # 브라우저가 새고 있다는 신호다. 기록(DB)이 최선, stderr 가 차선이고,
        # 어느 쪽으로도 조용히 사라지지는 않는다
        msg = f"{type(e).__name__}: {e}"
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.event("provider_close_failed", error=msg)
                return
        print(f"⚠  provider close 실패 — 브라우저가 샜을 수 있다: {msg}",
              file=sys.stderr)

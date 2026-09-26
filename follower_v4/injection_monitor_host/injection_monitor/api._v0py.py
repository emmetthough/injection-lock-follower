"""HTTP API.

Three structural changes from injection_monitor_v2.py:

* **Endpoints are ``async def``.**  The old synchronous endpoints called
  ``wait_for_operation_clear()``, which polls with ``time.sleep`` inside
  Starlette's threadpool.  Forty stuck requests wedge every route including
  ``/status`` -- the one you would reach for to diagnose it.  Awaiting yields
  the worker instead, and ``HC`` (tens of seconds) never holds one at all
  because it returns a request id immediately.

* **Route order is explicit.**  ``/holdoff/calibrate`` must be declared before
  ``/holdoff/{value}`` or FastAPI matches the converter first and 422s.

* **Unsupported features return 501, not silence.**  Against pre-v4 firmware
  the old code would enqueue ``HC`` and report ``queued``; the command would be
  discarded by the Arduino and nothing would ever happen.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import interlock as interlock_mod
from .config import Settings, describe, load
from .link import CapabilityError, SerialLink
from .logging_setup import setup_logging
from .params import SPEC_BY_NAME, ParameterStore, ValidationError
from .state import EventLog, RunningData, WarningRing, jsonable

log = logging.getLogger(__name__)


class Service:
    """Everything with a lifetime, assembled in one place."""

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.store = ParameterStore(cfg.params_file)
        self.store.load()
        self.data = RunningData(cfg.trace_samples)
        self.warnings = WarningRing(cfg.warning_ring_size)
        self.events = EventLog(cfg.events_file)
        self.link = SerialLink(
            cfg, self.store,
            on_block=self._on_block,
            on_tagged=self._on_tagged,
            on_event=self._on_event,
            on_untagged=self._on_untagged,
        )
        self.interlock = interlock_mod.Interlock(cfg, self.link)
        #: Long-running commands the client can poll for.
        self.operations: dict[str, dict] = {}
        self.started = time.time()

    # -- link callbacks (called on the reader thread) --

    def _on_block(self, key: str, value: Any) -> None:
        self.data.set(key, value)
        for op in self.operations.values():
            if op["status"] == "running" and op.get("expects") == key:
                op.update(status="done", finished=time.time(), result=jsonable(value))

    def _on_tagged(self, t) -> None:
        self.warnings.add(t.tag, t.level, t.text, t.sticky)
        if t.tag == "[DEBUG]":
            self.events.note_debug_line(t.text)
        elif t.tag == "[WARN]":
            log.warning("firmware: %s", t.text)
            self.events.record("warning", {"text": t.text})

    def _on_untagged(self, text: str) -> None:
        """Several firmware event lines carry no [DEBUG] tag at all."""
        self.events.note_debug_line(text)

    def _on_event(self, kind: str, data: dict) -> None:
        self.events.record(kind, data)

    # -- helpers --

    def new_operation(self, command: str, expects: str | None,
                      timeout: float) -> dict:
        op_id = uuid.uuid4().hex[:12]
        op = {
            "id": op_id, "command": command, "expects": expects,
            "status": "running", "started": time.time(),
            "deadline": time.time() + timeout, "result": None,
        }
        self.operations[op_id] = op
        # Bound the table; these are diagnostic, not durable.
        if len(self.operations) > 64:
            for k in sorted(self.operations,
                            key=lambda k: self.operations[k]["started"])[:16]:
                self.operations.pop(k, None)
        return op

    def expire_operations(self) -> None:
        now = time.time()
        for op in self.operations.values():
            if op["status"] == "running" and now > op["deadline"]:
                op.update(status="timed_out", finished=now)


svc: Service | None = None


async def wait_idle(timeout: float) -> bool:
    """Yield until the reader is not consuming a block.

    Async polling rather than a threadpool sleep: the coroutine is suspended,
    so a slow block cannot consume a worker.
    """
    deadline = time.monotonic() + timeout
    while svc.link.busy.is_set():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def enqueue(command: str, capability: str | None = None, **kw) -> dict:
    """Common path for fire-and-forget commands."""
    try:
        accepted, detail = svc.link.send(command, capability=capability, **kw)
    except CapabilityError as e:
        raise HTTPException(
            status_code=501,
            detail={
                "error": str(e),
                "capability": e.capability,
                "hint": "This firmware predates the feature. Flash the v4 "
                        "firmware, or check GET /capabilities.",
            },
        )
    if not accepted and detail == "serial link down":
        raise HTTPException(status_code=503, detail={"error": detail})
    return {"status": "queued" if accepted else "coalesced",
            "command": command, "id": detail}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Replaces the deprecated @app.on_event handlers.

    Shutdown now actually cleans up: the old version called ``ser.close()`` but
    never ``GPIO.cleanup()``, and enqueued ``Z`` onto a queue whose reader was
    about to stop, so the command was usually never sent.
    """
    global svc
    cfg = load()
    setup_logging(cfg)
    svc = Service(cfg)
    log.info("starting with %s", describe(cfg))
    svc.link.start()
    svc.interlock.start()
    # Give the capability probe a chance to finish before serving requests, so
    # the first /state a freshly-loaded dashboard fetches is not provisional.
    # Not fatal if it times out: the port may simply be unplugged, and the
    # supervisor will keep retrying in the background.
    if not await asyncio.get_running_loop().run_in_executor(
        None, svc.link.wait_ready, 6.0
    ):
        log.warning("serial link not ready after 6s; serving with provisional "
                    "capabilities")
    try:
        yield
    finally:
        log.info("shutting down")
        try:
            # Zero the DACs synchronously, before the reader stops, and give it
            # a moment to actually go out on the wire.
            svc.link.send("Z")
            await asyncio.sleep(0.3)
        except Exception:
            log.exception("failed to send Z on shutdown")
        svc.interlock.stop()
        svc.link.stop()
        svc.store.save()
        log.info("shutdown complete")


app = FastAPI(title="Injection lock monitor", version="3.0.0", lifespan=lifespan)


# ==========================================================================
# State: what Dash calls on page load
# ==========================================================================

@app.get("/state")
async def get_state():
    """Everything needed to rehydrate the UI in one request.

    This is the fix for the refresh problem.  The browser stops owning any of
    these values -- on load it reads them from here, so a refresh restores the
    displayed state instead of blanking it.
    """
    svc.expire_operations()
    caps = svc.link.caps.tags()
    return JSONResponse(content=jsonable({
        **svc.store.snapshot(caps),
        "ready": svc.link.ready.is_set(),
        "capabilities": svc.link.caps.as_dict(),
        "link": svc.link.status(),
        "holdoff": svc.data.get("holdoff_status"),
        "backoff": svc.data.get("backoff_status"),
        "stats": svc.data.get("stats"),
        "interlock": svc.interlock.status(),
        "faults": svc.warnings.sticky(),
        "data_ages_s": svc.data.ages(),
        "uptime_s": round(time.time() - svc.started, 1),
    }))


@app.get("/capabilities")
async def get_capabilities():
    return JSONResponse(content=svc.link.caps.as_dict())


@app.get("/params")
async def get_params():
    return JSONResponse(content=jsonable(
        svc.store.snapshot(svc.link.caps.tags())
    ))


@app.post("/params/sync")
async def sync_params():
    """Push every stored parameter to the firmware.

    The manual escape hatch for when automatic reset detection did not fire --
    a board that rebooted before it was ever calibrated, for instance, leaves
    no trace the heuristic can see.
    """
    n = svc.link.replay_parameters("manual sync")
    return JSONResponse(content={"status": "sent", "count": n})


@app.post("/params/reset")
async def reset_params():
    """Restore every parameter to its firmware default, and push."""
    for spec in SPEC_BY_NAME.values():
        svc.store.set(spec.name, spec.default, source="default")
    n = svc.link.replay_parameters("reset to defaults")
    return JSONResponse(content={"status": "reset", "count": n})


#: NOTE: /params/sync and /params/reset are declared ABOVE this converter
#: route on purpose. FastAPI matches in declaration order, so with this route
#: first, POST /params/sync binds name="sync" and 404s as an unknown
#: parameter -- the same trap as /holdoff/calibrate.
@app.post("/params/{name}")
async def set_param(name: str, value: float = Body(..., embed=True)):
    """Validate, store, enqueue.  Returns immediately.

    ``pending`` is true until the firmware echoes the value back in a status
    block.  Only 8 of the 25 parameters appear in any block, so waiting for
    confirmation would hang on the other 17.
    The store records what was sent; the UI can show unconfirmed values muted.
    """
    spec = SPEC_BY_NAME.get(name)
    if spec is None:
        raise HTTPException(404, detail={"error": f"unknown parameter {name!r}"})
    if spec.capability not in svc.link.caps.tags():
        raise HTTPException(501, detail={
            "error": f"{name} needs firmware capability {spec.capability!r}",
            "capability": spec.capability,
        })
    try:
        coerced, warnings = svc.store.set(name, value)
    except ValidationError as e:
        raise HTTPException(422, detail={"error": str(e), "parameter": name})

    result = enqueue(f"C{name},{_wire(coerced, spec)}")
    return JSONResponse(content=jsonable({
        **result,
        "parameter": name,
        "value": coerced,
        "pending": True,
        "confirmable": spec.confirmable,
        "warnings": warnings,
    }))


def _wire(value: float, spec) -> str:
    from .params import _fmt
    return _fmt(value, spec)


# ==========================================================================
# Holdoff drift servo
#
# Literal paths MUST precede /holdoff/{value}: FastAPI matches in declaration
# order, so a converter declared first would swallow "calibrate" and 422.
# ==========================================================================

@app.post("/holdoff/calibrate")
async def holdoff_calibrate():
    """Long-running.  Returns 202 and an id; poll /operations/{id}.

    Calibration takes tens of seconds.  Holding the connection open for it is
    what would wedge the threadpool under the old design, and a client timeout
    mid-calibration would leave the operator with no idea whether it ran.
    """
    result = enqueue("HC", capability="holdoff", coalesce_key="HC",
                     expects="holdoff_calibration")
    op = svc.new_operation("HC", "holdoff_calibration", svc.cfg.timeout_calibration)
    return JSONResponse(status_code=202, content={
        **result, "operation_id": op["id"],
        "poll": f"/operations/{op['id']}",
        "expected_duration_s": "tens of seconds",
    })


@app.post("/holdoff/toggle")
async def holdoff_toggle():
    slope = svc.store.value("holdoff_slope")
    result = enqueue("HF", capability="holdoff", expects="holdoff_status")
    if not slope:
        result["warning"] = (
            "holdoff_slope is 0: the servo is uncalibrated and will not "
            "actuate. Run POST /holdoff/calibrate first."
        )
    return JSONResponse(content=result)


@app.get("/holdoff/status")
async def holdoff_status(refresh: bool = Query(True)):
    if refresh:
        try:
            svc.link.send("HR", capability="holdoff", coalesce_key="HR")
        except CapabilityError:
            pass
        await wait_idle(2.0)
    return JSONResponse(content=jsonable({
        "status": svc.data.get("holdoff_status"),
        "age_s": svc.data.age("holdoff_status"),
        "last_calibration": svc.data.get("holdoff_calibration"),
    }))


@app.post("/holdoff/set/{us}")
async def holdoff_set(us: int):
    """Set the holdoff without re-initialising.  The automation-safe route."""
    if us < 0:
        raise HTTPException(422, detail={"error": "holdoff must be >= 0"})
    return JSONResponse(content=enqueue(f"HS{us}", capability="holdoff",
                                        expects="holdoff_status"))


@app.post("/holdoff/set_and_init/{us}")
async def holdoff_set_and_init(us: int):
    """``D<us>``: sets the holdoff **and** re-initialises.

    ``D`` runs initialize_peak_vals_locations() -- 130 scans -- and resets
    refHeight/refStd.  Anything automated must use /holdoff/set instead, or it
    will reset the setpoint on every adjustment.  Keep this bound to a manual
    "set holdoff and re-init" control only.
    """
    if us < 0:
        raise HTTPException(422, detail={"error": "holdoff must be >= 0"})
    return JSONResponse(content={
        **enqueue(f"D{us}", expects="initialization"),
        "note": "This also re-initialises and resets refHeight/refStd.",
    })


@app.post("/holdoff/{value}", deprecated=True)
async def holdoff_legacy(value: int):
    """Deprecated alias for the old ``POST /holdoff/{value}``.

    Declared last so the literal routes above win.  Kept so an un-updated Dash
    page keeps working, but it sends ``D`` and therefore re-initialises.
    """
    resp = await holdoff_set_and_init(value)
    body = resp.body.decode()
    log.warning("deprecated POST /holdoff/%d used; this sends D and resets the "
                "setpoint. Use /holdoff/set/%d for automation.", value, value)
    return JSONResponse(content={
        **jsonable(__import__("json").loads(body)),
        "deprecated": True,
        "replacement": f"/holdoff/set/{value}",
    })


# ==========================================================================
# Instability back-off
# ==========================================================================

@app.get("/backoff/status")
async def backoff_status(refresh: bool = Query(True)):
    if refresh:
        try:
            svc.link.send("BR", capability="backoff", coalesce_key="BR")
        except CapabilityError:
            pass
        await wait_idle(2.0)
    return JSONResponse(content=jsonable({
        "status": svc.data.get("backoff_status"),
        "age_s": svc.data.age("backoff_status"),
    }))


@app.post("/backoff/reset")
async def backoff_reset():
    return JSONResponse(content=enqueue("BZ", capability="backoff",
                                        expects="backoff_status"))


@app.post("/backoff/reset/{channel}")
async def backoff_reset_channel(channel: str):
    cmd = {"slower": "BZS", "xbeam": "BZX"}.get(channel.lower())
    if cmd is None:
        raise HTTPException(422, detail={
            "error": f"unknown channel {channel!r}", "valid": ["slower", "xbeam"]})
    return JSONResponse(content=enqueue(cmd, capability="backoff",
                                        expects="backoff_status"))


# ==========================================================================
# Operations
# ==========================================================================

@app.get("/operations/{op_id}")
async def get_operation(op_id: str):
    svc.expire_operations()
    op = svc.operations.get(op_id)
    if op is None:
        raise HTTPException(404, detail={"error": "unknown operation id"})
    return JSONResponse(content=jsonable(op))


@app.get("/operations")
async def list_operations():
    svc.expire_operations()
    return JSONResponse(content=jsonable(list(svc.operations.values())))


# ==========================================================================
# Data
# ==========================================================================

@app.get("/waveform")
async def get_waveform():
    """Request a trace and return the most recent one.

    Still one poll stale by construction, but the queue now coalesces, so a
    stalled firmware no longer accumulates a burst of ``R`` that all arrive at
    once on recovery.  ``age_s`` lets the client tell a stale trace from a
    fresh one rather than assuming.
    """
    if svc.link.connected.is_set() and not svc.link.busy.is_set():
        svc.link.send("R", coalesce_key="R", priority=2, expects="scan")
    return JSONResponse(content=jsonable({
        "scan_data": svc.data.get("scan"),
        "tracking_data": svc.data.get("tracking"),
        "age_s": svc.data.age("scan"),
        "busy": svc.link.busy.is_set(),
        "connected": svc.link.connected.is_set(),
    }))


def _simple(key: str):
    async def handler():
        return JSONResponse(content=jsonable(svc.data.get(key) or {}))
    return handler


app.add_api_route("/peaks", _simple("peaks"), methods=["GET"])
app.add_api_route("/clusters", _simple("clusters"), methods=["GET"])
app.add_api_route("/tracking", _simple("tracking"), methods=["GET"])
app.add_api_route("/stats", _simple("stats"), methods=["GET"])
app.add_api_route("/spc", _simple("spectral purity curve"), methods=["GET"])


# ==========================================================================
# Core commands
# ==========================================================================

@app.post("/init")
async def reinitialize():
    return JSONResponse(content=enqueue("I", expects="initialization"))


@app.post("/FB")
async def toggle_feedback():
    return JSONResponse(content=enqueue("FB"))


@app.post("/TD")
async def toggle_debug():
    return JSONResponse(content=enqueue("TD"))


@app.post("/ZS")
async def zero_slower():
    return JSONResponse(content=enqueue("ZS"))


@app.post("/ZX")
async def zero_xbeam():
    return JSONResponse(content=enqueue("ZX"))


@app.post("/Z")
async def zero_all():
    return JSONResponse(content=enqueue("Z"))


@app.post("/SPC/{startmA}/{stopmA}/{stepuA}")
async def run_spc(startmA: float, stopmA: float, stepuA: float):
    if stepuA <= 0:
        raise HTTPException(422, detail={"error": "step must be positive"})
    if startmA >= stopmA:
        raise HTTPException(422, detail={"error": "start must be below stop"})
    result = enqueue(f"S{startmA},{stopmA},{stepuA}",
                     coalesce_key="S", expects="spectral purity curve")
    op = svc.new_operation("S", "spectral purity curve", svc.cfg.timeout_spc)
    return JSONResponse(status_code=202, content={
        **result, "operation_id": op["id"], "poll": f"/operations/{op['id']}"})


@app.post("/C/{variable}/{value}")
async def change_var_legacy(variable: str, value: float):
    """Deprecated path-parameter form, kept for the current Dash page."""
    return await set_param(variable, value)


# ==========================================================================
# Diagnostics
# ==========================================================================

@app.get("/status")
async def get_status():
    svc.expire_operations()
    return JSONResponse(content=jsonable({
        "uptime_s": round(time.time() - svc.started, 1),
        "link": svc.link.status(),
        "interlock": svc.interlock.status(),
        "data_ages_s": svc.data.ages(),
        "log_counts": svc.warnings.counts(),
        "faults": svc.warnings.sticky(),
        "operations": len(svc.operations),
    }))


@app.get("/log/recent")
async def get_log(limit: int = Query(50, le=500), level: str | None = None):
    return JSONResponse(content=jsonable({
        "entries": svc.warnings.recent(limit, level),
        "sticky": svc.warnings.sticky(),
        "counts": svc.warnings.counts(),
    }))


@app.post("/log/acknowledge")
async def acknowledge(text: str | None = Body(None, embed=True)):
    return JSONResponse(content={"acknowledged": svc.warnings.acknowledge(text)})


@app.get("/events")
async def get_events(limit: int = Query(200, le=2000), kind: str | None = None):
    return JSONResponse(content=jsonable(svc.events.recent(limit, kind)))


@app.get("/metrics")
async def get_metrics(hours: float = Query(24.0, gt=0, le=168)):
    """Relock rate per hour per channel, and the other control-path rates.

    This is the number that says whether a lock is healthy. A servo that relocks
    twice an hour and one that relocks twice a minute look identical on the
    waveform display.
    """
    return JSONResponse(content=jsonable(svc.events.metrics(hours)))

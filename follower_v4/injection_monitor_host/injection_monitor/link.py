"""The serial link: the single owner of the port.

One thread reads and writes.  Everything else talks to it through
``CommandQueue`` and receives parsed results through callbacks.

Design notes that matter for this hardware:

* **The board does not reset when the Pi opens the port.**  The Due's native
  USB port keeps running across a host restart, so on connect the firmware may
  already be locked and servoing with parameters the host knows nothing about.
  Two consequences: we never send ``I`` on connect (that would reset refHeight
  on a healthy lock), and we *reconcile* against the firmware's reported state
  before considering a replay.

* **Blocks are dispatched by name, never by "what did we last ask for".**
  ``[START] Backoff Status`` arrives unsolicited on any bias-related ``C``.

* **Every read has a deadline.**  The old ``read_until`` looped forever on a
  dropped terminator while holding ``operation_in_progress``, which wedged the
  whole API and looked exactly like a hang.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import serial

from . import legacy, protocol
from .config import Settings
from .constants import MAX_SAMPLE_HIGH_BYTE, N as FIRMWARE_N
from .params import BACKOFF, CORE, HOLDOFF, SCAN_TIMEOUT, ParameterStore

log = logging.getLogger(__name__)

#: A firmware restart announces itself with this.  See firmware/boot_banner.md.
#: Matched loosely so a future format change still registers as "it rebooted".
BOOT_RE = re.compile(r"\bboot\b", re.IGNORECASE)


def _first_invalid_sample(buf: bytes) -> int | None:
    """Index of the first little-endian uint16 above 0x0FFF, or None.

    The ADC is ADC_BITS wide, so a valid trace has every high byte at or below
    MAX_SAMPLE_HIGH_BYTE.  Printable ASCII never is, so text in a high-byte
    position means the payload is no longer sample data.
    """
    for i in range(1, len(buf), 2):
        if buf[i] > MAX_SAMPLE_HIGH_BYTE:
            return i // 2
    return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

#: Commands that must never be dropped or delayed by a full queue.
SAFETY = frozenset({"ZS", "ZX", "Z"})


@dataclass(order=True)
class Command:
    #: Lower sorts first.  0 = safety, 1 = user action, 2 = polling.
    priority: int = 2
    seq: int = 0
    text: str = field(default="", compare=False)
    id: str = field(default="", compare=False)
    #: Commands sharing a key collapse to one pending instance.
    coalesce_key: str | None = field(default=None, compare=False)
    #: Canonical block name expected in reply, for logging only.
    expects: str | None = field(default=None, compare=False)
    created: float = field(default_factory=time.monotonic, compare=False)

    @property
    def wire(self) -> bytes:
        return (self.text.rstrip("\n") + "\n").encode("ascii", errors="ignore")


class CommandQueue:
    """Bounded, coalescing, priority queue.

    The old ``queue.Queue()`` was unbounded and the waveform endpoint enqueued
    an ``R`` per poll regardless of whether one was outstanding.  A blocking
    recovery sweep (up to ~13 s) therefore accumulated a burst that all hit the
    Arduino at once on recovery.  Coalescing ``R`` fixes the burst; the bound
    fixes the unbounded growth; the safety bypass makes sure ``ZS`` is never
    the thing that gets dropped.
    """

    def __init__(self, maxsize: int = 16):
        self._maxsize = maxsize
        self._items: deque[Command] = deque()
        self._lock = threading.Lock()
        self._seq = 0
        self.dropped = 0
        self.coalesced = 0

    def put(self, text: str, coalesce_key: str | None = None,
            expects: str | None = None, priority: int | None = None,
            cmd_id: str | None = None) -> tuple[bool, str]:
        """Returns (accepted, command id or reason)."""
        bare = text.strip()
        if priority is None:
            priority = 0 if bare in SAFETY else 1

        with self._lock:
            if coalesce_key is not None:
                for it in self._items:
                    if it.coalesce_key == coalesce_key:
                        self.coalesced += 1
                        return False, it.id
            if priority > 0 and len(self._items) >= self._maxsize:
                self.dropped += 1
                log.warning("command queue full (%d); dropping %r",
                            self._maxsize, bare)
                return False, "queue full"
            self._seq += 1
            cmd = Command(
                priority=priority, seq=self._seq, text=bare,
                id=cmd_id or f"cmd{self._seq}",
                coalesce_key=coalesce_key, expects=expects,
            )
            self._items.append(cmd)
            # Small queue; a sort costs nothing and keeps safety first.
            self._items = deque(sorted(self._items))
            return True, cmd.id

    def get(self) -> Command | None:
        with self._lock:
            return self._items.popleft() if self._items else None

    def clear(self, keep_safety: bool = True) -> int:
        with self._lock:
            before = len(self._items)
            self._items = deque(
                c for c in self._items if keep_safety and c.priority == 0
            )
            return before - len(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._items),
                "maxsize": self._maxsize,
                "dropped": self.dropped,
                "coalesced": self.coalesced,
                "pending": [c.text for c in self._items],
            }


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


@dataclass
class Capabilities:
    holdoff: bool = False
    backoff: bool = False
    #: Inferred, not probed: scan_timeout_ms shipped in the same firmware as
    #: the holdoff and back-off features, and there is no block that reports it.
    scan_timeout: bool = False
    firmware_banner: str | None = None
    probed_at: float | None = None
    #: True when the probe timed out on everything, i.e. this looks like v3.
    legacy_firmware: bool = True

    def tags(self) -> set[str]:
        t = {CORE}
        if self.holdoff:
            t.add(HOLDOFF)
        if self.backoff:
            t.add(BACKOFF)
        if self.scan_timeout:
            t.add(SCAN_TIMEOUT)
        return t

    def as_dict(self) -> dict:
        return {
            "holdoff": self.holdoff,
            "backoff": self.backoff,
            "scan_timeout": self.scan_timeout,
            "legacy_firmware": self.legacy_firmware,
            "firmware_banner": self.firmware_banner,
            "probed_at": self.probed_at,
            "tags": sorted(self.tags()),
            "probed": self.probed_at is not None,
        }


class CapabilityError(RuntimeError):
    """Raised when an endpoint needs a feature this firmware lacks."""

    def __init__(self, capability: str, detail: str = ""):
        self.capability = capability
        super().__init__(detail or f"firmware does not support: {capability}")


# --------------------------------------------------------------------------
# The link
# --------------------------------------------------------------------------

Sink = Callable[[str, Any], None]


class SerialLink:
    """Owns the port, the reader thread and the reconnect supervisor."""

    def __init__(
        self,
        settings: Settings,
        store: ParameterStore,
        on_block: Sink | None = None,
        on_tagged: Callable[[protocol.TaggedLine], None] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        on_untagged: Callable[[str], None] | None = None,
        on_connect_change: Callable[[bool], None] | None = None,
        on_reset: Callable[[], None] | None = None,
    ):
        self.cfg = settings
        self.store = store
        self.queue = CommandQueue(settings.command_queue_size)
        self.caps = Capabilities()
        self.parse_stats = protocol.ParseStats()

        self._on_block = on_block or (lambda k, v: None)
        self._on_tagged = on_tagged or (lambda t: None)
        self._on_event = on_event or (lambda k, v: None)
        self._on_untagged = on_untagged or (lambda text: None)
        self._on_connect_change = on_connect_change or (lambda ok: None)
        #: Fired when _looks_like_fresh_boot() concludes the board restarted.
        #: For state that, unlike holdoff_slope, the firmware never reports
        #: back on the wire (FB's enabled/disabled flag, for instance) this is
        #: the only signal a reset happened at all -- there is nothing to
        #: reconcile against, only something to invalidate.
        self._on_reset = on_reset or (lambda: None)

        self._ser: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = threading.Event()
        #: Set once the capability probe and replay decision have finished.
        #: Until then the reported capabilities are provisional -- endpoints
        #: that gate on them would wrongly return 501 for the first second or
        #: so after startup.
        self.ready = threading.Event()
        #: Set while a block is being consumed.  Endpoints use wait_idle().
        self.busy = threading.Event()
        self._idle = threading.Condition()

        #: Most recent [WARN], so a failed calibration can be reported with a
        #: reason rather than just slope,0.
        self.last_warning: str | None = None
        self._probe_seen: set[str] = set()
        self._probe_lock = threading.Lock()
        #: While set, status blocks are buffered rather than applied -- see
        #: _after_connect().
        self._probe_mode = False
        self._probe_blocks: dict[str, dict] = {}
        self._last_error: str | None = None
        self._connect_count = 0
        self._reset_count = 0

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervise, name="serial-link", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._close()

    def _close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self.ready.clear()
        if self.connected.is_set():
            self.connected.clear()
            self._on_connect_change(False)

    def _supervise(self) -> None:
        """Reconnect forever with exponential backoff.

        The old loop caught SerialException, slept 1 s and *returned* -- the
        thread died and was never restarted, so one USB glitch killed
        acquisition until someone restarted the service.
        """
        delay = self.cfg.reconnect_delay_min
        while not self._stop.is_set():
            try:
                self._open()
                delay = self.cfg.reconnect_delay_min
                self._after_connect()
                self._read_loop()
            except serial.SerialException as e:
                self._last_error = str(e)
                log.error("serial link down: %s; retrying in %.0fs", e, delay)
            except Exception as e:
                self._last_error = str(e)
                log.exception("unexpected error in serial link; retrying in %.0fs", delay)
            finally:
                self.busy.clear()
                self._wake_idle()
                self._close()
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.cfg.reconnect_delay_max)

    def _open(self) -> None:
        log.info("opening %s at %d baud", self.cfg.port, self.cfg.baud)
        self._ser = serial.Serial(
            self.cfg.port, self.cfg.baud, timeout=self.cfg.serial_timeout,
            write_timeout=2.0,
        )
        # No sleep-and-reset dance: the native USB port does not reset the
        # board on open, so there is nothing to wait for.  We do drop stale
        # bytes, which may be a half-finished block from before the host died.
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        if self.cfg.trace_samples != FIRMWARE_N:
            log.error(
                "trace_samples=%d but shared/constants.json says N=%d. Every "
                "trace will be misframed. Regenerate constants or fix the "
                "override.", self.cfg.trace_samples, FIRMWARE_N)
        self._connect_count += 1
        self.connected.set()
        self._last_error = None
        self._on_connect_change(True)
        log.info("serial link up (connection #%d)", self._connect_count)

    def _after_connect(self) -> None:
        """Probe, decide, then either adopt or replay.

        Order matters, and it is subtler than it looks.  Reconciling before
        deciding would destroy the evidence: on a rebooted board the firmware
        reports compiled-in defaults, so confirming them would overwrite every
        stored value with a default and the subsequent replay would push those
        defaults straight back.  Probe replies are therefore *buffered*; only
        once we know whether the board restarted do we either adopt what it
        reports or overwrite it with what we stored.
        """
        self.store.mark_unconfirmed()
        self._probe_mode = True
        self._probe_blocks.clear()
        try:
            self._probe_capabilities()
            rebooted = self._looks_like_fresh_boot()
        finally:
            self._probe_mode = False

        if rebooted:
            self._on_reset()

        policy = self.cfg.replay_policy
        if policy == "connect" or (policy == "boot" and rebooted):
            # The board is at defaults; ours are the real values.  Discard the
            # buffered report rather than adopting it.
            self.replay_parameters(
                "firmware appears to have restarted" if rebooted else "connect policy"
            )
        else:
            # The board has been running.  Whatever it reports is what is
            # actually acting, so adopt it.
            self._adopt_probe_results()
            log.info("not replaying parameters (policy=%s, rebooted=%s); "
                     "firmware state adopted", policy, rebooted)

        if self.cfg.send_init_on_connect:
            self.queue.put("I", expects="initialization")

        self.ready.set()

    def _adopt_probe_results(self) -> None:
        for key, data in self._probe_blocks.items():
            self._apply_confirmations(key, data)
        pending = self.store.pending_names()
        if pending and not self.caps.legacy_firmware:
            log.info("%d parameters remain unconfirmed: this firmware reports "
                     "only a subset in its status blocks", len(pending))

    def _probe_capabilities(self) -> None:
        """Ask for the status blocks and see what comes back.

        v3 firmware falls off the end of loop() for any unrecognised command
        and answers nothing at all, so a timeout means "not supported" rather
        than "broken".  Both probes are read-only and safe to send to a
        running, locked board.
        """
        with self._probe_lock:
            self._probe_seen.clear()
        for cmd in ("HR", "BR"):
            self._write_now(cmd)
        deadline = time.monotonic() + self.cfg.capability_probe_timeout
        while time.monotonic() < deadline:
            with self._probe_lock:
                if {"holdoff status", "backoff status"} <= self._probe_seen:
                    break
            self._pump(0.05)

        with self._probe_lock:
            seen = set(self._probe_seen)
        self.caps.holdoff = "holdoff status" in seen
        self.caps.backoff = "backoff status" in seen
        self.caps.scan_timeout = self.caps.holdoff or self.caps.backoff
        self.caps.legacy_firmware = not (self.caps.holdoff or self.caps.backoff)
        self.caps.probed_at = time.time()

        if self.caps.legacy_firmware:
            log.warning(
                "no reply to HR or BR: treating this as pre-v4 firmware. "
                "Holdoff and back-off endpoints will return 501 and their "
                "parameters will not be sent."
            )
        else:
            log.info("capabilities: %s", sorted(self.caps.tags()))

    def _looks_like_fresh_boot(self) -> bool:
        """Decide whether the board restarted while we were away.

        With a boot banner this is exact.  Without one it is a heuristic, and
        the one signal that is both cheap and specific is holdoff_slope: it is
        zero at boot, it can only become non-zero through a calibration or a
        host replay, and the firmware has no flash to keep it in.  So a stored
        non-zero slope against a firmware-reported zero means the board
        restarted.

        Anything ambiguous resolves to "no".  A spurious replay of 26 C lines
        into a healthy lock is a worse outcome than a missed one, because the
        host will notice a missing slope the next time it reads /params and the
        operator can press sync.
        """
        if self.caps.firmware_banner:
            log.info("boot banner seen: %s", self.caps.firmware_banner)
            return True
        if not self.caps.holdoff:
            return False
        stored = self.store.value("holdoff_slope")
        reported = self.store.runtime().get("holdoff_slope_reported")
        if stored != 0 and reported == 0:
            log.info("stored holdoff_slope=%g but firmware reports 0: "
                     "treating as a firmware restart", stored)
            self._reset_count += 1
            return True
        return False

    def replay_parameters(self, reason: str) -> int:
        """Push stored C parameters, filtered by capability."""
        cmds = self.store.replay_commands(self.caps.tags())
        log.info("replaying %d parameters (%s)", len(cmds), reason)

        # Each bias-related C triggers an unsolicited Backoff Status echoing
        # the state *part way through* the replay.  Applying those as they
        # arrive produces a mismatch warning per line for values we are in the
        # middle of setting.  Buffer instead, and adopt once after the final
        # HR/BR, which reflects the settled state.
        was_probing, self._probe_mode = self._probe_mode, True
        sent = 0
        try:
            for c in cmds:
                self._write_now(c)
                sent += 1
                # The firmware parses one line per loop() pass; pace them so a
                # burst cannot overrun the 64-byte command buffer.
                self._pump(0.02)
            if self.caps.holdoff:
                self._write_now("HR")
            if self.caps.backoff:
                self._write_now("BR")
            self._pump(0.3)
        finally:
            self._probe_mode = was_probing

        self._adopt_probe_results()
        return sent

    # ---------------- reading ----------------

    def _read_loop(self) -> None:
        while not self._stop.is_set() and self._ser is not None:
            line = self._readline()
            if line:
                self._handle_line(line)
            self._service_queue()

    def _pump(self, seconds: float) -> None:
        """Read and dispatch for a bounded time, without touching the queue."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = self._readline()
            if not line:
                return
            self._handle_line(line)

    def _readline(self) -> str:
        if self._ser is None:
            raise serial.SerialException("port closed")
        raw = self._ser.readline()
        if not raw:
            return ""
        return raw.decode("utf-8", errors="replace").strip()

    def _handle_line(self, line: str) -> None:
        name = protocol.block_name(line)
        if name is not None:
            self._handle_block(name)
            return

        tag = protocol.classify_line(line)
        if tag is not None:
            if tag.tag == "[WARN]":
                self.last_warning = tag.text
            if tag.tag == "[HOLDOFF]" and tag.event:
                self._on_event("holdoff", tag.event)
            if BOOT_RE.search(tag.text) and tag.tag in ("[INFO]", "[DEBUG]"):
                self._on_boot_banner(tag.text)
            self._on_tagged(tag)
            return

        # Untagged chatter, e.g. v3's "delayus set to: 200" -- and several of
        # the firmware's own event lines, which are not [DEBUG]-tagged at all
        # ("Recovery failed.", "Max relock attempts reached!"). These still
        # matter for /metrics, so they get a real callback rather than being
        # dropped into the log at DEBUG and lost.
        log.debug("untagged: %s", line)
        self._on_untagged(line)

    def _on_boot_banner(self, text: str) -> None:
        log.warning("firmware restart detected: %s", text)
        self.caps.firmware_banner = text
        self._reset_count += 1
        self.store.mark_unconfirmed()
        # The board came up with compiled-in defaults, so the queue may hold
        # commands aimed at the old state.
        self.queue.clear(keep_safety=True)
        if self.cfg.replay_policy in ("boot", "connect"):
            self.replay_parameters("boot banner")

    def _handle_block(self, name: str) -> None:
        self.busy.set()
        try:
            if name in protocol.BINARY_BLOCKS:
                self._read_trace()
            else:
                self._read_text_block(name)
        except TimeoutError as e:
            self.parse_stats.truncated += 1
            log.error("block %r: %s; resynchronising", name, e)
            self._resync()
        finally:
            self.busy.clear()
            self._wake_idle()

    def _timeout_for(self, name: str) -> float:
        if name == "spectral purity curve":
            return self.cfg.timeout_spc
        if name == "holdoff calibration":
            return self.cfg.timeout_calibration
        if name in ("initialization", "stats", "clusters", "peak_tracking"):
            return self.cfg.timeout_init
        if name in ("holdoff status", "backoff status"):
            return self.cfg.timeout_status
        return self.cfg.timeout_unknown

    def _read_until_end(self, name: str) -> list[str]:
        """Collect lines up to the next ``[END]``, with a deadline.

        The deadline is the whole point.  ``if not line: continue`` against a
        1 s serial timeout is an infinite loop the moment a terminator is
        dropped, and it holds ``busy`` while it spins.
        """
        timeout = self._timeout_for(name)
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while time.monotonic() < deadline:
            if len(lines) >= self.cfg.max_block_lines:
                raise TimeoutError(
                    f"{name}: exceeded {self.cfg.max_block_lines} lines without [END]"
                )
            line = self._readline()
            if not line:
                continue
            if protocol.is_end(line):
                return lines
            # A nested [START] means the previous block's [END] was lost.
            if protocol.block_name(line) is not None:
                log.warning("%r: nested [START] before [END]; previous block "
                            "truncated", name)
                lines.append(line)
                continue
            tag = protocol.classify_line(line)
            if tag is not None:
                if tag.tag == "[WARN]":
                    self.last_warning = tag.text
                self._on_tagged(tag)
                # Kept in `lines` too: the parsers skip tagged lines by name,
                # and having them preserves the interleaving for the log.
            lines.append(line)
        raise TimeoutError(f"{name}: no [END] within {timeout:.0f}s")

    def _read_text_block(self, name: str) -> None:
        lines = self._read_until_end(name)

        # New-style blocks are identified by the outer [START] name; legacy
        # ones by their inner BEGIN_ marker, because the outer names do not
        # describe the contents (one `I` emits [START] Initalization .. [END]
        # Peaks, and the body is BEGIN_peaks).
        key: str | None = None
        parser = None
        if name in protocol.BLOCK_PARSERS:
            key, parser = protocol.BLOCK_PARSERS[name]
        else:
            inner = legacy.inner_marker(lines)
            if inner is not None:
                key, parser = inner, legacy.LEGACY_PARSERS[inner]

        if parser is None:
            self.parse_stats.record_unknown(name)
            log.info("unrecognised block %r (%d lines) discarded", name, len(lines))
            return

        try:
            data = parser(lines)
        except protocol.BlockParseError as e:
            self.parse_stats.record_fail(key)
            log.error("parse failed for %s: %s", key, e)
            return
        except Exception as e:
            self.parse_stats.record_fail(key)
            log.exception("unexpected parse error for %s: %s", key, e)
            return

        self.parse_stats.record_ok(key)
        with self._probe_lock:
            self._probe_seen.add(name)
        self._post_process(key, data)
        self._on_block(key, data)

    def _post_process(self, key: str, data: dict) -> None:
        """Route a parsed block into the parameter store.

        Runtime state (what the firmware is doing) is always applied.
        Confirmations (what the firmware thinks the parameters are) are
        buffered during the connect probe, because at that point we cannot yet
        tell a running board from one that just rebooted into its defaults.
        """
        if key == "holdoff_status":
            self.store.set_runtime("holdoff_delayus", data.get("delayus"))
            self.store.set_runtime("holdoff_enabled", data.get("enabled"))
            self.store.set_runtime("holdoff_slope_reported", data.get("slope"))

        if key in ("holdoff_status", "backoff_status"):
            if self._probe_mode:
                self._probe_blocks[key] = data
                return
            self._apply_confirmations(key, data)
            return

        if key == "holdoff_calibration":
            self._apply_confirmations(key, data)

    def _apply_confirmations(self, key: str, data: dict) -> None:
        if key == "holdoff_status":
            slope = data.get("slope")
            # slope == 0 is the firmware's "never calibrated" sentinel, not a
            # measurement -- a real slope is strictly negative.  Confirming it
            # over a stored calibration would erase the very evidence that the
            # board rebooted, and _looks_like_fresh_boot() would then never
            # fire.  Treat zero as "no information" and leave the store alone.
            if slope:
                self.store.confirm("holdoff_slope", slope)
            elif self.store.value("holdoff_slope"):
                log.info("firmware reports holdoff_slope=0 but the host holds "
                         "%g; keeping the stored calibration pending replay",
                         self.store.value("holdoff_slope"))
            if data.get("cal_step") is not None:
                self.store.confirm("holdoff_cal_step", data["cal_step"])

        elif key == "backoff_status":
            if data.get("nominal") is not None:
                self.store.confirm("unlock_thresh", data["nominal"])
            if data.get("floor") is not None:
                self.store.confirm("backoff_floor", data["floor"])
            for ch, vals in (data.get("channels") or {}).items():
                if vals.get("bias_enabled") is not None:
                    self.store.confirm(f"{ch}_bias", vals["bias_enabled"])
                if vals.get("contbump_enabled") is not None:
                    self.store.confirm(f"{ch}_contbump", vals["contbump_enabled"])
                if vals.get("floor_latched"):
                    self._on_event("backoff_latched", {"channel": ch, **vals})

        elif key == "holdoff_calibration":
            if data.get("ok"):
                self.store.set("holdoff_slope", data["slope"], source="firmware")
                log.info("holdoff calibrated: slope=%g samples/us "
                         "(sample interval %.3f us, residual %.3g)",
                         data["slope"], data["sample_interval_us"] or 0.0,
                         data["residual_rms"] or 0.0)
            else:
                # slope,0 is the documented failure signal; the reason is in
                # the [WARN] that accompanied it.
                data["failure_reason"] = self.last_warning
                log.error("holdoff calibration failed: %s",
                          self.last_warning or "no reason reported")
            self._on_event("holdoff_calibration", data)

    def _read_trace(self) -> None:
        """Read exactly 2*N bytes.

        ``sendTrace()`` writes the payload with no terminator and no length
        prefix, so a short read leaves the remainder to be parsed as text and
        silently desynchronises the stream.  The old code did a single
        ``ser.read(2*N)`` against a 1 s timeout and returned zeros on any
        exception, which hid the desync rather than preventing it.
        """
        want = 2 * self.cfg.trace_samples
        deadline = time.monotonic() + self.cfg.timeout_status
        buf = bytearray()
        while len(buf) < want and time.monotonic() < deadline:
            chunk = self._ser.read(want - len(buf))
            if chunk:
                buf.extend(chunk)

        if len(buf) != want:
            self.parse_stats.truncated += 1
            log.error("trace: got %d of %d bytes; discarding and resyncing. "
                      "If this repeats, N in the firmware and trace_samples "
                      "(%d) disagree.", len(buf), want, self.cfg.trace_samples)
            self._resync()
            return

        bad = _first_invalid_sample(buf)
        if bad is not None:
            # A short trace cannot be caught by length alone: the firmware
            # sends no terminator, so the text of the *following* block flows
            # straight into the payload and pads it back to the expected size.
            # The 12-bit ADC gives us a rigorous check instead -- samples are
            # 0..4095, so in little-endian uint16 every high byte is <= 0x0F,
            # and ASCII is not.  The first high byte above 0x0F is where the
            # data ended and the text began.
            self.parse_stats.truncated += 1
            log.error(
                "trace: payload is not 12-bit sample data past sample %d of %d "
                "(high byte 0x%02X). The trace was short and the next block's "
                "text was read into it. Check that N in the firmware matches "
                "trace_samples (%d).",
                bad, self.cfg.trace_samples, buf[2 * bad + 1], self.cfg.trace_samples,
            )
            self._recover_stream(buf)
            return

        import numpy as np  # local: keeps the parser layer numpy-free

        scan = np.frombuffer(bytes(buf), dtype=np.uint16)
        self.parse_stats.record_ok("trace")
        self._on_block("scan", scan.tolist())

    def _recover_stream(self, buf: bytes) -> None:
        """Re-enter text framing after a corrupted trace.

        The overrun swallowed the first bytes of whatever came next, so that
        block is unrecoverable -- but anything after its newline is fine.  Log
        the fragment so the operator can see what was lost, then drop only what
        is still buffered in the driver.
        """
        tail = buf[buf.find(b"[START]"):] if b"[START]" in buf else b""
        if tail:
            log.error("trace overrun consumed: %r", tail[:120])
        self._resync()

    def _resync(self) -> None:
        """Drop buffered input after a truncated block."""
        try:
            if self._ser is not None:
                self._ser.reset_input_buffer()
        except Exception:
            pass

    # ---------------- writing ----------------

    def _service_queue(self) -> None:
        if self.busy.is_set():
            return
        cmd = self.queue.get()
        if cmd is None:
            return
        self._write(cmd)

    def _write(self, cmd: Command) -> None:
        wire = cmd.wire
        if len(wire) > 64:
            # The firmware's command buffer is 64 bytes and over-long lines are
            # discarded with a [WARN].  Better to refuse here than to send it.
            log.error("refusing to send %d-byte command %r (firmware buffer is "
                      "64 bytes)", len(wire), cmd.text)
            return
        try:
            self._ser.write(wire)
            log.debug("-> %s (waited %.2fs)", cmd.text,
                      time.monotonic() - cmd.created)
        except serial.SerialTimeoutException:
            log.error("write timed out sending %r", cmd.text)

    def _write_now(self, text: str) -> None:
        """Bypass the queue.  Probe and replay only, from the reader thread."""
        if self._ser is None:
            return
        self._write(Command(text=text, id="direct"))

    # ---------------- public API ----------------

    def wait_ready(self, timeout: float) -> bool:
        """Block until capabilities are known.  False on timeout."""
        return self.ready.wait(timeout)

    def send(self, text: str, capability: str | None = None, **kw) -> tuple[bool, str]:
        """Enqueue a command, refusing it if the firmware lacks the feature."""
        if capability is not None and not self.ready.is_set():
            raise CapabilityError(
                capability,
                "firmware capabilities not yet probed; retry in a moment",
            )
        if capability is not None and capability not in self.caps.tags():
            raise CapabilityError(capability)
        if not self.connected.is_set():
            return False, "serial link down"
        return self.queue.put(text, **kw)

    def _wake_idle(self) -> None:
        with self._idle:
            self._idle.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """Block until no block is being consumed.  Returns False on timeout.

        Kept for the synchronous path; api.py wraps this in an asyncio.Event so
        endpoints do not hold a threadpool worker.
        """
        deadline = time.monotonic() + timeout
        with self._idle:
            while self.busy.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
        return True

    def status(self) -> dict:
        return {
            "connected": self.connected.is_set(),
            "busy": self.busy.is_set(),
            "port": self.cfg.port,
            "connect_count": self._connect_count,
            "firmware_resets_seen": self._reset_count,
            "last_error": self._last_error,
            "last_warning": self.last_warning,
            "capabilities": self.caps.as_dict(),
            "queue": self.queue.stats(),
            "parsing": self.parse_stats.as_dict(),
        }

"""
network_service — The single authoritative source of network state.

Replaces `sync/network_monitor.py`, which had four defects that together
produced the reported "Online / Online (Slow: 2.3s) / Offline" flicker:

  * it assumed `is_online = True` before the first probe ever ran, so the UI
    published a state it had not measured;
  * a single transient failure flipped the state immediately — no debounce, no
    hysteresis;
  * it could not distinguish "this machine has no network" from "the backend
    is down" from "the token expired", so a 500 from the API read as offline
    and paused the sync queue;
  * it subclassed QThread with a `while` loop parked in a 30-second
    `QWaitCondition.wait()`, which `quit()` cannot interrupt.

This service runs on the `LoopService` pattern (QObject + moveToThread +
QTimer), so shutdown is deterministic. Probes are strictly sequential on one
thread, which is what makes stale-result-overwrites-newer-result structurally
impossible rather than merely unlikely; a monotonic probe sequence is still
recorded and published so consumers can reason about ordering.
"""
from __future__ import annotations

import socket
import time

import httpx
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtCore import Signal

from app.api.client import ApiClient
from app.api.exceptions import ApiConnectionError, ApiHttpError, ApiTimeoutError
from core.service import LoopService, ServiceState


class NetworkState:
    """Meaningful connectivity states (STEP 11 of the stability spec)."""
    UNKNOWN = "UNKNOWN"
    NO_NETWORK = "NO_NETWORK"
    NETWORK_AVAILABLE = "NETWORK_AVAILABLE"
    BACKEND_REACHABLE = "BACKEND_REACHABLE"
    BACKEND_UNREACHABLE = "BACKEND_UNREACHABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"

    #: States in which it is worth attempting API work.
    USABLE = frozenset({BACKEND_REACHABLE, AUTH_REQUIRED})

    #: States in which API work is worth *attempting* even though the backend
    #: has not been confirmed reachable. UNKNOWN belongs here and not in
    #: USABLE: it is the absence of a measurement, not the measurement of an
    #: outage, and treating it as an outage is what left a freshly logged-in
    #: dashboard with no projects until the first probe happened to commit.
    #: NETWORK_AVAILABLE is the same case one step further along.
    #:
    #: `USABLE` still means "confirmed usable" for everything that reports
    #: connectivity to the user; this set means "do not refuse to try".
    WORTH_TRYING = USABLE | frozenset({UNKNOWN, NETWORK_AVAILABLE})


class NetworkService(LoopService):
    """
    Probes backend reachability and publishes one debounced, hysteretic state.

    Signals:
        network_state_changed(str)  — emitted only on a committed transition
        latency_measured(int)       — round-trip milliseconds, successful probes
    """

    name = "network"

    network_state_changed = Signal(str)
    latency_measured = Signal(int)

    #: Probe cadence when the backend is healthy.
    HEALTHY_INTERVAL_MS = 30_000
    #: Probe cadence while degraded — faster, but still bounded, and jittered
    #: so a fleet recovering from an outage does not synchronise.
    DEGRADED_INTERVAL_MS = 5_000
    #: Consecutive failures required before committing to a "down" state.
    #: One failure is noise; three in a row is a signal. This asymmetry is the
    #: hysteresis that stops the status pill flickering.
    FAILURES_TO_DEGRADE = 3
    #: Consecutive successes required to commit back to healthy.
    SUCCESSES_TO_RECOVER = 1
    #: A probe slower than this is reported as reachable-but-slow.
    #: MUST stay below PROBE_READ_TIMEOUT_S, or "slow" is unreachable as a
    #: classification: the request would time out and be counted a failure
    #: before it could ever be measured as slow. These two were previously both
    #: 2s, so a backend answering in just over 2 seconds -- a cold serverless
    #: start, or a cross-region query, both entirely normal -- was recorded as
    #: a failure. Three of those in a row committed a false offline state.
    SLOW_THRESHOLD_MS = 1_500
    #: Probes use short timeouts of their own; a health check must never
    #: inherit a 30-second upload timeout. Kept deliberately tight: the probe
    #: blocks its service thread, and a thread blocked in I/O cannot honour
    #: `quit()` until the call returns.
    #:
    #: `connect` is separated from `read` on purpose. A hostname that resolves
    #: to several addresses — `localhost` resolves to both ::1 and 127.0.0.1 —
    #: is tried one address at a time, and the connect timeout applies to
    #: *each attempt*. A single scalar 2.5s timeout therefore produced a probe
    #: that measured 7s in practice, which is why shutdown was escalating to
    #: terminate(). Budget for two attempts.
    #:
    #: `read` is the budget for the backend to *think*, which is a different
    #: question from whether it is reachable, and it must be generous enough to
    #: cover a legitimately slow response. A deployed backend behind serverless
    #: cold starts answers /auth/me in well over two seconds routinely; at the
    #: old 2s read timeout those healthy responses were counted as failures.
    PROBE_CONNECT_TIMEOUT_S = 1.0
    PROBE_READ_TIMEOUT_S = 5.0
    #: Timeout for the OS-level routability check, which runs only after a
    #: request has already failed.
    ROUTE_CHECK_TIMEOUT_S = 1.0
    #: Address attempts to budget for when computing the worst case.
    ADDRESS_ATTEMPTS = 2

    #: Worst case for one tick: a failed request across every address, plus a
    #: routability check. The stop budget must exceed that, or shutdown
    #: escalates to terminate() — a last resort, not a normal path.
    stop_timeout_ms = int(
        (PROBE_CONNECT_TIMEOUT_S * ADDRESS_ATTEMPTS
         + PROBE_READ_TIMEOUT_S
         + ROUTE_CHECK_TIMEOUT_S * ADDRESS_ATTEMPTS) * 1000
    ) + 2000

    def __init__(self, runtime, api_client: ApiClient, parent=None) -> None:
        super().__init__(runtime, parent)
        self._api_client = api_client
        self._state = NetworkState.UNKNOWN
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._probe_sequence = 0
        self._last_latency_ms: Optional[int] = None
        self.interval_ms = self.DEGRADED_INTERVAL_MS

    # ── Public state ──────────────────────────────────────────────────────────

    # NOTE: this is `network_state`, not `state`. `BaseService.state` is the
    # *service lifecycle* (RUNNING / DEGRADED / STOPPED) and belongs to the
    # ServiceManager. An earlier revision overrode `state` here, which made the
    # service manager read connectivity where it expected lifecycle — the same
    # class of mistake as overloading `start()`.

    @property
    def network_state(self) -> str:
        """The committed network state (a `NetworkState` value)."""
        return self._state

    @property
    def is_online(self) -> bool:
        """
        True when API work is worth attempting.

        Retained under this name because existing UI code reads it; it now
        derives from the state machine rather than being its own variable.
        """
        return self._state in NetworkState.USABLE

    @property
    def last_latency_ms(self) -> Optional[int]:
        return self._last_latency_ms

    @property
    def is_slow(self) -> bool:
        return (
            self._last_latency_ms is not None
            and self._last_latency_ms >= self.SLOW_THRESHOLD_MS
        )

    def check_now(self) -> None:
        """Request an immediate probe (safe from any thread)."""
        self.wake()

    def note_backend_reachable(self) -> None:
        """
        Record first-hand evidence that the backend just answered us.

        A completed login or session verification *is* a successful probe --
        a better one than the health check, because it exercised the same
        client, host and credentials the dashboard is about to use. Without
        this the service kept its own slower opinion: the user could be
        signed in and looking at an authenticated screen while the committed
        state was still UNKNOWN, which held the dashboard's first data load
        back until a probe caught up seconds later.

        Safe from any thread: it only commits a transition into a usable
        state, which is the direction that requires a single observation
        anyway (SUCCESSES_TO_RECOVER == 1), so it cannot be used to paper
        over an outage.
        """
        self._consecutive_failures = 0
        self._consecutive_successes = max(self._consecutive_successes, 1)
        if self._state == NetworkState.BACKEND_REACHABLE:
            return
        previous = self._state
        self._state = NetworkState.BACKEND_REACHABLE
        self.log.info("network state %s -> %s (observed by an authenticated call)",
                      previous, self._state)
        self.network_state_changed.emit(self._state)
        self._set_state(ServiceState.RUNNING, None)

    # ── Probing ───────────────────────────────────────────────────────────────

    def _host_port(self):
        parsed = urlparse(self._api_client.base_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname, port

    def _has_network_route(self) -> bool:
        """
        Cheap OS-level check for whether the API host is routable at all.

        This is what lets us distinguish NO_NETWORK from BACKEND_UNREACHABLE,
        so a backend outage does not tell the user their internet is down.
        """
        host, port = self._host_port()
        if not host:
            return False
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return False
        for family, socktype, proto, _canonname, sockaddr in addresses:
            if self.stopping:
                # Shutdown was requested; the answer is about to be discarded.
                return False
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(self.ROUTE_CHECK_TIMEOUT_S)
            try:
                sock.connect(sockaddr)
                return True
            except OSError:
                continue
            finally:
                sock.close()
        return False

    def _probe(self) -> str:
        """Run one probe and return the state it implies (uncommitted)."""
        started = time.monotonic()
        timeout = httpx.Timeout(
            connect=self.PROBE_CONNECT_TIMEOUT_S,
            read=self.PROBE_READ_TIMEOUT_S,
            write=self.PROBE_READ_TIMEOUT_S,
            pool=self.PROBE_CONNECT_TIMEOUT_S,
        )
        try:
            # No retry: the probe reports the first failure, which is its job.
            self._api_client.get("/auth/me", timeout=timeout, retry=False)
        except (ApiConnectionError, ApiTimeoutError):
            # Could be no network, or the backend being down. Ask the OS —
            # unless we are shutting down, in which case skip the extra
            # blocking call and let the thread exit.
            if self.stopping:
                return NetworkState.BACKEND_UNREACHABLE
            return (
                NetworkState.BACKEND_UNREACHABLE
                if self._has_network_route()
                else NetworkState.NO_NETWORK
            )
        except ApiHttpError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._last_latency_ms = elapsed_ms
            self.latency_measured.emit(elapsed_ms)
            if exc.status_code in (401, 403):
                # The server answered — it is reachable. The token is the
                # problem, which is a different condition entirely and must
                # not be reported to the user as being offline.
                return NetworkState.AUTH_REQUIRED
            if 500 <= exc.status_code < 600:
                return NetworkState.BACKEND_UNREACHABLE
            return NetworkState.BACKEND_REACHABLE
        except Exception:  # noqa: BLE001
            self.log.exception("unexpected error during network probe")
            return NetworkState.BACKEND_UNREACHABLE

        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._last_latency_ms = elapsed_ms
        self.latency_measured.emit(elapsed_ms)
        return NetworkState.BACKEND_REACHABLE

    def tick(self) -> Optional[int]:
        """One probe cycle. Runs on the network service thread."""
        import random

        self._probe_sequence += 1
        sequence = self._probe_sequence
        observed = self._probe()

        healthy = observed in NetworkState.USABLE
        if healthy:
            self._consecutive_successes += 1
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            self._consecutive_successes = 0

        self.heartbeat(success=healthy)
        self.log.debug(
            "probe #%d observed=%s (fail streak=%d, ok streak=%d, %sms)",
            sequence, observed, self._consecutive_failures,
            self._consecutive_successes, self._last_latency_ms,
        )

        # Commit a transition only once the streak clears the threshold.
        commit = False
        if healthy and self._consecutive_successes >= self.SUCCESSES_TO_RECOVER:
            commit = True
        elif not healthy and self._consecutive_failures >= self.FAILURES_TO_DEGRADE:
            commit = True
        # AUTH_REQUIRED vs BACKEND_REACHABLE are both "usable"; switch between
        # them immediately since neither is a connectivity change.
        elif healthy and self._state in NetworkState.USABLE:
            commit = True

        if commit and observed != self._state:
            previous = self._state
            self._state = observed
            self.log.info("network state %s -> %s (probe #%d)", previous, observed, sequence)
            self.network_state_changed.emit(observed)
            self._set_state(
                ServiceState.RUNNING if healthy else ServiceState.DEGRADED,
                None if healthy else f"backend state {observed}",
            )

        base = self.HEALTHY_INTERVAL_MS if self.is_online else self.DEGRADED_INTERVAL_MS
        # Jitter so 200+ clients do not probe in lockstep after an outage.
        return int(base * (0.85 + random.random() * 0.3))

    def on_start(self) -> None:
        self._state = NetworkState.UNKNOWN
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        super().on_start()

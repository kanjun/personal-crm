import threading
import time
from typing import Final

from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import Field, PrivateAttr

# Gmail's quota is per minute per user and shared with every other process using the
# account, so the sustainable rate is unknowable up front: start moderate, halve on
# pushback, and creep back up while calls succeed.
DEFAULT_START_RATE_PER_SECOND: Final[float] = 3.0
MIN_RATE_PER_SECOND: Final[float] = 1.0
MAX_RATE_PER_SECOND: Final[float] = 6.0
_BACKOFF_FACTOR: Final[float] = 0.7
_RECOVERY_FACTOR: Final[float] = 1.15
_RECOVERY_AFTER_SUCCESSES: Final[int] = 400
# After a rate-limit response, hold everything for a full quota window: the bucket is
# per minute, so anything sent sooner is refused too and just extends the penalty.
# Pace Google calls conservatively to avoid per-account quota refusals.
_PAUSE_AFTER_LIMIT_SECONDS: Final[float] = 61.0


class AdaptiveRateLimiter(MutableModel):
    """Paces calls across threads at a rate that adapts to upstream rate-limit responses."""

    rate_per_second: float = Field(description="Current target rate")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Pacing waits on an Event rather than sleeping, so a shutdown can interrupt a wait.
    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _next_slot_at: float = PrivateAttr(default=0.0)
    _paused_until: float = PrivateAttr(default=0.0)
    _successes_since_change: int = PrivateAttr(default=0)

    def wait_for_slot(self) -> None:
        """Block until this call may proceed under the current rate."""
        with self._lock:
            now = time.monotonic()
            earliest = max(self._next_slot_at, self._paused_until, now)
            self._next_slot_at = earliest + 1.0 / self.rate_per_second
            wait_seconds = earliest - now
        if wait_seconds > 0:
            self._wake.wait(timeout=wait_seconds)

    def paused_for_seconds(self) -> float:
        """How long every call is held back after a rate-limit answer (0 when not paused)."""
        with self._lock:
            return max(0.0, self._paused_until - time.monotonic())

    def interrupt_waits(self) -> None:
        """Release every thread waiting for a slot (used at shutdown)."""
        self._wake.set()

    def record_success(self) -> None:
        with self._lock:
            self._successes_since_change += 1
            if self._successes_since_change >= _RECOVERY_AFTER_SUCCESSES and self.rate_per_second < MAX_RATE_PER_SECOND:
                self.rate_per_second = min(MAX_RATE_PER_SECOND, self.rate_per_second * _RECOVERY_FACTOR)
                self._successes_since_change = 0
                logger.debug("Raised request rate to {:.1f}/s", self.rate_per_second)

    def record_rate_limited(self) -> None:
        with self._lock:
            now = time.monotonic()
            # Several threads see the same limit at once; only the first one in a window backs off.
            if now < self._paused_until:
                return
            self.rate_per_second = max(MIN_RATE_PER_SECOND, self.rate_per_second * _BACKOFF_FACTOR)
            self._paused_until = now + _PAUSE_AFTER_LIMIT_SECONDS
            self._successes_since_change = 0
            logger.info("Rate limited; pausing {:.0f}s and lowering request rate to {:.1f}/s", _PAUSE_AFTER_LIMIT_SECONDS, self.rate_per_second)

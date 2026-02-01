"""Backend pool management with failover logic."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from src.config import parse_backend
from src.dns_resolver import DNSResolver
from src.event_hook import EventContext, EventHook, EventType

logger = logging.getLogger(__name__)


class NoBackendAvailableError(Exception):
    """Raised when no healthy backend is available."""

    pass


@dataclass
class Backend:
    """
    Represents a backend server.

    Tracks DNS resolution state, failure count, and original configuration order.
    """

    host: str  # Original hostname or IP
    port: int
    host_type: str = "domain"  # "ip" or "domain"
    resolved_ips: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    original_index: int = 0
    marked_unavailable_at: float | None = None  # Timestamp when marked unavailable
    cooldown_seconds: float = 1800.0  # Cooldown period (default: 30 minutes)

    def __repr__(self) -> str:
        ips_str = ",".join(self.resolved_ips) if self.resolved_ips else "unresolved"
        cooldown_str = ""
        if self.marked_unavailable_at is not None:
            import time

            remaining = self.cooldown_seconds - (time.time() - self.marked_unavailable_at)
            if remaining > 0:
                cooldown_str = f", cooldown={remaining:.0f}s"
        return f"Backend({self.host}:{self.port}, ips=[{ips_str}], failures={self.consecutive_failures}{cooldown_str})"


class BackendPool:
    """
    Manages backend servers with sequential failover strategy.

    Features:
    - Sequential connection attempts (first successful wins)
    - DNS cache invalidation on first failure
    - Backend rotation to end of queue after second failure
    - Thread-safe operations for concurrent connections
    """

    def __init__(
        self,
        service_name: str,
        backends: list[str],
        dns_resolver: DNSResolver,
        cooldown_seconds: float = 1800.0,
        protocol: Literal["tcp", "udp", "both"] = "both",
        health_check_interval: float | None = None,
        health_check_timeout: float = 5.0,
        event_hook: EventHook | None = None,
    ):
        """
        Initialize backend pool.

        Args:
            service_name: Name of the service (for logging)
            backends: List of backend strings in "host:port" format
            dns_resolver: DNS resolver instance
            cooldown_seconds: Cooldown period in seconds after second failure (default: 1800)
            protocol: Service protocol type ('tcp', 'udp', or 'both')
            health_check_interval: Health check interval in seconds (None to disable)
            health_check_timeout: Health check timeout in seconds (default: 5)
            event_hook: Event hook instance (optional)
        """
        self.service_name = service_name
        self.dns_resolver = dns_resolver
        self._lock = asyncio.Lock()
        self.cooldown_seconds = cooldown_seconds
        self.protocol = protocol
        self.health_check_interval = health_check_interval
        self.health_check_timeout = health_check_timeout
        self._health_check_task: asyncio.Task[None] | None = None
        self.event_hook = event_hook
        self._all_backends_unavailable = False  # Flag to prevent duplicate events

        # Parse and create backend objects
        self.backends: list[Backend] = []
        for idx, backend_str in enumerate(backends):
            host, port = parse_backend(backend_str)
            # Determine if host is IP or domain name (check once at initialization)
            host_type = "ip" if self.dns_resolver._is_ip_address(host) else "domain"
            backend = Backend(
                host=host,
                port=port,
                host_type=host_type,
                original_index=idx,
                cooldown_seconds=cooldown_seconds,
            )
            self.backends.append(backend)

        logger.info(
            f"[{service_name}] Backend pool initialized with {len(self.backends)} backends "
            f"(cooldown: {cooldown_seconds:.0f}s)"
        )

        # Start health check if enabled (only for TCP services)
        if health_check_interval and protocol in ("tcp", "both"):
            logger.info(
                f"[{service_name}] Health check enabled: "
                f"interval={health_check_interval:.0f}s, timeout={health_check_timeout:.0f}s"
            )
            # Health check task will be started after event loop is running
        elif health_check_interval and protocol == "udp":
            logger.info(f"[{service_name}] Health check disabled for UDP-only service")

    def _is_in_cooldown(self, backend: Backend, current_time: float) -> bool:
        """
        Check if backend is in cooldown period.

        Args:
            backend: Backend to check
            current_time: Current timestamp

        Returns:
            True if backend is in cooldown, False otherwise
        """
        if backend.marked_unavailable_at is None:
            return False

        elapsed = current_time - backend.marked_unavailable_at
        return elapsed < backend.cooldown_seconds

    async def _ensure_resolved(self, backend: Backend) -> None:
        """
        Ensure backend hostname is resolved to IPs.

        Args:
            backend: Backend to resolve
        """
        if not backend.resolved_ips:
            if backend.host_type == "ip":
                # IP address - use directly without DNS resolution
                backend.resolved_ips = [backend.host]
                logger.debug(
                    f"[{self.service_name}] Backend {backend.host}:{backend.port} is IP address"
                )
            else:
                # Domain name - perform DNS resolution
                ips = await self.dns_resolver.resolve(backend.host)
                backend.resolved_ips = ips

                if ips:
                    logger.debug(
                        f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                        f"resolved to {ips}"
                    )
                else:
                    logger.warning(
                        f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                        f"failed to resolve"
                    )

    async def get_backends_in_order(self) -> list[tuple[str, int, Backend]]:
        """
        Get all backends in connection attempt order.

        Returns resolved (IP, port, backend) tuples for all backends that
        successfully resolved. Filters out backends in cooldown period.
        If all backends are in cooldown, returns empty list.

        Returns:
            List of (ip, port, backend) tuples. Empty if all backends unavailable.
        """
        async with self._lock:
            result: list[tuple[str, int, Backend]] = []
            now = time.time()
            unavailable_count = 0

            for backend in self.backends:
                # Ensure DNS is resolved
                await self._ensure_resolved(backend)

                # Skip backends without resolved IPs
                if not backend.resolved_ips:
                    continue

                # Check if in cooldown period
                if self._is_in_cooldown(backend, now):
                    unavailable_count += 1
                    if backend.marked_unavailable_at is not None:
                        remaining = backend.cooldown_seconds - (now - backend.marked_unavailable_at)
                        logger.debug(
                            f"[{self.service_name}] Skipping backend {backend.host}:{backend.port} "
                            f"({remaining:.0f}s remaining in cooldown)"
                        )
                    continue

                # Add to result
                backend_tuple = (backend.resolved_ips[0], backend.port, backend)
                result.append(backend_tuple)

            # Log status
            if unavailable_count > 0:
                if not result:
                    logger.warning(
                        f"[{self.service_name}] All {unavailable_count} backend(s) are unavailable "
                        f"(in cooldown or failed DNS resolution)"
                    )
                    # Trigger all_backends_unavailable event (only once)
                    if not self._all_backends_unavailable:
                        self._all_backends_unavailable = True
                        await self._trigger_event(
                            event_type="all_backends_unavailable",
                            backend=None,
                            available_count=0,
                        )
                else:
                    logger.debug(
                        f"[{self.service_name}] {unavailable_count} backend(s) in cooldown period"
                    )
                    # Reset flag when backends become available again
                    if self._all_backends_unavailable:
                        self._all_backends_unavailable = False

            return result

    async def on_connect_success(self, backend: Backend) -> None:
        """
        Handle successful connection to backend.

        Resets failure counter and clears cooldown status for the backend.

        Args:
            backend: Backend that was successfully connected
        """
        async with self._lock:
            # Check if backend was in cooldown
            was_in_cooldown = backend.marked_unavailable_at is not None

            if was_in_cooldown and backend.marked_unavailable_at is not None:
                unavailable_duration = time.time() - backend.marked_unavailable_at
                logger.info(
                    f"[{self.service_name}] Backend {backend.host}:{backend.port} recovered "
                    f"(was unavailable for {unavailable_duration:.1f}s)"
                )
                backend.marked_unavailable_at = None

                # Trigger backend_recovered event
                await self._trigger_event(
                    event_type="backend_recovered",
                    backend=backend,
                )

            if backend.consecutive_failures > 0:
                logger.info(
                    f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                    f"reconnected successfully (was failing {backend.consecutive_failures} times)"
                )
                backend.consecutive_failures = 0
            elif not was_in_cooldown:
                logger.debug(
                    f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                    f"connected successfully"
                )

    async def on_connect_failure(self, backend: Backend) -> None:
        """
        Handle failed connection to backend.

        Implements two-strike policy:
        - First failure: Clear DNS cache and force re-resolution
        - Second failure: Move backend to end of queue and mark unavailable for cooldown period

        Args:
            backend: Backend that failed to connect
        """
        async with self._lock:
            backend.consecutive_failures += 1

            logger.warning(
                f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                f"connection failed (attempt #{backend.consecutive_failures})"
            )

            if backend.consecutive_failures == 1:
                # First failure: Clear DNS cache and re-resolve (only for domains)
                if backend.host_type == "domain":
                    logger.info(f"[{self.service_name}] Clearing DNS cache for {backend.host}")
                    await self.dns_resolver.clear_cache_async(backend.host)
                backend.resolved_ips.clear()

                # Immediately re-resolve
                await self._ensure_resolved(backend)

            elif backend.consecutive_failures >= 2:
                # Second failure: Move to end of queue and mark unavailable
                now = time.time()
                backend.marked_unavailable_at = now
                cooldown_end_time = datetime.fromtimestamp(now + backend.cooldown_seconds)

                logger.warning(
                    f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                    f"marked unavailable for {backend.cooldown_seconds:.0f}s "
                    f"(until {cooldown_end_time.strftime('%H:%M:%S')})"
                )

                # Trigger backend_failed event
                await self._trigger_event(
                    event_type="backend_failed",
                    backend=backend,
                )

                # Remove from current position and append to end
                self.backends.remove(backend)
                self.backends.append(backend)

                # Reset failure counter for fresh start
                backend.consecutive_failures = 0

                # Log new backend order
                backend_order = [f"{b.host}:{b.port}" for b in self.backends]
                logger.info(
                    f"[{self.service_name}] New backend order: {' -> '.join(backend_order)}"
                )

    async def get_status(self) -> dict[str, Any]:
        """
        Get current pool status.

        Returns:
            Dictionary with pool status information
        """
        async with self._lock:
            backends_info = []
            for idx, backend in enumerate(self.backends):
                backends_info.append(
                    {
                        "position": idx,
                        "host": backend.host,
                        "port": backend.port,
                        "resolved_ips": backend.resolved_ips,
                        "failures": backend.consecutive_failures,
                        "original_index": backend.original_index,
                    }
                )

            return {
                "service": self.service_name,
                "total_backends": len(self.backends),
                "backends": backends_info,
                "health_check_enabled": self._health_check_task is not None,
            }

    async def start_health_check(self) -> None:
        """Start health check task if configured."""
        if self.health_check_interval and self.protocol in ("tcp", "both"):
            if self._health_check_task is None or self._health_check_task.done():
                self._health_check_task = asyncio.create_task(self._health_check_loop())
                logger.info(f"[{self.service_name}] Health check task started")

    async def stop_health_check(self) -> None:
        """Stop health check task."""
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            logger.info(f"[{self.service_name}] Health check task stopped")

    async def _health_check_loop(self) -> None:
        """
        Background task for periodic health checking.

        Probes each backend with TCP connection and updates backend status
        using existing failure/success handlers.
        """
        try:
            logger.info(
                f"[{self.service_name}] Health check loop started "
                f"(interval: {self.health_check_interval}s)"
            )

            while True:
                # Wait for the next check interval
                await asyncio.sleep(self.health_check_interval)  # type: ignore[arg-type]

                # Perform health check on all backends
                await self._perform_health_check()

        except asyncio.CancelledError:
            logger.debug(f"[{self.service_name}] Health check loop cancelled")
            raise

        except Exception as e:
            logger.error(f"[{self.service_name}] Health check loop error: {e}", exc_info=True)

    async def _perform_health_check(self) -> None:
        """
        Perform health check on all backends.

        Skips backends in cooldown period to reduce overhead.
        """
        now = time.time()

        # Get snapshot of backends to check (avoid holding lock during checks)
        async with self._lock:
            backends_to_check = [
                backend for backend in self.backends if not self._is_in_cooldown(backend, now)
            ]

        if not backends_to_check:
            logger.debug(f"[{self.service_name}] Health check: all backends in cooldown, skipping")
            return

        logger.debug(
            f"[{self.service_name}] Health check: probing {len(backends_to_check)} backend(s)"
        )

        # Check each backend
        for backend in backends_to_check:
            await self._check_backend_health(backend)

    async def _check_backend_health(self, backend: Backend) -> None:
        """
        Check health of a single backend using TCP connection.

        Args:
            backend: Backend to check
        """
        # Ensure backend is resolved
        async with self._lock:
            await self._ensure_resolved(backend)

        if not backend.resolved_ips:
            logger.warning(
                f"[{self.service_name}] Health check: {backend.host}:{backend.port} "
                f"has no resolved IPs, skipping"
            )
            return

        # Use first resolved IP for health check
        backend_ip = backend.resolved_ips[0]

        try:
            # Attempt TCP connection with timeout
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(backend_ip, backend.port),
                timeout=self.health_check_timeout,
            )

            # Close connection immediately
            writer.close()
            await writer.wait_closed()

            # Success - update backend status
            await self.on_connect_success(backend)

            logger.debug(
                f"[{self.service_name}] Health check: {backend.host}:{backend.port} "
                f"({backend_ip}) is healthy"
            )

        except TimeoutError:
            logger.warning(
                f"[{self.service_name}] Health check: {backend.host}:{backend.port} "
                f"({backend_ip}) timeout"
            )
            await self.on_connect_failure(backend)

        except (ConnectionRefusedError, OSError) as e:
            logger.warning(
                f"[{self.service_name}] Health check: {backend.host}:{backend.port} "
                f"({backend_ip}) failed: {e}"
            )
            await self.on_connect_failure(backend)

        except Exception as e:
            logger.error(
                f"[{self.service_name}] Health check: {backend.host}:{backend.port} "
                f"unexpected error: {e}",
                exc_info=True,
            )

    async def _trigger_event(
        self,
        event_type: EventType,
        backend: Backend | None,
        available_count: int | None = None,
    ) -> None:
        """
        Trigger event hook if configured.

        Args:
            event_type: Type of event to trigger
            backend: Backend instance (None for all_backends_unavailable)
            available_count: Number of available backends (optional, calculated if not provided)
        """
        if self.event_hook is None:
            return

        # Calculate available count if not provided
        if available_count is None:
            now = time.time()
            available_count = sum(
                1 for b in self.backends if b.resolved_ips and not self._is_in_cooldown(b, now)
            )

        # Build event context
        context = EventContext(
            event_type=event_type,
            service_name=self.service_name,
            backend_host=backend.host if backend else None,
            backend_port=backend.port if backend else None,
            backend_ip=backend.resolved_ips[0] if backend and backend.resolved_ips else None,
            failure_count=backend.consecutive_failures if backend else 0,
            available_count=available_count,
            total_count=len(self.backends),
        )

        # Trigger hook asynchronously
        await self.event_hook.trigger(context)

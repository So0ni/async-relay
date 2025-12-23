"""Backend pool management with failover logic."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from src.config import parse_backend
from src.dns_resolver import DNSResolver

logger = logging.getLogger(__name__)


class NoBackendAvailable(Exception):
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
    resolved_ips: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    original_index: int = 0

    def __repr__(self) -> str:
        ips_str = ','.join(self.resolved_ips) if self.resolved_ips else 'unresolved'
        return f"Backend({self.host}:{self.port}, ips=[{ips_str}], failures={self.consecutive_failures})"


class BackendPool:
    """
    Manages backend servers with sequential failover strategy.

    Features:
    - Sequential connection attempts (first successful wins)
    - DNS cache invalidation on first failure
    - Backend rotation to end of queue after second failure
    - Thread-safe operations for concurrent connections
    """

    def __init__(self, service_name: str, backends: list[str], dns_resolver: DNSResolver):
        """
        Initialize backend pool.

        Args:
            service_name: Name of the service (for logging)
            backends: List of backend strings in "host:port" format
            dns_resolver: DNS resolver instance
        """
        self.service_name = service_name
        self.dns_resolver = dns_resolver
        self._lock = asyncio.Lock()

        # Parse and create backend objects
        self.backends: list[Backend] = []
        for idx, backend_str in enumerate(backends):
            host, port = parse_backend(backend_str)
            backend = Backend(
                host=host,
                port=port,
                original_index=idx,
            )
            self.backends.append(backend)

        logger.info(
            f"[{service_name}] Backend pool initialized with {len(self.backends)} backends"
        )

    async def _ensure_resolved(self, backend: Backend) -> None:
        """
        Ensure backend hostname is resolved to IPs.

        Args:
            backend: Backend to resolve
        """
        if not backend.resolved_ips:
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
        successfully resolved. Returns empty list if no backends are available.

        Returns:
            List of (ip, port, backend) tuples
        """
        async with self._lock:
            result: list[tuple[str, int, Backend]] = []

            for backend in self.backends:
                # Ensure DNS is resolved
                await self._ensure_resolved(backend)

                # Add all resolved IPs (prefer first one)
                if backend.resolved_ips:
                    result.append((
                        backend.resolved_ips[0],
                        backend.port,
                        backend
                    ))

            return result

    async def on_connect_success(self, backend: Backend) -> None:
        """
        Handle successful connection to backend.

        Resets failure counter for the backend.

        Args:
            backend: Backend that was successfully connected
        """
        async with self._lock:
            if backend.consecutive_failures > 0:
                logger.info(
                    f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                    f"reconnected successfully (was failing {backend.consecutive_failures} times)"
                )
                backend.consecutive_failures = 0
            else:
                logger.debug(
                    f"[{self.service_name}] Backend {backend.host}:{backend.port} "
                    f"connected successfully"
                )

    async def on_connect_failure(self, backend: Backend) -> None:
        """
        Handle failed connection to backend.

        Implements two-strike policy:
        - First failure: Clear DNS cache and force re-resolution
        - Second failure: Move backend to end of queue

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
                # First failure: Clear DNS cache and re-resolve
                logger.info(
                    f"[{self.service_name}] Clearing DNS cache for {backend.host}"
                )
                await self.dns_resolver.clear_cache_async(backend.host)
                backend.resolved_ips.clear()

                # Immediately re-resolve
                await self._ensure_resolved(backend)

            elif backend.consecutive_failures >= 2:
                # Second failure: Move to end of queue
                logger.warning(
                    f"[{self.service_name}] Moving backend {backend.host}:{backend.port} "
                    f"to end of queue after {backend.consecutive_failures} failures"
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
                backends_info.append({
                    'position': idx,
                    'host': backend.host,
                    'port': backend.port,
                    'resolved_ips': backend.resolved_ips,
                    'failures': backend.consecutive_failures,
                    'original_index': backend.original_index,
                })

            return {
                'service': self.service_name,
                'total_backends': len(self.backends),
                'backends': backends_info,
            }

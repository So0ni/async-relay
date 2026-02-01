"""DNS resolver with caching and periodic refresh."""

import asyncio
import logging
import socket
import time

logger = logging.getLogger(__name__)


class DNSResolver:
    """
    DNS resolver with TTL-based caching.

    Resolves domain names to IP addresses (both IPv4 and IPv6) and caches
    results for a specified TTL. Supports manual cache invalidation for
    failover scenarios.
    """

    def __init__(self, ttl: int = 3600):
        """
        Initialize DNS resolver.

        Args:
            ttl: Time-to-live for cache entries in seconds (default: 1 hour)
        """
        self.ttl = ttl
        self.cache: dict[str, tuple[list[str], float]] = {}
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

        logger.info(f"DNS resolver initialized with TTL={ttl}s")

    async def start_refresh_task(self) -> None:
        """Start background task to refresh DNS cache periodically."""
        if self._refresh_task is not None:
            logger.warning("Refresh task already running")
            return

        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("DNS cache refresh task started")

    async def stop_refresh_task(self) -> None:
        """Stop background refresh task."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
            logger.info("DNS cache refresh task stopped")

    async def _refresh_loop(self) -> None:
        """Background task to periodically clear cache."""
        try:
            while True:
                await asyncio.sleep(self.ttl)

                async with self._lock:
                    hostnames = list(self.cache.keys())
                    self.cache.clear()

                if hostnames:
                    logger.info(f"DNS cache expired, cleared {len(hostnames)} entries")

        except asyncio.CancelledError:
            logger.debug("DNS refresh loop cancelled")
            raise

    def _is_ip_address(self, hostname: str) -> bool:
        """
        Check if hostname is already an IP address.

        Args:
            hostname: Hostname to check

        Returns:
            True if hostname is an IP address (IPv4 or IPv6)
        """
        # Try IPv4
        try:
            socket.inet_pton(socket.AF_INET, hostname)
            return True
        except OSError:
            pass

        # Try IPv6
        try:
            socket.inet_pton(socket.AF_INET6, hostname)
            return True
        except OSError:
            pass

        return False

    async def resolve(self, hostname: str) -> list[str]:
        """
        Resolve hostname to IP addresses.

        Returns both IPv4 and IPv6 addresses if available. Results are cached
        for the configured TTL period.

        Args:
            hostname: Hostname or IP address to resolve

        Returns:
            List of IP addresses (may be empty if resolution fails)
        """
        # If already an IP address, return as-is
        if self._is_ip_address(hostname):
            return [hostname]

        async with self._lock:
            now = time.time()

            # Check cache
            if hostname in self.cache:
                ips, timestamp = self.cache[hostname]
                if now - timestamp < self.ttl:
                    logger.debug(f"DNS cache hit for '{hostname}': {ips}")
                    return ips.copy()

            # Cache miss or expired - resolve
            logger.debug(f"Resolving DNS for '{hostname}'")

            try:
                loop = asyncio.get_running_loop()
                addrinfo = await loop.getaddrinfo(
                    hostname,
                    None,
                    family=socket.AF_UNSPEC,  # Both IPv4 and IPv6
                    type=socket.SOCK_STREAM,
                )

                # Extract unique IP addresses
                ips = list(set(info[4][0] for info in addrinfo))

                if not ips:
                    logger.warning(f"DNS resolution returned no IPs for '{hostname}'")
                    return []

                # Cache the result
                self.cache[hostname] = (ips, now)

                logger.info(f"DNS resolved '{hostname}' -> {ips}")
                return ips.copy()

            except (socket.gaierror, OSError) as e:
                logger.error(f"DNS resolution failed for '{hostname}': {e}")

                # Return stale cache if available
                if hostname in self.cache:
                    stale_ips, _ = self.cache[hostname]
                    logger.warning(f"Using stale DNS cache for '{hostname}': {stale_ips}")
                    return stale_ips.copy()

                return []

    def clear_cache(self, hostname: str) -> None:
        """
        Clear cache entry for specific hostname.

        Used when connection to a resolved IP fails, forcing re-resolution
        on next attempt.

        Args:
            hostname: Hostname to clear from cache
        """
        if hostname in self.cache:
            del self.cache[hostname]
            logger.info(f"Cleared DNS cache for '{hostname}'")

    async def clear_cache_async(self, hostname: str) -> None:
        """
        Clear cache entry for specific hostname (async version with lock).

        Args:
            hostname: Hostname to clear from cache
        """
        async with self._lock:
            if hostname in self.cache:
                del self.cache[hostname]
                logger.info(f"Cleared DNS cache for '{hostname}'")

    def get_cache_stats(self) -> dict[str, int]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        return {
            "total_entries": len(self.cache),
            "ttl_seconds": self.ttl,
        }

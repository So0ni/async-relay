"""Service manager for managing multiple relay services."""

import asyncio
import logging
import signal
from typing import Any

from src.backend_pool import BackendPool
from src.config import Config
from src.dns_resolver import DNSResolver
from src.relay_service import RelayService

logger = logging.getLogger(__name__)


class ServiceManager:
    """
    Manages multiple relay services.

    Coordinates DNS resolver, backend pools, and relay services.
    Handles graceful shutdown on signals.
    """

    def __init__(self, config: Config):
        """
        Initialize service manager.

        Args:
            config: Application configuration
        """
        self.config = config
        self.dns_resolver = DNSResolver(ttl=3600)  # 1 hour DNS cache
        self.services: list[RelayService] = []
        self._shutdown_event = asyncio.Event()

        logger.info("Service manager initialized")

    async def start(self) -> None:
        """
        Start all configured services.

        Sets up signal handlers and starts DNS refresh task.
        """
        # Start DNS resolver refresh task
        await self.dns_resolver.start_refresh_task()

        # Create services
        for service_config in self.config.services:
            try:
                # Create backend pool
                backend_pool = BackendPool(
                    service_name=service_config.name,
                    backends=service_config.backends,
                    dns_resolver=self.dns_resolver,
                )

                # Create relay service
                relay_service = RelayService(
                    name=service_config.name,
                    listen_addr=service_config.listen.address,
                    listen_port=service_config.listen.port,
                    backend_pool=backend_pool,
                    protocol=service_config.protocol,
                )

                self.services.append(relay_service)

                logger.info(
                    f"Created service '{service_config.name}' on "
                    f"{service_config.listen.address}:{service_config.listen.port} "
                    f"({service_config.protocol})"
                )

            except Exception as e:
                logger.error(
                    f"Failed to create service '{service_config.name}': {e}",
                    exc_info=True
                )
                raise

        if not self.services:
            raise RuntimeError("No services configured")

        # Setup signal handlers
        self._setup_signal_handlers()

        # Start all services
        service_tasks = [
            asyncio.create_task(service.start())
            for service in self.services
        ]

        logger.info(f"Started {len(self.services)} service(s)")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        logger.info("Shutdown signal received, stopping services")

        # Cancel all service tasks
        for task in service_tasks:
            task.cancel()

        # Wait for services to stop
        await asyncio.gather(*service_tasks, return_exceptions=True)

        # Stop services gracefully
        await self._stop_all_services()

    async def _stop_all_services(self) -> None:
        """Stop all services and cleanup resources."""
        logger.info("Stopping all services")

        # Stop all relay services
        stop_tasks = [service.stop() for service in self.services]
        await asyncio.gather(*stop_tasks, return_exceptions=True)

        # Stop DNS resolver
        await self.dns_resolver.stop_refresh_task()

        logger.info("All services stopped")

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        def signal_handler(signame: str) -> None:
            logger.info(f"Received signal {signame}")
            self._shutdown_event.set()

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: signal_handler(signal.Signals(s).name)
                )
            logger.debug("Signal handlers registered")
        except NotImplementedError:
            # Signal handlers not supported on this platform (e.g., Windows)
            logger.warning("Signal handlers not supported on this platform")

    async def get_status(self) -> dict[str, Any]:
        """
        Get status of all services.

        Returns:
            Dictionary with service status information
        """
        services_status = []

        for service in self.services:
            services_status.append({
                'name': service.name,
                'listen': f"{service.listen_addr}:{service.listen_port}",
                'stats': service.stats.copy(),
                'backend_pool': await service.pool.get_status(),
            })

        return {
            'total_services': len(self.services),
            'dns_cache': self.dns_resolver.get_cache_stats(),
            'services': services_status,
        }

"""Service manager for managing multiple relay services."""

import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import Any, Literal

from src.backend_pool import BackendPool
from src.config import Config, ServiceConfig, load_config
from src.config_watcher import ConfigWatcher
from src.dns_resolver import DNSResolver
from src.relay_service import RelayService

logger = logging.getLogger(__name__)


@dataclass
class ServiceComparison:
    """Result of comparing old and new service configuration."""

    name: str
    status: Literal["unchanged", "modified", "added", "removed"]
    old_config: ServiceConfig | None
    new_config: ServiceConfig | None


class ServiceManager:
    """
    Manages multiple relay services.

    Coordinates DNS resolver, backend pools, and relay services.
    Handles graceful shutdown on signals.
    """

    def __init__(
        self,
        config: Config,
        config_path: str | None = None,
        enable_reload: bool = True,
        reload_delay: float = 10.0,
    ):
        """
        Initialize service manager.

        Args:
            config: Application configuration
            config_path: Path to config file (for hot reload)
            enable_reload: Enable configuration hot reload
            reload_delay: Debounce delay in seconds for config reload
        """
        self.config = config
        self.dns_resolver = DNSResolver(ttl=3600)  # 1 hour DNS cache
        self.services: list[RelayService] = []
        self._services_dict: dict[str, RelayService] = {}  # Service lookup by name
        self._shutdown_event = asyncio.Event()

        # Config reload support
        self._config_path = config_path
        self._enable_reload = enable_reload and config_path is not None
        self._reload_delay = reload_delay
        self._config_watcher: ConfigWatcher | None = None
        self._reload_lock = asyncio.Lock()

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
                # Parse health check configuration
                health_check_interval = None
                health_check_timeout = 5.0
                if service_config.health_check and service_config.health_check.enabled:
                    health_check_interval = service_config.health_check.interval
                    health_check_timeout = service_config.health_check.timeout

                # Create backend pool
                backend_pool = BackendPool(
                    service_name=service_config.name,
                    backends=service_config.backends,
                    dns_resolver=self.dns_resolver,
                    cooldown_seconds=service_config.backend_cooldown,
                    protocol=service_config.protocol,
                    health_check_interval=health_check_interval,
                    health_check_timeout=health_check_timeout,
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
                self._services_dict[service_config.name] = relay_service

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

        # Start config file watcher if enabled
        if self._enable_reload:
            self._start_config_watcher()

        # Start health check tasks for all services
        for service in self.services:
            await service.pool.start_health_check()

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

        # Stop config watcher
        if self._config_watcher:
            self._config_watcher.stop()
            self._config_watcher = None

        # Stop health check tasks
        health_check_tasks = [service.pool.stop_health_check() for service in self.services]
        await asyncio.gather(*health_check_tasks, return_exceptions=True)

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
                    lambda s=sig: signal_handler(signal.Signals(s).name)  # type: ignore[misc]
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

    def _start_config_watcher(self) -> None:
        """Start configuration file watcher."""
        if not self._config_path:
            return

        self._config_watcher = ConfigWatcher(
            config_path=self._config_path,
            on_change_callback=self._on_config_change,
            debounce_seconds=self._reload_delay,
        )
        self._config_watcher.start()

        logger.info(
            f"Config file watcher enabled (reload-delay: {self._reload_delay}s)"
        )

    async def _on_config_change(self) -> None:
        """Callback invoked when config file changes."""
        try:
            await self.reload_config()
        except Exception as e:
            logger.error(
                f"Failed to reload configuration: {e}",
                exc_info=True
            )

    async def reload_config(self) -> None:
        """
        Reload configuration from file and apply changes.

        This method:
        1. Loads and validates new configuration
        2. Compares with current configuration
        3. Restarts only modified services
        4. Keeps unchanged services running
        """
        # Use lock to prevent concurrent reloads
        async with self._reload_lock:
            logger.info(f"Loading configuration from: {self._config_path}")

            try:
                # Load new configuration
                new_config = load_config(self._config_path)  # type: ignore[arg-type]
                logger.info("Configuration parsed successfully")

            except Exception as e:
                logger.error(
                    f"Failed to load configuration: {e}",
                    exc_info=True
                )
                logger.error("Configuration validation failed, keeping current config")
                return

            # Compare configurations
            comparisons = self._compare_configs(self.config, new_config)

            # Count changes
            unchanged = sum(1 for c in comparisons if c.status == "unchanged")
            modified = sum(1 for c in comparisons if c.status == "modified")
            added = sum(1 for c in comparisons if c.status == "added")
            removed = sum(1 for c in comparisons if c.status == "removed")

            # Log comparison results
            logger.info("Config comparison results:")
            logger.info(f"  - Unchanged: {unchanged} service(s)")
            logger.info(f"  - Modified: {modified} service(s)")
            logger.info(f"  - Added: {added} service(s)")
            logger.info(f"  - Removed: {removed} service(s)")

            # If nothing changed, skip reload
            if modified == 0 and added == 0 and removed == 0:
                logger.info("No configuration changes detected, skipping reload")
                return

            # Apply changes
            await self._apply_config_changes(comparisons)

            # Update current config
            self.config = new_config

            logger.info("Config reload completed successfully")

    def _compare_configs(
        self,
        old_config: Config,
        new_config: Config,
    ) -> list[ServiceComparison]:
        """
        Compare old and new configurations.

        Args:
            old_config: Current configuration
            new_config: New configuration

        Returns:
            List of service comparisons
        """
        old_services = {s.name: s for s in old_config.services}
        new_services = {s.name: s for s in new_config.services}

        comparisons = []
        all_service_names = set(old_services.keys()) | set(new_services.keys())

        for name in sorted(all_service_names):
            old_svc = old_services.get(name)
            new_svc = new_services.get(name)

            status: Literal["unchanged", "modified", "added", "removed"]
            if old_svc and new_svc:
                # Service exists in both - check if modified
                if self._compare_service_config(old_svc, new_svc):
                    status = "unchanged"
                else:
                    status = "modified"
            elif new_svc:
                # New service
                status = "added"
            else:
                # Service removed
                status = "removed"

            comparisons.append(
                ServiceComparison(
                    name=name,
                    status=status,
                    old_config=old_svc,
                    new_config=new_svc,
                )
            )

        return comparisons

    def _compare_service_config(
        self,
        old: ServiceConfig,
        new: ServiceConfig,
    ) -> bool:
        """
        Compare two service configurations for equality.

        Args:
            old: Old service configuration
            new: New service configuration

        Returns:
            True if configurations are identical, False otherwise
        """
        # Compare basic fields
        if (
            old.listen.address != new.listen.address
            or old.listen.port != new.listen.port
            or old.protocol != new.protocol
            or old.backends != new.backends
            or old.backend_cooldown != new.backend_cooldown
        ):
            return False

        # Compare health check configuration
        old_hc = old.health_check
        new_hc = new.health_check

        # Both None or both not None
        if (old_hc is None) != (new_hc is None):
            return False

        # If both are not None, compare fields
        if old_hc is not None and new_hc is not None:
            if (
                old_hc.enabled != new_hc.enabled
                or old_hc.interval != new_hc.interval
                or old_hc.timeout != new_hc.timeout
            ):
                return False

        return True

    async def _apply_config_changes(
        self,
        comparisons: list[ServiceComparison],
    ) -> None:
        """
        Apply configuration changes to services.

        Args:
            comparisons: List of service comparisons
        """
        for comparison in comparisons:
            try:
                if comparison.status == "unchanged":
                    # Keep service running
                    logger.debug(f"Service '{comparison.name}' unchanged, keeping running")
                    continue

                elif comparison.status == "removed":
                    # Stop and remove service
                    logger.info(f"Stopping service: {comparison.name} (removed)")
                    service = self._services_dict.get(comparison.name)
                    if service:
                        # Stop health check first
                        await service.pool.stop_health_check()
                        # Stop relay service
                        await service.stop()
                        self.services.remove(service)
                        del self._services_dict[comparison.name]
                        logger.info(f"Service '{comparison.name}' stopped and removed")

                elif comparison.status == "modified":
                    # Stop old service, start new service
                    logger.info(f"Restarting service: {comparison.name} (modified)")

                    # Stop old service
                    old_service = self._services_dict.get(comparison.name)
                    if old_service:
                        # Stop health check first
                        await old_service.pool.stop_health_check()
                        # Stop relay service
                        await old_service.stop()
                        self.services.remove(old_service)
                        logger.info(f"Service '{comparison.name}' stopped")

                    # Start new service
                    if comparison.new_config:
                        new_service = await self._create_service(comparison.new_config)
                        self.services.append(new_service)
                        self._services_dict[comparison.name] = new_service

                        # Start health check
                        await new_service.pool.start_health_check()
                        # Start service in background
                        asyncio.create_task(new_service.start())
                        logger.info(f"Service '{comparison.name}' restarted with new config")

                elif comparison.status == "added":
                    # Create and start new service
                    logger.info(f"Starting new service: {comparison.name}")
                    if comparison.new_config:
                        new_service = await self._create_service(comparison.new_config)
                        self.services.append(new_service)
                        self._services_dict[comparison.name] = new_service

                        # Start health check
                        await new_service.pool.start_health_check()
                        # Start service in background
                        asyncio.create_task(new_service.start())
                        logger.info(f"Service '{comparison.name}' started")

            except Exception as e:
                logger.error(
                    f"Failed to apply changes to service '{comparison.name}': {e}",
                    exc_info=True
                )

    async def _create_service(self, service_config: ServiceConfig) -> RelayService:
        """
        Create a relay service from configuration.

        Args:
            service_config: Service configuration

        Returns:
            Created relay service
        """
        # Parse health check configuration
        health_check_interval = None
        health_check_timeout = 5.0
        if service_config.health_check and service_config.health_check.enabled:
            health_check_interval = service_config.health_check.interval
            health_check_timeout = service_config.health_check.timeout

        # Create backend pool
        backend_pool = BackendPool(
            service_name=service_config.name,
            backends=service_config.backends,
            dns_resolver=self.dns_resolver,
            cooldown_seconds=service_config.backend_cooldown,
            protocol=service_config.protocol,
            health_check_interval=health_check_interval,
            health_check_timeout=health_check_timeout,
        )

        # Create relay service
        relay_service = RelayService(
            name=service_config.name,
            listen_addr=service_config.listen.address,
            listen_port=service_config.listen.port,
            backend_pool=backend_pool,
            protocol=service_config.protocol,
        )

        logger.debug(
            f"Created service '{service_config.name}' on "
            f"{service_config.listen.address}:{service_config.listen.port} "
            f"({service_config.protocol})"
        )

        return relay_service

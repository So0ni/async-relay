"""Web API server for runtime configuration management."""

import json
import logging

from aiohttp import web
from aiohttp_basicauth import BasicAuthMiddleware  # type: ignore[import-untyped]

from src.app.service_manager import ServiceManager
from src.config.runtime import RuntimeConfigManager

logger = logging.getLogger(__name__)


class WebUIServer:
    """Web UI and API server for configuration management."""

    def __init__(
        self,
        service_manager: ServiceManager,
        runtime_config_manager: RuntimeConfigManager,
        listen_address: str = "127.0.0.1",
        port: int = 8088,
        auth_enabled: bool = False,
        username: str | None = None,
        password: str | None = None,
    ):
        """
        Initialize Web UI server.

        Args:
            service_manager: Service manager instance
            runtime_config_manager: Runtime config manager
            listen_address: Address to bind to
            port: Port to listen on
            auth_enabled: Enable HTTP Basic Auth
            username: Basic auth username
            password: Basic auth password
        """
        self.service_manager = service_manager
        self.runtime_config_manager = runtime_config_manager
        self.listen_address = listen_address
        self.port = port
        self.auth_enabled = auth_enabled
        self.username = username
        self.password = password

        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def start(self) -> None:
        """Start web server."""
        # Create app
        middlewares = []

        if self.auth_enabled and self.username and self.password:
            # Add basic auth middleware
            auth_middleware = BasicAuthMiddleware(
                username=self.username,
                password=self.password,
                force=True,
            )
            middlewares.append(auth_middleware)
            logger.info("HTTP Basic Auth enabled")

        self.app = web.Application(middlewares=middlewares)

        # Setup routes
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/api/status", self._handle_status)
        self.app.router.add_get("/api/config", self._handle_get_config)
        self.app.router.add_put("/api/config", self._handle_update_config)
        self.app.router.add_get("/api/config/source", self._handle_get_source)
        self.app.router.add_post("/api/config/reload", self._handle_reload_config)
        self.app.router.add_post("/api/test-backend", self._handle_test_backend)

        # Start server
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.site = web.TCPSite(
            self.runner,
            self.listen_address,
            self.port,
        )
        await self.site.start()

        logger.info(f"Web UI server started at http://{self.listen_address}:{self.port}")

    async def stop(self) -> None:
        """Stop web server."""
        if self.runner:
            await self.runner.cleanup()
            logger.info("Web UI server stopped")

    # ========== Route Handlers ==========

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the main UI page."""
        html = self._get_ui_html()
        return web.Response(text=html, content_type="text/html")

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Get service status."""
        try:
            status = await self.service_manager.get_status()
            return web.json_response(status)
        except Exception as e:
            logger.error(f"Failed to get status: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        """Get current runtime configuration."""
        try:
            config_dict = self.runtime_config_manager.get_config_dict()
            source = self.runtime_config_manager.get_config_source()

            return web.json_response(
                {
                    "config": config_dict,
                    "source": source,
                }
            )
        except Exception as e:
            logger.error(f"Failed to get config: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_config(self, request: web.Request) -> web.Response:
        """Update runtime configuration."""
        try:
            # Parse request body
            config_dict = await request.json()

            # Validate config structure (basic check)
            if "services" not in config_dict:
                return web.json_response(
                    {"error": 'Missing "services" key in configuration'}, status=400
                )

            # Save to runtime config
            success = self.runtime_config_manager.save_runtime_config(config_dict)
            if not success:
                return web.json_response(
                    {"error": "Failed to save runtime configuration"}, status=500
                )

            # Trigger reload
            try:
                await self.service_manager.reload_config()
                logger.info("Configuration updated from Web UI")
                return web.json_response(
                    {"status": "success", "message": "Configuration updated and reloaded"}
                )
            except Exception as e:
                logger.error(f"Failed to reload config: {e}", exc_info=True)
                return web.json_response(
                    {"error": f"Config saved but reload failed: {e}"}, status=500
                )

        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)
        except Exception as e:
            logger.error(f"Failed to update config: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_source(self, request: web.Request) -> web.Response:
        """Get configuration source info."""
        try:
            source = self.runtime_config_manager.get_config_source()
            return web.json_response(
                {
                    "source": source,
                    "config_path": str(self.runtime_config_manager.config_path),
                    "runtime_path": str(self.runtime_config_manager.runtime_path),
                }
            )
        except Exception as e:
            logger.error(f"Failed to get source: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_reload_config(self, request: web.Request) -> web.Response:
        """Manually reload configuration from config.yaml."""
        try:
            await self.service_manager.reload_config()
            return web.json_response(
                {"status": "success", "message": "Configuration reloaded from config.yaml"}
            )
        except Exception as e:
            logger.error(f"Failed to reload config: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_test_backend(self, request: web.Request) -> web.Response:
        """Test TCP connectivity to a backend server."""
        try:
            import asyncio
            import socket
            import time

            # Parse request
            data = await request.json()
            backend = data.get("backend", "")

            if not backend:
                return web.json_response({"error": "Backend address required"}, status=400)

            # Parse host:port
            try:
                # Handle IPv6 format [host]:port
                if backend.startswith("["):
                    end_bracket = backend.find("]")
                    if end_bracket == -1:
                        raise ValueError("Invalid IPv6 format")
                    host = backend[1:end_bracket]
                    port_str = backend[end_bracket + 2 :]  # Skip ']:'
                else:
                    # IPv4 or domain format
                    parts = backend.rsplit(":", 1)
                    if len(parts) != 2:
                        raise ValueError("Invalid backend format (expected host:port)")
                    host, port_str = parts

                port = int(port_str)
                if not (1 <= port <= 65535):
                    raise ValueError("Port must be between 1 and 65535")

            except ValueError as e:
                return web.json_response(
                    {"error": f"Invalid backend format: {e}"}, status=400
                )

            # Perform TCP connection test
            start_time = time.time()

            try:
                # Resolve DNS if needed
                try:
                    addr_info = await asyncio.get_event_loop().getaddrinfo(
                        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
                    )
                    if not addr_info:
                        return web.json_response(
                            {"success": False, "error": "DNS resolution failed"}, status=200
                        )

                    # Use first resolved address
                    resolved_host = addr_info[0][4][0]

                except Exception as e:
                    return web.json_response(
                        {"success": False, "error": f"DNS error: {e}"}, status=200
                    )

                # Test TCP connection
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(resolved_host, port), timeout=5.0
                    )

                    # Successfully connected
                    latency_ms = round((time.time() - start_time) * 1000, 2)

                    # Close connection
                    writer.close()
                    await writer.wait_closed()

                    return web.json_response(
                        {
                            "success": True,
                            "latency_ms": latency_ms,
                            "message": f"Connected to {host}:{port}",
                        }
                    )

                except TimeoutError:
                    return web.json_response(
                        {"success": False, "error": "Connection timeout (5s)"}, status=200
                    )
                except ConnectionRefusedError:
                    return web.json_response(
                        {"success": False, "error": "Connection refused"}, status=200
                    )
                except Exception as e:
                    return web.json_response(
                        {"success": False, "error": f"Connection error: {e}"}, status=200
                    )

            except Exception as e:
                logger.error(f"Backend test error: {e}", exc_info=True)
                return web.json_response(
                    {"success": False, "error": f"Test failed: {e}"}, status=200
                )

        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)
        except Exception as e:
            logger.error(f"Failed to test backend: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    def _get_ui_html(self) -> str:
        """Get the UI HTML content from static file."""
        try:
            from pathlib import Path

            # Get the path to static/index.html
            static_dir = Path(__file__).parent.parent / "static"
            index_path = static_dir / "index.html"

            if not index_path.exists():
                logger.error(f"Static file not found: {index_path}")
                return "<html><body><h1>Error: UI file not found</h1></body></html>"

            with open(index_path, encoding="utf-8") as f:
                return f.read()

        except Exception as e:
            logger.error(f"Failed to load UI HTML: {e}", exc_info=True)
            return f"<html><body><h1>Error loading UI: {e}</h1></body></html>"

"""Configuration loader and validator."""

import logging
from pathlib import Path

import yaml

from src.config.models import (
    Config,
    EventHookConfig,
    HealthCheckConfig,
    ListenConfig,
    ServiceConfig,
    WebUIConfig,
)

logger = logging.getLogger(__name__)


def parse_backend(backend_str: str) -> tuple[str, int]:
    """
    Parse backend configuration string.

    Supports formats:
    - example.com:80
    - 192.168.1.1:80
    - [2001:db8::1]:80 (IPv6)

    Args:
        backend_str: Backend string in format "host:port"

    Returns:
        Tuple of (host, port)

    Raises:
        ValueError: If format is invalid
    """
    try:
        if backend_str.startswith("["):
            # IPv6 format: [host]:port
            if "]:" not in backend_str:
                raise ValueError(f"Invalid IPv6 backend format: {backend_str}")
            host, port = backend_str.rsplit("]:", 1)
            return (host[1:], int(port))
        else:
            # IPv4 or domain format: host:port
            if ":" not in backend_str:
                raise ValueError(f"Invalid backend format (missing port): {backend_str}")
            host, port = backend_str.rsplit(":", 1)
            return (host, int(port))
    except (ValueError, IndexError) as e:
        raise ValueError(f"Invalid backend format '{backend_str}': {e}") from e


def load_config(config_path: str | Path) -> Config:
    """
    Load and validate configuration from YAML file.

    Args:
        config_path: Path to configuration file

    Returns:
        Parsed configuration

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If configuration is invalid
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    logger.info(f"Loading configuration from {config_path}")

    with open(config_path, encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    if not raw_config or "services" not in raw_config:
        raise ValueError("Configuration must contain 'services' section")

    services: list[ServiceConfig] = []

    for idx, svc_data in enumerate(raw_config["services"]):
        try:
            # Validate required fields
            if "name" not in svc_data:
                raise ValueError("Service must have 'name' field")
            if "listen" not in svc_data:
                raise ValueError("Service must have 'listen' field")
            if "backends" not in svc_data or not svc_data["backends"]:
                raise ValueError("Service must have at least one backend")

            # Parse listen config
            listen_data = svc_data["listen"]
            if "address" not in listen_data or "port" not in listen_data:
                raise ValueError("Listen config must have 'address' and 'port'")

            listen_config = ListenConfig(
                address=listen_data["address"], port=int(listen_data["port"])
            )

            # Validate backends format
            backends = svc_data["backends"]
            for backend in backends:
                parse_backend(backend)  # Validate format

            # Parse protocol (default: both)
            protocol = svc_data.get("protocol", "both").lower()
            if protocol not in ("tcp", "udp", "both"):
                raise ValueError(f"Invalid protocol '{protocol}', must be 'tcp', 'udp', or 'both'")

            # Parse backend cooldown (default: 1800 seconds / 30 minutes)
            backend_cooldown = float(svc_data.get("backend_cooldown", 1800.0))
            if backend_cooldown < 0:
                raise ValueError(f"Invalid backend_cooldown '{backend_cooldown}', must be >= 0")

            # Parse health check configuration (optional)
            health_check_config: HealthCheckConfig | None = None
            if "health_check" in svc_data:
                hc_data = svc_data["health_check"]
                if not isinstance(hc_data, dict):
                    raise ValueError("health_check must be a dictionary")

                enabled = bool(hc_data.get("enabled", False))
                interval = float(hc_data.get("interval", 60.0))
                timeout = float(hc_data.get("timeout", 5.0))

                if interval <= 0:
                    raise ValueError(f"Invalid health_check interval '{interval}', must be > 0")
                if timeout <= 0 or timeout > interval:
                    raise ValueError(
                        f"Invalid health_check timeout '{timeout}', must be > 0 and <= interval"
                    )

                health_check_config = HealthCheckConfig(
                    enabled=enabled,
                    interval=interval,
                    timeout=timeout,
                )

            # Parse event hook configuration (optional)
            event_hook_config: EventHookConfig | None = None
            if "event_hook" in svc_data:
                hook_data = svc_data["event_hook"]
                if not isinstance(hook_data, dict):
                    raise ValueError("event_hook must be a dictionary")

                if "command" not in hook_data:
                    raise ValueError("event_hook must have 'command' field")

                command = str(hook_data["command"])
                args = hook_data.get("args", [])
                events = hook_data.get("events", [])
                timeout = float(hook_data.get("timeout", 30.0))

                if not isinstance(args, list):
                    raise ValueError("event_hook 'args' must be a list")
                if not isinstance(events, list):
                    raise ValueError("event_hook 'events' must be a list")
                if timeout <= 0:
                    raise ValueError(f"Invalid event_hook timeout '{timeout}', must be > 0")

                # Validate event types
                valid_events = {"backend_failed", "all_backends_unavailable", "backend_recovered"}
                for event in events:
                    if event not in valid_events:
                        raise ValueError(
                            f"Invalid event type '{event}', must be one of: {', '.join(valid_events)}"
                        )

                event_hook_config = EventHookConfig(
                    command=command,
                    args=args,
                    events=events,
                    timeout=timeout,
                )

            service = ServiceConfig(
                name=svc_data["name"],
                listen=listen_config,
                backends=backends,
                protocol=protocol,
                backend_cooldown=backend_cooldown,
                health_check=health_check_config,
                event_hook=event_hook_config,
            )

            services.append(service)
            logger.info(
                f"Loaded service '{service.name}': "
                f"{service.listen.address}:{service.listen.port} ({protocol}) -> "
                f"{len(service.backends)} backends"
            )

        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"Invalid configuration for service #{idx}: {e}") from e

    if not services:
        raise ValueError("No valid services configured")

    # Parse Web UI configuration (optional)
    web_ui_config = WebUIConfig()
    if "web_ui" in raw_config:
        ui_data = raw_config["web_ui"]
        if not isinstance(ui_data, dict):
            raise ValueError("web_ui must be a dictionary")

        web_ui_config = WebUIConfig(
            enabled=bool(ui_data.get("enabled", False)),
            listen_address=str(ui_data.get("listen_address", "127.0.0.1")),
            port=int(ui_data.get("port", 8088)),
            auth_enabled=bool(ui_data.get("auth", {}).get("enabled", False)),
            username=ui_data.get("auth", {}).get("username"),
            password=ui_data.get("auth", {}).get("password"),
        )

        # Validate auth config
        if web_ui_config.auth_enabled:
            if not web_ui_config.username or not web_ui_config.password:
                raise ValueError("web_ui.auth requires both 'username' and 'password' when enabled")

        logger.info(
            f"Web UI configured: {web_ui_config.listen_address}:{web_ui_config.port} "
            f"(auth: {web_ui_config.auth_enabled})"
        )

    logger.info(f"Successfully loaded {len(services)} service(s)")
    return Config(services=services, web_ui=web_ui_config)

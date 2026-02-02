"""Configuration models."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ListenConfig:
    """Listen address configuration."""

    address: str
    port: int


@dataclass
class HealthCheckConfig:
    """Health check configuration."""

    enabled: bool = False
    interval: float = 60.0  # Health check interval in seconds (default: 60s)
    timeout: float = 5.0  # Single health check timeout (default: 5s)


@dataclass
class EventHookConfig:
    """Event hook configuration."""

    command: str  # Command to execute (binary or shell script)
    args: list[str] = field(default_factory=list)  # Command arguments
    events: list[str] = field(default_factory=list)  # List of events to subscribe to
    timeout: float = 30.0  # Command execution timeout in seconds (default: 30s)


@dataclass
class WebUIConfig:
    """Web UI configuration."""

    enabled: bool = False
    listen_address: str = "127.0.0.1"
    port: int = 8088
    auth_enabled: bool = False
    username: str | None = None
    password: str | None = None


@dataclass
class ServiceConfig:
    """Service configuration."""

    name: str
    listen: ListenConfig
    backends: list[str]
    protocol: Literal["tcp", "udp", "both"] = "both"
    backend_cooldown: float = 1800.0  # Cooldown period in seconds (default: 30 minutes)
    health_check: HealthCheckConfig | None = None  # Health check configuration (optional)
    event_hook: EventHookConfig | None = None  # Event hook configuration (optional)


@dataclass
class Config:
    """Root configuration."""

    services: list[ServiceConfig]
    web_ui: WebUIConfig = field(default_factory=WebUIConfig)

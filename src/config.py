"""Configuration loader and validator."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ListenConfig:
    """Listen address configuration."""
    address: str
    port: int


@dataclass
class ServiceConfig:
    """Service configuration."""
    name: str
    listen: ListenConfig
    backends: list[str]
    protocol: Literal["tcp", "udp", "both"] = "both"


@dataclass
class Config:
    """Root configuration."""
    services: list[ServiceConfig]


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
        if backend_str.startswith('['):
            # IPv6 format: [host]:port
            if ']:' not in backend_str:
                raise ValueError(f"Invalid IPv6 backend format: {backend_str}")
            host, port = backend_str.rsplit(']:', 1)
            return (host[1:], int(port))
        else:
            # IPv4 or domain format: host:port
            if ':' not in backend_str:
                raise ValueError(f"Invalid backend format (missing port): {backend_str}")
            host, port = backend_str.rsplit(':', 1)
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

    with open(config_path, 'r', encoding='utf-8') as f:
        raw_config = yaml.safe_load(f)

    if not raw_config or 'services' not in raw_config:
        raise ValueError("Configuration must contain 'services' section")

    services: list[ServiceConfig] = []

    for idx, svc_data in enumerate(raw_config['services']):
        try:
            # Validate required fields
            if 'name' not in svc_data:
                raise ValueError("Service must have 'name' field")
            if 'listen' not in svc_data:
                raise ValueError("Service must have 'listen' field")
            if 'backends' not in svc_data or not svc_data['backends']:
                raise ValueError("Service must have at least one backend")

            # Parse listen config
            listen_data = svc_data['listen']
            if 'address' not in listen_data or 'port' not in listen_data:
                raise ValueError("Listen config must have 'address' and 'port'")

            listen_config = ListenConfig(
                address=listen_data['address'],
                port=int(listen_data['port'])
            )

            # Validate backends format
            backends = svc_data['backends']
            for backend in backends:
                parse_backend(backend)  # Validate format

            # Parse protocol (default: both)
            protocol = svc_data.get('protocol', 'both').lower()
            if protocol not in ('tcp', 'udp', 'both'):
                raise ValueError(
                    f"Invalid protocol '{protocol}', must be 'tcp', 'udp', or 'both'"
                )

            service = ServiceConfig(
                name=svc_data['name'],
                listen=listen_config,
                backends=backends,
                protocol=protocol,
            )

            services.append(service)
            logger.info(
                f"Loaded service '{service.name}': "
                f"{service.listen.address}:{service.listen.port} ({protocol}) -> "
                f"{len(service.backends)} backends"
            )

        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid configuration for service #{idx}: {e}"
            ) from e

    if not services:
        raise ValueError("No valid services configured")

    logger.info(f"Successfully loaded {len(services)} service(s)")
    return Config(services=services)

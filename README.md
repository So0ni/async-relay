# async-relay

A high-performance TCP/UDP relay service with automatic failover, built with Python's `asyncio` and `uvloop`.

## Features

- 🚀 **Dual Protocol Support**: Simultaneously forwards both TCP and UDP traffic on the same port (configurable)
- ⚡ **High Performance**: Built with `asyncio` and `uvloop` for 2-4x performance improvement
- 🔄 **Automatic Failover**: Sequential backend connection attempts with intelligent failure handling
- 🌐 **IPv4/IPv6 Compatible**: Full support for both IPv4 and IPv6 addresses
- 🔍 **DNS Resolution**: Automatic domain name resolution with hourly cache refresh
- 💪 **Smart Failure Recovery**:
  - First failure: Clear DNS cache and retry
  - Second failure: Move backend to end of queue and enter cooldown period
  - Backend cooldown: Failed backends are temporarily skipped (configurable, default: 30 minutes)
  - Automatic recovery: Backends are retried after cooldown expires or on successful reconnection
- 🛡️ **Resource Management**: Proper connection cleanup and timeout handling
- 📝 **Comprehensive Logging**: Detailed logging of all key events
- ⚙️ **Flexible Configuration**: YAML-based configuration with protocol selection (tcp/udp/both)
- 🔥 **Hot Reload**: Configuration file changes are automatically detected and applied (10s debounce)
- 🎛️ **Web UI Management** (Optional): Web-based configuration interface with runtime modifications
  - Runtime configuration changes (no restart required)
  - Config file remains authoritative (manual edits override UI changes)
  - Optional HTTP Basic Authentication

## Requirements

- Python 3.11 or higher
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Quick Start

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone https://github.com/So0ni/async-relay.git
cd async-relay
uv sync

# Edit configuration
vim config/config.yaml

# Run the service
uv run async-relay -c config/config.yaml
```

## Installation

### Using uv (Recommended)

1. Install uv if you haven't already:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Clone the repository:
```bash
git clone https://github.com/So0ni/async-relay.git
cd async-relay
```

3. Install dependencies:
```bash
uv sync
```

### Using pip

```bash
pip install -r requirements.txt
```

## Configuration

Create a configuration file in YAML format (see [config/config.yaml](config/config.yaml) for example):

```yaml
# Optional: Enable Web UI for runtime configuration management
web_ui:
  enabled: true              # Enable Web UI (default: false)
  listen_address: "127.0.0.1" # Bind address (default: 127.0.0.1)
  port: 8088                 # Web UI port (default: 8088)
  auth:                      # Optional HTTP Basic Auth
    enabled: false
    username: "admin"
    password: "changeme"

services:
  - name: "web-proxy"
    protocol: "both"     # tcp, udp, or both (default: both)
    listen:
      address: "::"      # IPv6 (also accepts IPv4)
      port: 8080
    backends:
      - "example.com:80"           # Domain name
      - "192.168.1.10:80"          # IPv4 address
      - "[2001:db8::1]:80"         # IPv6 address
      - "backup.example.com:80"    # Backup backend
    backend_cooldown: 1800  # Optional: cooldown period in seconds (default: 1800 = 30 min)

  - name: "dns-forward"
    protocol: "udp"      # Only forward UDP traffic
    listen:
      address: "0.0.0.0"  # IPv4 wildcard
      port: 53
    backends:
      - "dns.google:53"
      - "8.8.8.8:53"
    backend_cooldown: 300  # Shorter cooldown for DNS (5 minutes)
```

### Configuration Options

#### Web UI (Optional)

- `web_ui`: Web management interface configuration
  - `enabled`: Enable/disable Web UI (default: `false`)
  - `listen_address`: Address to bind to (default: `127.0.0.1`)
  - `port`: Port to listen on (default: `8088`)
  - `auth`: HTTP Basic Authentication (optional)
    - `enabled`: Enable authentication (default: `false`)
    - `username`: Basic auth username
    - `password`: Basic auth password

**How Web UI works:**
- UI modifications are saved to `config.runtime.yaml`
- If `config.yaml` is manually edited, it automatically overrides `config.runtime.yaml`
- Hash comparison ensures config file is always the authoritative source
- Provides REST API for programmatic configuration management

#### Services

- `services`: List of relay services to run
  - `name`: Service identifier (used in logs)
  - `protocol`: Protocol to relay - `tcp`, `udp`, or `both` (default: `both`)
  - `listen`: Listening configuration
    - `address`: IP address to bind to (`0.0.0.0` for IPv4, `::` for IPv6)
    - `port`: Port number to listen on
  - `backends`: List of backend servers in priority order
    - Format: `host:port` or `[ipv6]:port`
    - Supports domain names and IP addresses
  - `backend_cooldown`: (Optional) Cooldown period in seconds after 2nd consecutive failure (default: 1800)
    - Backends that fail twice are marked unavailable and skipped for this duration
    - Set to 0 to disable cooldown (not recommended)
    - Recommended values: 300-600 for DNS/critical services, 1800-3600 for web services

## Usage

### Using uv (Recommended)

Start the service:
```bash
uv run async-relay -c config/config.yaml
```

Or use the module directly:
```bash
uv run python -m src -c config/config.yaml
```

### Using Python directly

```bash
python -m src -c config/config.yaml
```

### Command-line options

```bash
uv run async-relay --help

options:
  -h, --help            show this help message and exit
  -c CONFIG, --config CONFIG
                        Path to configuration file (default: config/config.yaml)
  --log-level {DEBUG,INFO,WARNING,ERROR}
                        Logging level (default: INFO)
  --no-reload           Disable configuration file hot reload
  --reload-delay SECONDS
                        Debounce delay in seconds for config reload (default: 10.0)
  --version             show program's version number and exit
```

### Examples

Start with custom config:
```bash
uv run async-relay -c /path/to/config.yaml
```

Enable debug logging:
```bash
uv run async-relay --log-level DEBUG
```

Run in development mode:
```bash
uv run --dev async-relay -c config/config.yaml
```

## Architecture

```
┌─────────────────────┐
│   Service Manager   │
│  (Coordinates all)  │
└──────────┬──────────┘
           │
           ├──────────────┬──────────────┐
           ▼              ▼              ▼
    ┌──────────┐   ┌──────────┐   ┌──────────┐
    │ Service1 │   │ Service2 │   │ Service3 │
    │ TCP+UDP  │   │ TCP+UDP  │   │ TCP+UDP  │
    └─────┬────┘   └─────┬────┘   └─────┬────┘
          │              │              │
          └──────────────┴──────────────┘
                         │
                  ┌──────▼──────┐
                  │ Backend Pool│
                  │  (Failover) │
                  └──────┬──────┘
                         │
                  ┌──────▼──────┐
                  │DNS Resolver │
                  │  (1hr cache)│
                  └─────────────┘
```

### Key Components

1. **Service Manager**: Coordinates all services and handles shutdown
2. **Relay Service**: Handles TCP and UDP connections for a single port
3. **Backend Pool**: Manages backend servers with failover logic
4. **DNS Resolver**: Resolves and caches domain names

### Failover Strategy

The service uses a sequential failover strategy with intelligent cooldown:

1. Attempts connection to backends in configured order
2. First successful connection is used
3. On failure:
   - **First failure**: Clears DNS cache and retries immediately
   - **Second failure**: Moves backend to end of queue and marks as unavailable for cooldown period
   - **During cooldown**: Backend is skipped in connection attempts (reduces retry overhead)
   - **After cooldown**: Backend is automatically eligible for retry
   - **On success**: Cooldown status is immediately cleared
4. All available backends are tried before giving up
5. **Fallback**: If all backends are in cooldown, they are tried anyway to prevent complete service failure

**Example behavior:**
```
Time 0:00: [A, B, C]
Request 1: A fails (1st) → Retry A → Still fails (2nd) → A enters cooldown (30min)
Time 0:01: [B, C, A🔥] (A skipped for 30min)
Request 2: Only tries B and C (saves 5 seconds timeout per request)
Time 0:31: [B, C, A✓] (A cooldown expired, automatically retried)
```

### DNS Caching

- Domain names are resolved to IP addresses automatically
- Cache TTL: 1 hour (3600 seconds)
- Cache is cleared on first connection failure
- Background task refreshes cache every hour

## Resource Management

The service includes comprehensive resource management:

- **Connection Timeouts**: 5 seconds for backend connections
- **Idle Timeouts**: 60 seconds for inactive connections
- **Backend Cooldown**: 30 minutes default (configurable per service)
- **Proper Cleanup**: All sockets are properly closed on errors
- **Exception Handling**: Prevents single connection failures from affecting service
- **Graceful Shutdown**: SIGTERM/SIGINT trigger clean shutdown
- **Configuration Hot Reload**: 10 seconds debounce delay (configurable)

## Logging

The service logs all key events:

- Service startup/shutdown and configuration reload
- Backend connection attempts and results
- Backend cooldown enter/exit events
- DNS resolution and cache operations
- Connection errors and timeouts
- Failover actions and backend rotation

Log levels:
- `DEBUG`: Detailed connection and data transfer info
- `INFO`: Service lifecycle and important events (default)
- `WARNING`: Connection failures and issues
- `ERROR`: Critical errors

## Performance Considerations

- Uses `uvloop` for 2-4x better performance than standard asyncio
- Efficient buffer sizes (64KB for TCP, standard for UDP)
- Concurrent connection handling
- Minimal overhead per connection
- **Backend cooldown reduces timeout overhead**: Failed backends are skipped during cooldown, saving up to 5 seconds per request
- **Smart DNS caching**: 1-hour TTL with automatic refresh, cleared on first failure

## Limitations

- UDP session tracking uses simple timeout-based cleanup
- No load balancing (sequential failover only)
- No authentication or encryption (design as a local relay)
- Maximum one backend connection per client at a time

## License

MIT License - See LICENSE file for details

## Development

### Setup development environment

```bash
# Install with dev dependencies
uv sync --all-extras
```

### Code quality

```bash
# Format code
uv run ruff format src/

# Lint code
uv run ruff check src/

# Type checking
uv run mypy src/

# Run tests
uv run pytest
```

### Project structure

```
async-relay/
├── src/
│   ├── __init__.py
│   ├── __main__.py          # Entry point for python -m src
│   ├── cli.py               # CLI entry point
│   ├── app/
│   │   └── service_manager.py   # Service coordinator
│   ├── core/
│   │   ├── backend_pool.py      # Backend pool management
│   │   ├── dns_resolver.py      # DNS resolver with caching
│   │   ├── event_hook.py        # Event hook runner
│   │   └── relay_service.py     # TCP/UDP relay service
│   ├── config/
│   │   ├── loader.py            # Configuration loader
│   │   ├── models.py            # Configuration models
│   │   ├── runtime.py           # Runtime config manager
│   │   └── watcher.py           # Config watcher
│   └── web/
│       └── web_ui.py            # Web UI server
├── config/
│   └── config.yaml          # Configuration file
├── pyproject.toml           # Project metadata and dependencies
├── requirements.txt         # Pip fallback
├── LICENSE
└── README.md
```

## Contributing

Contributions are welcome! Please ensure:
- Code follows Python 3.11+ type hints (use `mypy` for checking)
- Code is formatted with `ruff format`
- All changes are logged appropriately
- Resource cleanup is properly handled
- Tests pass (if implemented)

## Troubleshooting

### Service won't start
- Check if ports are already in use: `netstat -tuln | grep <port>`
- Verify configuration file syntax
- Check file permissions

### Backends not connecting
- Verify backend addresses are reachable
- Check DNS resolution: `nslookup <domain>`
- Review logs with `--log-level DEBUG`
- Check if backends are in cooldown: Look for "marked unavailable" or "cooldown" in logs
- To force retry of cooled-down backend: Restart the service or wait for cooldown to expire

### High memory usage
- Check for stale UDP sessions (automatically cleaned after 5 minutes)
- Reduce number of concurrent connections
- Monitor with `htop` or similar tools

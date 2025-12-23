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
  - Second failure: Move backend to end of queue
- 🛡️ **Resource Management**: Proper connection cleanup and timeout handling
- 📝 **Comprehensive Logging**: Detailed logging of all key events
- ⚙️ **Flexible Configuration**: YAML-based configuration with protocol selection (tcp/udp/both)

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
services:
  - name: "web-proxy"
    listen:
      address: "::"      # IPv6 (also accepts IPv4)
      port: 8080
    backends:
      - "example.com:80"           # Domain name
      - "192.168.1.10:80"          # IPv4 address
      - "[2001:db8::1]:80"         # IPv6 address
      - "backup.example.com:80"    # Backup backend

  - name: "dns-forward"
    listen:
      address: "0.0.0.0"  # IPv4 wildcard
      port: 53
    backends:
      - "dns.google:53"
      - "8.8.8.8:53"
```

### Configuration Options

- `services`: List of relay services to run
  - `name`: Service identifier (used in logs)
  - `protocol`: Protocol to relay - `tcp`, `udp`, or `both` (default: `both`)
  - `listen`: Listening configuration
    - `address`: IP address to bind to (`0.0.0.0` for IPv4, `::` for IPv6)
    - `port`: Port number to listen on
  - `backends`: List of backend servers in priority order
    - Format: `host:port` or `[ipv6]:port`
    - Supports domain names and IP addresses

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

The service uses a sequential failover strategy:

1. Attempts connection to backends in configured order
2. First successful connection is used
3. On failure:
   - **First failure**: Clears DNS cache and retries
   - **Second failure**: Moves backend to end of queue
4. All backends are tried before giving up

### DNS Caching

- Domain names are resolved to IP addresses automatically
- Cache TTL: 1 hour (3600 seconds)
- Cache is cleared on first connection failure
- Background task refreshes cache every hour

## Resource Management

The service includes comprehensive resource management:

- **Connection Timeouts**: 5 seconds for backend connections
- **Idle Timeouts**: 5 minutes for inactive connections
- **Proper Cleanup**: All sockets are properly closed on errors
- **Exception Handling**: Prevents single connection failures from affecting service
- **Graceful Shutdown**: SIGTERM/SIGINT trigger clean shutdown

## Logging

The service logs all key events:

- Service startup/shutdown
- Backend connection attempts and results
- DNS resolution and cache operations
- Connection errors and timeouts
- Failover actions

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
│   ├── main.py              # Main application
│   ├── config.py            # Configuration loader
│   ├── dns_resolver.py      # DNS resolver with caching
│   ├── backend_pool.py      # Backend pool management
│   ├── relay_service.py     # TCP/UDP relay service
│   └── service_manager.py   # Service coordinator
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

### High memory usage
- Check for stale UDP sessions (automatically cleaned after 5 minutes)
- Reduce number of concurrent connections
- Monitor with `htop` or similar tools

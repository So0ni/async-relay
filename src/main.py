"""Main entry point for TCP/UDP relay service."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False

from src.service_manager import ServiceManager


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure root logger
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Set asyncio log level to WARNING to reduce noise
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    if UVLOOP_AVAILABLE:
        logger.info("uvloop enabled for improved performance")
    else:
        logger.warning("uvloop not available, using default event loop")


def parse_arguments() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns:
        Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="TCP/UDP Relay Service with automatic failover",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -c config/config.yaml
  %(prog)s -c config/config.yaml --log-level DEBUG
  %(prog)s -c config/config.yaml --no-reload
  %(prog)s -c config/config.yaml --reload-delay 5
        """,
    )

    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to configuration file (default: config/config.yaml)",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "--no-reload", action="store_true", help="Disable configuration file hot reload"
    )

    parser.add_argument(
        "--reload-delay",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Debounce delay in seconds for config reload (default: 10.0)",
    )

    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")

    return parser.parse_args()


async def main() -> int:
    """
    Main application entry point.

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    args = parse_arguments()
    setup_logging(args.log_level)

    logger = logging.getLogger(__name__)

    try:
        # Load configuration
        config_path = Path(args.config)
        logger.info(f"Loading configuration from {config_path.absolute()}")

        # Create runtime config manager
        from src.runtime_config import RuntimeConfigManager

        runtime_config_manager = RuntimeConfigManager(str(config_path.absolute()))

        # Load active configuration (checks hash, uses runtime.yaml if valid)
        config = runtime_config_manager.load_active_config()

        # Create and start service manager
        manager = ServiceManager(
            config=config,
            config_path=str(config_path.absolute()),
            runtime_config_manager=runtime_config_manager,
            enable_reload=not args.no_reload,
            reload_delay=args.reload_delay,
        )
        await manager.start()

        logger.info("Service shutdown complete")
        return 0

    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        return 1

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 0

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return 1


def run() -> None:
    """
    Entry point wrapper for running the application.

    This function is used as the console script entry point.
    """
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    run()

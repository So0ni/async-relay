"""Configuration file watcher with debounce mechanism."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from watchdog.events import DirModifiedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class ConfigWatcher:
    """
    Watches configuration file for changes with debounce.

    When the config file is modified, waits for a quiet period (debounce)
    before triggering the reload callback. Multiple modifications within
    the debounce window are collapsed into a single reload.
    """

    def __init__(
        self,
        config_path: str,
        on_change_callback: Callable[[], Awaitable[None]],
        debounce_seconds: float = 10.0,
    ):
        """
        Initialize config file watcher.

        Args:
            config_path: Path to configuration file to watch
            on_change_callback: Async callback to invoke on config change
            debounce_seconds: Seconds to wait after last change before reloading
        """
        self.config_path = Path(config_path).resolve()
        self.on_change_callback = on_change_callback
        self.debounce_seconds = debounce_seconds

        # Watchdog components
        self._observer: Any = None  # watchdog.observers.Observer
        self._event_handler: _ConfigFileEventHandler | None = None

        # Event loop reference (set when start() is called)
        self._loop: asyncio.AbstractEventLoop | None = None

        # Debounce state
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_reload = False

        logger.info(
            f"Config watcher initialized: {self.config_path} (debounce: {debounce_seconds}s)"
        )

    def start(self) -> None:
        """Start watching the configuration file."""
        if self._observer is not None:
            logger.warning("Config watcher already started")
            return

        # Get event loop reference
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("ConfigWatcher.start() must be called from within an async context")

        # Create event handler
        self._event_handler = _ConfigFileEventHandler(
            config_path=self.config_path,
            on_modified=self._on_file_modified,
        )

        # Create and start observer
        self._observer = Observer()
        watch_dir = self.config_path.parent
        self._observer.schedule(
            self._event_handler,
            str(watch_dir),
            recursive=False,
        )
        self._observer.start()

        logger.info(f"Config watcher started: {self.config_path}")

    def stop(self) -> None:
        """Stop watching the configuration file."""
        # Cancel pending debounce task
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            logger.debug("Cancelled pending debounce task")

        # Stop observer
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
            logger.info("Config watcher stopped")

    def _on_file_modified(self) -> None:
        """
        Called by watchdog when config file is modified.

        Starts/resets the debounce timer.

        Note: This runs in watchdog's thread, not the asyncio thread.
        We use call_soon_threadsafe to schedule the task creation.
        """
        if not self._loop:
            logger.error("Event loop not initialized, cannot schedule reload")
            return

        # Schedule task creation in the event loop thread
        self._loop.call_soon_threadsafe(self._schedule_debounce)

    def _schedule_debounce(self) -> None:
        """
        Schedule or reschedule the debounce task.

        This runs in the event loop thread (called via call_soon_threadsafe).
        """
        # Cancel existing debounce task if running
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            logger.debug(
                f"Config file changed again, resetting debounce timer ({self.debounce_seconds}s)"
            )
        else:
            logger.debug(
                f"Config file change detected: {self.config_path.name} "
                f"(debounce: {self.debounce_seconds}s)"
            )

        # Create new debounce task
        self._debounce_task = asyncio.create_task(self._debounced_reload())

    async def _debounced_reload(self) -> None:
        """
        Wait for debounce period, then trigger reload callback.

        This task gets cancelled and restarted if file is modified again
        during the debounce period.
        """
        try:
            # Wait for quiet period
            await asyncio.sleep(self.debounce_seconds)

            # Debounce period complete, trigger reload
            logger.info("Debounce period complete, triggering config reload")
            await self.on_change_callback()

        except asyncio.CancelledError:
            # Task was cancelled due to new file modification
            logger.debug("Debounce task cancelled (new modification detected)")
            raise


class _ConfigFileEventHandler(FileSystemEventHandler):
    """Internal event handler for watchdog."""

    def __init__(self, config_path: Path, on_modified: Callable[[], None]):
        """
        Initialize event handler.

        Args:
            config_path: Resolved path to config file
            on_modified: Callback to invoke on modification
        """
        super().__init__()
        self.config_path = config_path
        self.on_modified_callback = on_modified

    def on_modified(self, event: DirModifiedEvent | FileModifiedEvent) -> None:
        """
        Handle file modification events.

        Args:
            event: File system event from watchdog
        """
        # Ignore directory modifications
        if event.is_directory:
            return

        # Check if the modified file is our config file
        event_path = Path(str(event.src_path)).resolve()
        if event_path == self.config_path:
            # Invoke callback (will run in watchdog's thread)
            self.on_modified_callback()

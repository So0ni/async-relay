"""Event hook executor for backend state changes."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Event type definition
EventType = Literal["backend_failed", "all_backends_unavailable", "backend_recovered"]


@dataclass
class EventContext:
    """Context information for an event."""

    event_type: EventType  # Event type: backend_failed, all_backends_unavailable, backend_recovered
    service_name: str  # Service name
    backend_host: str | None = None  # Backend hostname (None for all_backends_unavailable)
    backend_port: int | None = None  # Backend port (None for all_backends_unavailable)
    backend_ip: str | None = None  # Resolved backend IP (None for all_backends_unavailable)
    failure_count: int = 0  # Consecutive failure count
    available_count: int = 0  # Number of currently available backends
    total_count: int = 0  # Total number of backends
    timestamp: str | None = None  # ISO format timestamp

    def __post_init__(self) -> None:
        """Set timestamp if not provided."""
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC).isoformat()


class EventHook:
    """
    Manages event hook execution for backend state changes.

    Executes configured commands asynchronously when subscribed events occur,
    passing event context via environment variables and JSON.
    """

    def __init__(
        self,
        service_name: str,
        command: str,
        args: list[str] | None = None,
        events: list[str] | None = None,
        timeout: float = 30.0,
    ):
        """
        Initialize event hook.

        Args:
            service_name: Service name (for logging)
            command: Command to execute (binary or script path)
            args: Command arguments
            events: List of event types to subscribe to
            timeout: Command execution timeout in seconds
        """
        self.service_name = service_name
        self.command = command
        self.args = args or []
        self.events = set(events or [])
        self.timeout = timeout
        self._executing_tasks: set[asyncio.Task[None]] = set()

        logger.info(
            f"[{service_name}] Event hook initialized: command={command}, "
            f"events={sorted(self.events)}, timeout={timeout}s"
        )

    def is_subscribed(self, event_type: str) -> bool:
        """
        Check if hook is subscribed to an event type.

        Args:
            event_type: Event type to check

        Returns:
            True if subscribed, False otherwise
        """
        return event_type in self.events

    async def trigger(self, context: EventContext) -> None:
        """
        Trigger event hook execution.

        Args:
            context: Event context information
        """
        if not self.is_subscribed(context.event_type):
            logger.debug(
                f"[{self.service_name}] Event hook not subscribed to '{context.event_type}', skipping"
            )
            return

        logger.info(f"[{self.service_name}] Triggering event hook for '{context.event_type}'")

        # Execute in background task
        task = asyncio.create_task(self._execute(context))
        self._executing_tasks.add(task)
        task.add_done_callback(self._executing_tasks.discard)

    async def _execute(self, context: EventContext) -> None:
        """
        Execute hook command asynchronously.

        Args:
            context: Event context information
        """
        try:
            # Build environment variables
            env = os.environ.copy()
            env.update(self._build_env_vars(context))

            # Build command arguments
            cmd_args = [self.command] + self.args

            logger.debug(
                f"[{self.service_name}] Executing hook: {' '.join(cmd_args)} "
                f"(timeout: {self.timeout}s)"
            )

            # Execute command with timeout
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )

                if process.returncode == 0:
                    logger.info(
                        f"[{self.service_name}] Event hook completed successfully "
                        f"for '{context.event_type}'"
                    )
                    if stdout:
                        logger.debug(
                            f"[{self.service_name}] Hook stdout: {stdout.decode('utf-8', errors='replace').strip()}"
                        )
                else:
                    logger.warning(
                        f"[{self.service_name}] Event hook exited with code {process.returncode} "
                        f"for '{context.event_type}'"
                    )
                    if stderr:
                        logger.warning(
                            f"[{self.service_name}] Hook stderr: {stderr.decode('utf-8', errors='replace').strip()}"
                        )

            except TimeoutError:
                logger.error(
                    f"[{self.service_name}] Event hook timeout ({self.timeout}s) "
                    f"for '{context.event_type}'"
                )
                # Kill the process
                try:
                    process.kill()
                    await process.wait()
                except Exception as e:
                    logger.debug(f"[{self.service_name}] Error killing hook process: {e}")

        except FileNotFoundError:
            logger.error(f"[{self.service_name}] Event hook command not found: {self.command}")
        except PermissionError:
            logger.error(f"[{self.service_name}] Event hook command not executable: {self.command}")
        except Exception as e:
            logger.error(
                f"[{self.service_name}] Event hook execution error for '{context.event_type}': {e}",
                exc_info=True,
            )

    def _build_env_vars(self, context: EventContext) -> dict[str, str]:
        """
        Build environment variables for hook command.

        Args:
            context: Event context information

        Returns:
            Dictionary of environment variables
        """
        env_vars = {
            "RELAY_EVENT_TYPE": context.event_type,
            "RELAY_SERVICE_NAME": context.service_name,
            "RELAY_FAILURE_COUNT": str(context.failure_count),
            "RELAY_AVAILABLE_COUNT": str(context.available_count),
            "RELAY_TOTAL_COUNT": str(context.total_count),
            "RELAY_TIMESTAMP": context.timestamp or "",
        }

        # Add backend-specific variables (if available)
        if context.backend_host is not None:
            env_vars["RELAY_BACKEND_HOST"] = context.backend_host
        if context.backend_port is not None:
            env_vars["RELAY_BACKEND_PORT"] = str(context.backend_port)
        if context.backend_ip is not None:
            env_vars["RELAY_BACKEND_IP"] = context.backend_ip

        # Add complete JSON representation
        event_data: dict[str, Any] = {
            "event": context.event_type,
            "service": context.service_name,
            "failure_count": context.failure_count,
            "available_count": context.available_count,
            "total_count": context.total_count,
            "timestamp": context.timestamp,
        }

        if context.backend_host is not None:
            event_data["backend"] = {
                "host": context.backend_host,
                "port": context.backend_port,
                "ip": context.backend_ip,
            }

        env_vars["RELAY_EVENT_JSON"] = json.dumps(event_data, ensure_ascii=False)

        return env_vars

    async def shutdown(self) -> None:
        """Wait for all executing tasks to complete."""
        if self._executing_tasks:
            logger.debug(
                f"[{self.service_name}] Waiting for {len(self._executing_tasks)} hook task(s) to complete"
            )
            await asyncio.gather(*self._executing_tasks, return_exceptions=True)
            logger.debug(f"[{self.service_name}] All hook tasks completed")

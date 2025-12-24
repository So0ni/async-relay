"""TCP and UDP relay service with failover."""

import asyncio
import logging
import time
from typing import Any, Literal

from src.backend_pool import BackendPool

logger = logging.getLogger(__name__)

# Connection and data transfer timeouts
CONNECT_TIMEOUT = 5.0  # seconds
IDLE_TIMEOUT = 300.0  # 5 minutes for idle connections
BUFFER_SIZE = 65536  # 64KB buffer for data transfer


class RelayService:
    """
    Dual-protocol relay service.

    Listens on a single port for both TCP and UDP traffic and forwards
    to backend servers with automatic failover.
    """

    def __init__(
        self,
        name: str,
        listen_addr: str,
        listen_port: int,
        backend_pool: BackendPool,
        protocol: Literal["tcp", "udp", "both"] = "both",
    ):
        """
        Initialize relay service.

        Args:
            name: Service name (for logging)
            listen_addr: Address to listen on
            listen_port: Port to listen on
            backend_pool: Backend pool for failover
            protocol: Protocol to relay ('tcp', 'udp', or 'both')
        """
        self.name = name
        self.listen_addr = listen_addr
        self.listen_port = listen_port
        self.pool = backend_pool
        self.protocol = protocol

        self._tcp_server: asyncio.Server | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._running = False

        # Statistics
        self.stats = {
            'tcp_connections': 0,
            'tcp_active': 0,
            'tcp_bytes_sent': 0,
            'tcp_bytes_received': 0,
            'udp_packets': 0,
            'udp_bytes_sent': 0,
            'udp_bytes_received': 0,
        }

        logger.info(
            f"[{self.name}] Relay service initialized: "
            f"{listen_addr}:{listen_port} (protocol: {protocol})"
        )

    async def start(self) -> None:
        """Start TCP and/or UDP listeners based on protocol configuration."""
        if self._running:
            logger.warning(f"[{self.name}] Service already running")
            return

        self._running = True

        # Start servers based on protocol configuration
        tasks: list[asyncio.Task[None]] = []

        if self.protocol in ('tcp', 'both'):
            tcp_task = asyncio.create_task(self._start_tcp())
            tasks.append(tcp_task)

        if self.protocol in ('udp', 'both'):
            udp_task = asyncio.create_task(self._start_udp())
            tasks.append(udp_task)

        if not tasks:
            raise ValueError(f"Invalid protocol: {self.protocol}")

        protocol_str = self.protocol.upper()
        logger.info(
            f"[{self.name}] Starting {protocol_str} listener(s) on "
            f"{self.listen_addr}:{self.listen_port}"
        )

        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"[{self.name}] Service error: {e}", exc_info=True)
            self._running = False
            raise

    async def stop(self) -> None:
        """Stop the service and clean up resources."""
        logger.info(f"[{self.name}] Stopping service")
        self._running = False

        # Stop TCP server
        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            logger.info(f"[{self.name}] TCP server stopped")

        # Stop UDP transport
        if self._udp_transport:
            self._udp_transport.close()
            logger.info(f"[{self.name}] UDP transport stopped")

    async def _start_tcp(self) -> None:
        """Start TCP server."""
        try:
            self._tcp_server = await asyncio.start_server(
                self._handle_tcp_client,
                self.listen_addr,
                self.listen_port,
                reuse_port=True,  # Allow UDP to bind same port
            )

            addrs = ', '.join(str(sock.getsockname()) for sock in self._tcp_server.sockets)
            logger.info(f"[{self.name}] TCP server listening on {addrs}")

            async with self._tcp_server:
                await self._tcp_server.serve_forever()

        except asyncio.CancelledError:
            logger.debug(f"[{self.name}] TCP server cancelled")
        except Exception as e:
            logger.error(
                f"[{self.name}] TCP server error: {e}",
                exc_info=True
            )
            raise

    async def _start_udp(self) -> None:
        """Start UDP server."""
        try:
            loop = asyncio.get_running_loop()

            # Create UDP endpoint
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: UDPRelayProtocol(self.name, self.pool, self.stats),
                local_addr=(self.listen_addr, self.listen_port),
                reuse_port=True,  # Allow TCP to bind same port
            )

            self._udp_transport = transport

            logger.info(
                f"[{self.name}] UDP server listening on "
                f"{self.listen_addr}:{self.listen_port}"
            )

            # Keep UDP server running
            while self._running:
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.debug(f"[{self.name}] UDP server cancelled")
        except Exception as e:
            logger.error(
                f"[{self.name}] UDP server error: {e}",
                exc_info=True
            )
            raise

    async def _handle_tcp_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """
        Handle incoming TCP connection with failover.

        Tries backends in order until one succeeds. Implements proper
        resource cleanup and timeout handling.

        Args:
            client_reader: Client stream reader
            client_writer: Client stream writer
        """
        client_addr = client_writer.get_extra_info('peername')
        connection_id = f"{client_addr}->{self.name}"

        self.stats['tcp_connections'] += 1
        self.stats['tcp_active'] += 1

        logger.info(f"[{connection_id}] New TCP connection")

        remote_reader: asyncio.StreamReader | None = None
        remote_writer: asyncio.StreamWriter | None = None

        try:
            # Try backends in order
            backends = await self.pool.get_backends_in_order()

            if not backends:
                logger.error(
                    f"[{connection_id}] No backends available"
                )
                return

            for backend_ip, backend_port, backend in backends:
                try:
                    logger.debug(
                        f"[{connection_id}] Trying backend "
                        f"{backend.host}:{backend.port} ({backend_ip})"
                    )

                    # Attempt connection with timeout
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(backend_ip, backend_port),
                        timeout=CONNECT_TIMEOUT,
                    )

                    # Success!
                    await self.pool.on_connect_success(backend)

                    logger.info(
                        f"[{connection_id}] Connected to backend "
                        f"{backend.host}:{backend.port} ({backend_ip})"
                    )
                    break

                except TimeoutError:
                    logger.warning(
                        f"[{connection_id}] Backend {backend.host}:{backend.port} "
                        f"({backend_ip}) connection timeout"
                    )
                    await self.pool.on_connect_failure(backend)

                except (ConnectionRefusedError, OSError) as e:
                    logger.warning(
                        f"[{connection_id}] Backend {backend.host}:{backend.port} "
                        f"({backend_ip}) connection failed: {e}"
                    )
                    await self.pool.on_connect_failure(backend)

            # Check if any backend succeeded
            if not remote_writer or not remote_reader:
                logger.error(
                    f"[{connection_id}] All backends failed, closing connection"
                )
                return

            # Perform bidirectional relay
            await self._relay_tcp_data(
                connection_id,
                client_reader,
                client_writer,
                remote_reader,
                remote_writer,
            )

        except asyncio.CancelledError:
            logger.debug(f"[{connection_id}] Connection cancelled")
            raise

        except Exception as e:
            logger.error(
                f"[{connection_id}] Unexpected error: {e}",
                exc_info=True
            )

        finally:
            # Cleanup: ensure both sides are closed
            self.stats['tcp_active'] -= 1

            if client_writer:
                try:
                    if not client_writer.is_closing():
                        client_writer.close()
                        await client_writer.wait_closed()
                except Exception as e:
                    logger.debug(f"[{connection_id}] Error closing client: {e}")

            if remote_writer:
                try:
                    if not remote_writer.is_closing():
                        remote_writer.close()
                        await remote_writer.wait_closed()
                except Exception as e:
                    logger.debug(f"[{connection_id}] Error closing remote: {e}")

            logger.info(f"[{connection_id}] Connection closed")

    async def _relay_tcp_data(
        self,
        connection_id: str,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        remote_reader: asyncio.StreamReader,
        remote_writer: asyncio.StreamWriter,
    ) -> None:
        """
        Relay data bidirectionally between client and remote.

        Handles EOF, timeouts, and errors gracefully. Ensures both
        directions are properly closed when one side terminates.

        Args:
            connection_id: Connection identifier for logging
            client_reader: Client stream reader
            client_writer: Client stream writer
            remote_reader: Remote stream reader
            remote_writer: Remote stream writer
        """

        async def forward(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
            direction: str,
        ) -> None:
            """Forward data in one direction."""
            try:
                while True:
                    # Read with timeout to detect stale connections
                    try:
                        data = await asyncio.wait_for(
                            reader.read(BUFFER_SIZE),
                            timeout=IDLE_TIMEOUT,
                        )
                    except TimeoutError:
                        logger.debug(
                            f"[{connection_id}] {direction} idle timeout"
                        )
                        break

                    if not data:
                        # EOF received
                        logger.debug(
                            f"[{connection_id}] {direction} EOF received"
                        )
                        break

                    # Write data
                    writer.write(data)
                    await writer.drain()

                    # Update stats
                    if direction == 'client->remote':
                        self.stats['tcp_bytes_sent'] += len(data)
                    else:
                        self.stats['tcp_bytes_received'] += len(data)

            except (ConnectionResetError, BrokenPipeError) as e:
                logger.debug(
                    f"[{connection_id}] {direction} connection error: {e}"
                )

            except Exception as e:
                logger.warning(
                    f"[{connection_id}] {direction} error: {e}",
                    exc_info=True
                )

            finally:
                # Signal EOF to other side
                try:
                    if not writer.is_closing():
                        writer.write_eof()
                except Exception:
                    pass

        # Run both directions concurrently
        await asyncio.gather(
            forward(client_reader, remote_writer, 'client->remote'),
            forward(remote_reader, client_writer, 'remote->client'),
            return_exceptions=True,
        )


class UDPRelayProtocol(asyncio.DatagramProtocol):
    """
    UDP relay protocol handler.

    Maintains client session mappings and forwards packets to backends
    with failover support.
    """

    def __init__(self, service_name: str, pool: BackendPool, stats: dict[str, Any]):
        """
        Initialize UDP relay protocol.

        Args:
            service_name: Service name for logging
            pool: Backend pool
            stats: Statistics dictionary
        """
        self.service_name = service_name
        self.pool = pool
        self.stats = stats
        self.transport: asyncio.DatagramTransport | None = None

        # Client session tracking: client_addr -> (backend_transport, last_activity)
        self.sessions: dict[tuple[str, int], tuple[asyncio.DatagramTransport, float]] = {}

        # Task management: limit concurrent datagram processing
        self._max_concurrent_tasks = 1000  # Prevent task explosion under high load
        self._task_semaphore = asyncio.Semaphore(self._max_concurrent_tasks)
        self._pending_tasks: set[asyncio.Task[None]] = set()

        # Start cleanup task
        self._cleanup_task: asyncio.Task[None] | None = None

        logger.debug(f"[{service_name}] UDP protocol initialized")

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        """Called when UDP socket is ready."""
        self.transport = transport
        logger.debug(f"[{self.service_name}] UDP connection made")

        # Start session cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_sessions())

    def connection_lost(self, exc: Exception | None) -> None:
        """Called when UDP socket is closed."""
        logger.debug(f"[{self.service_name}] UDP connection lost: {exc}")

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()

        # Cancel all pending datagram processing tasks
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()
        self._pending_tasks.clear()

        # Close all session transports
        for session_transport, _ in self.sessions.values():
            session_transport.close()
        self.sessions.clear()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """
        Handle incoming UDP datagram.

        Args:
            data: Received data
            addr: Client address tuple (ip, port)
        """
        self.stats['udp_packets'] += 1
        self.stats['udp_bytes_received'] += len(data)

        # Handle in async context with task tracking
        task = asyncio.create_task(self._handle_datagram_wrapper(data, addr))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _handle_datagram_wrapper(self, data: bytes, client_addr: tuple[str, int]) -> None:
        """
        Wrapper for datagram handling with concurrency control and error handling.

        Args:
            data: Datagram data
            client_addr: Client address
        """
        try:
            # Acquire semaphore to limit concurrent tasks
            async with self._task_semaphore:
                await self._handle_datagram(data, client_addr)
        except asyncio.CancelledError:
            # Task was cancelled during shutdown, this is expected
            logger.debug(f"[{self.service_name}] UDP: Datagram task cancelled for {client_addr}")
            raise
        except Exception as e:
            # Log unexpected errors to prevent silent failures
            logger.error(
                f"[{self.service_name}] UDP: Unhandled error processing datagram from {client_addr}: {e}",
                exc_info=True
            )

    async def _handle_datagram(self, data: bytes, client_addr: tuple[str, int]) -> None:
        """
        Process UDP datagram with backend failover.

        Args:
            data: Datagram data
            client_addr: Client address
        """
        if self.transport is None:
            logger.error(f"[{self.service_name}] UDP: Transport not initialized")
            return

        backend_transport: asyncio.DatagramTransport | None = None
        transport_created = False

        try:
            # Try to get or create backend connection
            backends = await self.pool.get_backends_in_order()

            if not backends:
                logger.warning(
                    f"[{self.service_name}] UDP: No backends available for {client_addr}"
                )
                return

            # Use first backend (simplified for UDP)
            backend_ip, backend_port, backend = backends[0]

            logger.debug(
                f"[{self.service_name}] UDP: Forwarding from {client_addr} to "
                f"{backend.host}:{backend.port} ({backend_ip})"
            )

            # Get or create backend transport for this client
            loop = asyncio.get_running_loop()

            if client_addr not in self.sessions:
                # Create new backend connection with explicit error handling
                try:
                    backend_transport, _ = await loop.create_datagram_endpoint(
                        lambda: UDPBackendProtocol(
                            self.service_name,
                            client_addr,
                            self.transport,  # type: ignore
                            self.stats,
                        ),
                        remote_addr=(backend_ip, backend_port),
                    )
                    transport_created = True

                    # Only add to sessions if we successfully created the transport
                    self.sessions[client_addr] = (backend_transport, time.time())
                    logger.debug(
                        f"[{self.service_name}] UDP: Created session for {client_addr}"
                    )
                except Exception as e:
                    logger.error(
                        f"[{self.service_name}] UDP: Failed to create backend transport for {client_addr}: {e}",
                        exc_info=True
                    )
                    # Clean up the transport if it was created
                    if backend_transport is not None:
                        backend_transport.close()
                    return
            else:
                # Update last activity time
                backend_transport, _ = self.sessions[client_addr]
                self.sessions[client_addr] = (backend_transport, time.time())

            # Forward packet to backend
            backend_transport.sendto(data)
            self.stats['udp_bytes_sent'] += len(data)

        except Exception as e:
            logger.error(
                f"[{self.service_name}] UDP datagram handling error: {e}",
                exc_info=True
            )
            # If we created a transport but failed to add it to sessions, clean it up
            if transport_created and backend_transport is not None and client_addr not in self.sessions:
                backend_transport.close()

    async def _cleanup_stale_sessions(self) -> None:
        """Background task to clean up idle UDP sessions."""
        try:
            while True:
                await asyncio.sleep(60)  # Check every minute

                now = time.time()
                stale_sessions = []

                for client_addr, (transport, last_activity) in self.sessions.items():
                    if now - last_activity > IDLE_TIMEOUT:
                        stale_sessions.append(client_addr)

                # Remove stale sessions
                for client_addr in stale_sessions:
                    transport, _ = self.sessions.pop(client_addr)
                    transport.close()
                    logger.debug(
                        f"[{self.service_name}] UDP: Cleaned up stale session for {client_addr}"
                    )

        except asyncio.CancelledError:
            logger.debug(f"[{self.service_name}] UDP cleanup task cancelled")


class UDPBackendProtocol(asyncio.DatagramProtocol):
    """Protocol for receiving responses from UDP backend."""

    def __init__(
        self,
        service_name: str,
        client_addr: tuple[str, int],
        client_transport: asyncio.DatagramTransport,
        stats: dict[str, int],
    ):
        """
        Initialize backend protocol.

        Args:
            service_name: Service name for logging
            client_addr: Original client address
            client_transport: Transport to send responses back to client
            stats: Statistics dictionary
        """
        self.service_name = service_name
        self.client_addr = client_addr
        self.client_transport = client_transport
        self.stats = stats

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """
        Handle response from backend.

        Args:
            data: Response data
            addr: Backend address
        """
        try:
            # Forward response back to client
            self.client_transport.sendto(data, self.client_addr)
            self.stats['udp_bytes_received'] += len(data)

            logger.debug(
                f"[{self.service_name}] UDP: Forwarded {len(data)} bytes "
                f"from backend to {self.client_addr}"
            )

        except Exception as e:
            logger.error(
                f"[{self.service_name}] UDP backend response error: {e}",
                exc_info=True
            )

    def error_received(self, exc: Exception) -> None:
        """Handle protocol errors."""
        logger.warning(
            f"[{self.service_name}] UDP backend protocol error: {exc}"
        )

"""Private, version-scoped HTTP/2 cancellation prototype; never patches installed code.

Only direct asyncio connections are exercised. Proxy production integration is outside
this local proof. A reentrant close queues control frames until the current write
exits, avoiding reacquisition of its own non-reentrant lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import logging
import time
from pathlib import Path
from typing import Any

import h2.errors
import h2.events
import h2.exceptions
import httpcore2
from httpcore2._async.connection import AsyncHTTPConnection
from httpcore2._async.http2 import AsyncHTTP2Connection, HTTPConnectionState
from httpcore2._async.http11 import AsyncHTTP11Connection
from httpcore2._models import Origin, Request, Response
from httpcore2._synchronization import AsyncLock
from httpcore2._trace import Trace

logger = logging.getLogger("private.http2_cancel_fix")


SUPPORTED_SOURCES = {
    "_async/http2.py": "dc4f973ea2cc76bf6ab31229e437dd8cbe0928ce48424c983047d5c1891d110f",
    "_async/connection.py": "9c3271ee7524a7d1d780e143a87514c48fed3f297d06d62e7c542a1bbe3eb980",
    "_synchronization.py": "2588e6dde67d2859bec1c4d9053215f3b29c41232cd4065e4533f66a66f82281",
}


def validate_dependencies() -> None:
    if importlib.metadata.version("httpcore2") != "2.13.1":
        raise RuntimeError("Private cancellation prototype requires reviewed httpcore2 2.13.1")
    if importlib.metadata.version("h2") != "4.4.1":
        raise RuntimeError("Private cancellation prototype requires reviewed h2 4.4.1")
    package = Path(httpcore2.__file__).parent
    for relative, expected in SUPPORTED_SOURCES.items():
        if hashlib.sha256((package / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Installed transport source differs from the reviewed prototype")


class OwnedWriteLock(AsyncLock):
    def __init__(self) -> None:
        super().__init__()
        self.owner: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> OwnedWriteLock:
        await super().__aenter__()
        self.owner = asyncio.current_task()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.owner = None
        await super().__aexit__(*args)


class CancellationSafeHTTP2Connection(AsyncHTTP2Connection):
    CLEANUP_TIMEOUT_S = 2.0
    SOCKET_CLOSE_TIMEOUT_S = 1.0
    CLEANUP_TASK_LIMIT = 100  # Installed local HTTP/2 admitted-stream limit.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._write_lock = OwnedWriteLock()
        self._close_requests: dict[int, Request] = {}
        self._control_flush_pending = False
        self._closing_tasks: dict[int, asyncio.Task[None]] = {}
        self.cleanup_failures: list[str] = []
        self.reclaimed_queued_bytes = 0
        self.reclaimed_orphan_bytes = 0
        self.cancel_resets = 0
        self.deferred_reentrant_flushes = 0

    async def _send_request_headers(self, request: Request, stream_id: int) -> None:
        self._close_requests[stream_id] = request
        await super()._send_request_headers(request, stream_id)

    async def _write_outgoing_data(self, request: Request) -> None:
        # An in-flight write may invoke stream cleanup. That cleanup cannot wait
        # for this lock, but its newly queued controls must leave after this write.
        while True:
            await super()._write_outgoing_data(request)
            if not self._control_flush_pending:
                return
            self._control_flush_pending = False

    async def _response_closed(self, stream_id: int) -> None:
        task = self._closing_tasks.get(stream_id)
        if task is None:
            if stream_id not in self._events and stream_id not in self._close_requests:
                return
            if len(self._closing_tasks) >= self.CLEANUP_TASK_LIMIT:
                self._connection_error = True
                async with asyncio.timeout(self.SOCKET_CLOSE_TIMEOUT_S):
                    await super().aclose()
                raise RuntimeError("HTTP/2 owned cleanup capacity exhausted")
            defer_flush = self._write_lock.owner is asyncio.current_task()
            task = asyncio.create_task(self._bounded_response_close(stream_id, defer_flush))
            self._closing_tasks[stream_id] = task

            def settled(done: asyncio.Task[None]) -> None:
                self._closing_tasks.pop(stream_id, None)
                if done.cancelled():
                    self._connection_error = True
                    self.cleanup_failures.append("CancelledError")
                elif (error := done.exception()) is not None:
                    self._connection_error = True
                    self.cleanup_failures.append(type(error).__name__)

            task.add_done_callback(settled)
        # A caller's second cancellation cannot discard accepted stream cleanup.
        # The connection owns the task until it settles, including client close.
        await asyncio.shield(task)

    async def _bounded_response_close(self, stream_id: int, defer_flush: bool) -> None:
        try:
            async with asyncio.timeout(self.CLEANUP_TIMEOUT_S):
                await self._finish_response_close(stream_id, defer_flush)
        except BaseException:
            self._connection_error = True
            # A failed cleanup cannot leave this connection available to new calls.
            # Connection-wide close on this exceptional path may fail siblings;
            # ordinary cancellation remains stream-local.
            try:
                async with asyncio.timeout(self.SOCKET_CLOSE_TIMEOUT_S):
                    await super().aclose()
            except BaseException:
                pass
            raise

    async def _finish_response_close(self, stream_id: int, defer_flush: bool) -> None:
        request = self._close_requests.pop(stream_id, None)
        events = self._events.get(stream_id)
        controls = False
        if events is not None:
            # Event-loop synchronous: count only still-queued DATA, not bytes a
            # body iterator already acknowledged before yielding to its consumer.
            for event in events:
                if isinstance(event, h2.events.DataReceived):
                    amount = event.flow_controlled_length
                    self._h2_state.acknowledge_received_data(amount, stream_id)
                    self.reclaimed_queued_bytes += amount
                    controls = True
            events.clear()
            stream = self._h2_state.streams.get(stream_id)
            if (
                stream is not None
                and not stream.closed
                and self._connection_terminated is None
                and not self.is_closed()
            ):
                try:
                    self._h2_state.reset_stream(stream_id, h2.errors.ErrorCodes.CANCEL)
                except h2.exceptions.StreamClosedError:
                    pass
                else:
                    self.cancel_resets += 1
                    controls = True
        close_connection = False
        async with self._state_lock:
            if stream_id in self._events:
                await self._max_streams_semaphore.release()
                del self._events[stream_id]
            if self._connection_terminated and not self._events:
                close_connection = True
            elif self._state == HTTPConnectionState.ACTIVE and not self._events:
                self._state = HTTPConnectionState.IDLE
                if self._keepalive_expiry is not None:
                    self._expire_at = time.monotonic() + self._keepalive_expiry
                close_connection = self._used_all_stream_ids
        # Never join another cleanup while holding the state lock it may need.
        if close_connection:
            await super().aclose()
        if controls and request is not None and not self.is_closed():
            self._control_flush_pending = True
            if defer_flush:
                self.deferred_reentrant_flushes += 1
                return
            await self._write_outgoing_data(request)

    async def aclose(self) -> None:
        # Caller must first drain request owners. Otherwise a still-running
        # request may register later cleanup after this finite snapshot.
        error: BaseException | None = None
        try:
            async with asyncio.timeout(self.SOCKET_CLOSE_TIMEOUT_S):
                await super().aclose()
        except BaseException as exc:
            error = exc
        finally:
            current = asyncio.current_task()
            pending = [task for task in self._closing_tasks.values() if task is not current]
            if pending:
                try:
                    async with asyncio.timeout(
                        self.CLEANUP_TIMEOUT_S + self.SOCKET_CLOSE_TIMEOUT_S + 0.1
                    ):
                        await asyncio.gather(*pending, return_exceptions=True)
                except BaseException as exc:
                    if error is None:
                        error = exc
            self._close_requests.clear()
        if error is not None:
            raise error

    async def _receive_events(self, request: Request, stream_id: int | None = None) -> None:
        # Upstream 2.13.1 loop retained, with a single orphan DATA branch. This
        # branch handles a removed stream even if no local reset was possible.
        async with self._read_lock:
            if self._connection_terminated is not None:
                last_stream_id = self._connection_terminated.last_stream_id
                if stream_id and last_stream_id and stream_id > last_stream_id:
                    self._request_count -= 1
                    raise httpcore2.ConnectionNotAvailable()
                raise httpcore2.RemoteProtocolError(self._connection_terminated)
            if stream_id is None or not self._events.get(stream_id):
                events = await self._read_incoming_data(request)
                for event in events:
                    if isinstance(event, h2.events.RemoteSettingsChanged):
                        async with Trace("receive_remote_settings", logger, request) as trace:
                            await self._receive_remote_settings_change(event)
                            trace.return_value = event
                    elif isinstance(
                        event,
                        (
                            h2.events.ResponseReceived,
                            h2.events.DataReceived,
                            h2.events.StreamEnded,
                            h2.events.StreamReset,
                        ),
                    ):
                        if event.stream_id in self._events:
                            self._events[event.stream_id].append(event)
                        elif isinstance(event, h2.events.DataReceived):
                            self._h2_state.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id
                            )
                            self.reclaimed_orphan_bytes += event.flow_controlled_length
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        self._connection_terminated = event
        await self._write_outgoing_data(request)


class CancellationSafeHTTPConnection(AsyncHTTPConnection):
    async def handle_async_request(self, request: Request) -> Response:
        if not self.can_handle_request(request.url.origin):
            raise RuntimeError("Wrong origin for scoped connection")
        try:
            async with self._request_lock:
                if self._connection is None:
                    stream = await self._connect(request)
                    ssl_object = stream.get_extra_info("ssl_object")
                    negotiated = (
                        ssl_object is not None and ssl_object.selected_alpn_protocol() == "h2"
                    )
                    cls = (
                        CancellationSafeHTTP2Connection
                        if negotiated or (self._http2 and not self._http1)
                        else AsyncHTTP11Connection
                    )
                    self._connection = cls(
                        origin=self._origin, stream=stream, keepalive_expiry=self._keepalive_expiry
                    )
        except BaseException:
            self._connect_failed = True
            raise
        return await self._connection.handle_async_request(request)


class CancellationSafePool(httpcore2.AsyncConnectionPool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        validate_dependencies()
        super().__init__(*args, **kwargs)

    def create_connection(self, origin: Origin) -> CancellationSafeHTTPConnection:
        if self._proxy is not None:
            raise ValueError("Proxy transport is outside this private direct-connection proof")
        return CancellationSafeHTTPConnection(
            origin=origin,
            ssl_context=self._ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            retries=self._retries,
            local_address=self._local_address,
            uds=self._uds,
            network_backend=self._network_backend,
            socket_options=self._socket_options,
        )

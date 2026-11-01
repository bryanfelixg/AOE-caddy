"""Windows named-pipe transport and V3 read-only protocol client.

Uses one connection per request, exactly as Agent Bridge V3 specifies.
No gameplay action API is exposed by this module.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import time
from typing import Any, Callable

ALLOWED_OPERATIONS = frozenset({"status", "open", "observe", "map", "close"})
MAX_REQUEST = 1 << 20
MAX_RESPONSE = 16 << 20
ERROR_MORE_DATA = 234
RETRYABLE_WINERRORS = {2, 231}  # FILE_NOT_FOUND, PIPE_BUSY


class BridgeError(Exception):
    """Base class for bridge transport and protocol errors."""


class BridgeTransportError(BridgeError):
    pass


class BridgeProtocolError(BridgeError):
    pass


class BridgeRemoteError(BridgeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def pipe_request(player: int, request: dict[str, Any], *, timeout_s: float = 6.0) -> dict[str, Any]:
    """Send and receive one UTF-8 JSON message over Agent Bridge V3 pipe.

    On Windows, CreateFileW can return ERROR_FILE_NOT_FOUND while CONTROL starts,
    or ERROR_PIPE_BUSY while another request is in flight. Only those connection
    errors are retried. Connection/read/write timeout policy is enforced by the
    bridge (5s); `timeout_s` bounds connection retrying, not the entire call.
    """
    if os.name != "nt":
        raise BridgeTransportError("Windows is required for named-pipe telemetry")
    if player not in range(1, 9):
        raise ValueError("player must be 1..8")
    body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_REQUEST:
        raise BridgeProtocolError("request exceeds 1 MiB")

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.CreateFileW.restype = wt.HANDLE
    k32.SetNamedPipeHandleState.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD), wt.LPVOID, wt.LPVOID]
    k32.SetNamedPipeHandleState.restype = wt.BOOL
    k32.WriteFile.argtypes = [wt.HANDLE, wt.LPCVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
    k32.WriteFile.restype = wt.BOOL
    k32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
    k32.ReadFile.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.CloseHandle.restype = wt.BOOL

    pipe_name = rf"\\.\pipe\AoE2ControlAgentV3.P{player}"
    invalid = ctypes.c_void_p(-1).value
    deadline = time.monotonic() + timeout_s
    handle = None
    while True:
        candidate = k32.CreateFileW(pipe_name, 0xC0000000, 0, None, 3, 0, None)
        if candidate != invalid and candidate is not None:
            handle = candidate
            break
        code = ctypes.get_last_error()
        if code not in RETRYABLE_WINERRORS or time.monotonic() >= deadline:
            raise BridgeTransportError(f"Cannot open {pipe_name}: winerror={code}")
        time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))

    try:
        mode = wt.DWORD(2)  # PIPE_READMODE_MESSAGE
        if not k32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            raise BridgeTransportError(f"SetNamedPipeHandleState failed: {ctypes.get_last_error()}")
        sent = wt.DWORD(0)
        if not k32.WriteFile(handle, body, len(body), ctypes.byref(sent), None):
            raise BridgeTransportError(f"WriteFile failed: {ctypes.get_last_error()}")
        if sent.value != len(body):
            raise BridgeTransportError(f"Short write: {sent.value}/{len(body)}")
        chunks: list[bytes] = []
        total = 0
        buffer = ctypes.create_string_buffer(65536)
        while True:
            received = wt.DWORD(0)
            ok = k32.ReadFile(handle, buffer, len(buffer), ctypes.byref(received), None)
            error = 0 if ok else ctypes.get_last_error()
            if not ok and error != ERROR_MORE_DATA:
                raise BridgeTransportError(f"ReadFile failed: winerror={error}")
            total += received.value
            if total > MAX_RESPONSE:
                raise BridgeProtocolError("response exceeds 16 MiB")
            chunks.append(buffer.raw[:received.value])
            if ok:
                break
        try:
            response = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise BridgeProtocolError("response is not valid UTF-8 JSON") from exc
        if not isinstance(response, dict):
            raise BridgeProtocolError("response must be a JSON object")
        return response
    finally:
        k32.CloseHandle(handle)


class ReadOnlyBridge:
    def __init__(self, player: int = 1, transport: Callable[..., dict[str, Any]] = pipe_request):
        if player not in range(1, 9):
            raise ValueError("player must be 1..8")
        self.player = player
        self.transport = transport
        self.session: str | None = None

    def request(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"Operation {operation!r} is not allowed by read-only client")
        request: dict[str, Any] = {"contract_version": 3, "operation": operation, "payload": payload or {}}
        if operation not in ("status", "open"):
            if not self.session:
                raise BridgeProtocolError("No active session; call open() first")
            request["session"] = self.session
        response = self.transport(self.player, request)
        if response.get("ok") is not True:
            error = response.get("error") or {}
            raise BridgeRemoteError(str(error.get("code", "unknown_error")), str(error.get("message", "")))
        data = response.get("data")
        if not isinstance(data, dict):
            raise BridgeProtocolError(f"{operation} response has no object data")
        return data

    def status(self) -> dict[str, Any]:
        return self.request("status")

    def open(self) -> dict[str, Any]:
        if self.session:
            raise BridgeProtocolError("Session already open")
        data = self.request("open", {"client_name": "aoe2-coach-ro"})
        if data.get("information_policy") != "player_view":
            session = data.get("session")
            if isinstance(session, str):
                self.session = session
                try:
                    self.close()
                except BridgeError:
                    pass
            raise BridgeProtocolError("Refusing omniscient mode; turn off 'Modules See Everything'")
        session = data.get("session")
        if not isinstance(session, str) or not session:
            raise BridgeProtocolError("Missing session ID from open response")
        self.session = session
        return data

    def observe(self, *, technologies: bool = False) -> dict[str, Any]:
        """Newest observation; `technologies=True` adds the ~40 KB technology state list."""
        data = self.request("observe", {"object_types": "none", "technologies": "all" if technologies else "none"})
        if data.get("information_policy") != "player_view":
            raise BridgeProtocolError("Observation is not player_view")
        return data

    def map(self) -> dict[str, Any]:
        return self.request("map")

    def close(self) -> None:
        if self.session:
            try:
                self.request("close")
            finally:
                self.session = None

    def __enter__(self) -> "ReadOnlyBridge":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

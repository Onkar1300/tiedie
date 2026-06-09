import asyncio
import binascii
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import serial


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Advertisement:
    mac_address: str
    rssi: int


@dataclass(frozen=True)
class ConnectionEvent:
    mac_address: str
    connected: bool


class ZephyrSerialBackend:
    """USB-serial transport for a Zephyr-based nRF BLE coprocessor.

    Protocol (line oriented, UTF-8, '\n' terminated):

    Requests from host (Pi) -> dongle:
      REQ,<id>,<CMD>[,<arg>...]

    Responses/events from dongle -> host:
      ADV,<mac>,<rssi>,<payload_len>
      RSP,<id>,OK[,<tag>[,<data>...]]
      RSP,<id>,ERR,<message>
      SVC,<id>,<svc_uuid>
      CHR,<id>,<svc_uuid>,<char_uuid>,<props>

    `DISCOVER` is multi-line:
      RSP,<id>,OK,DISCOVER_BEGIN
      (SVC/CHR lines)
      RSP,<id>,OK,DISCOVER_END

    The backend is safe to share across multiple RPCs; it reference-counts users and keeps
    a single reader thread.
    """

    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        baudrate: int = 460800,
        timeout_s: float = 1.0,
        request_timeout_s: float = 20.0,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout_s = timeout_s
        self._request_timeout_s = request_timeout_s

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ser: Optional[serial.Serial] = None

        self._reader_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._write_lock = threading.Lock()

        self._id_lock = threading.Lock()
        self._next_id = 1

        self._pending: dict[int, asyncio.Future] = {}
        self._discover_state: dict[int, dict] = {}

        self._ref_lock = asyncio.Lock()
        self._refcount = 0

        self._on_adv: Optional[Callable[[Advertisement], None]] = None
        self._on_conn_event: Optional[Callable[[ConnectionEvent], None]] = None

    @property
    def request_timeout_s(self) -> float:
        return self._request_timeout_s

    async def acquire(
        self,
        *,
        on_adv: Optional[Callable[[Advertisement], None]] = None,
        on_conn_event: Optional[Callable[[ConnectionEvent], None]] = None
    ) -> None:
        async with self._ref_lock:
            self._refcount += 1
            if on_adv is not None:
                self._on_adv = on_adv
            if on_conn_event is not None:
                self._on_conn_event = on_conn_event
            if self._refcount == 1:
                self._start()

    async def release(self) -> None:
        async with self._ref_lock:
            self._refcount = max(0, self._refcount - 1)
            if self._refcount == 0:
                self._stop()

    def _start(self) -> None:
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return

        self._loop = asyncio.get_running_loop()
        self._stop_evt.clear()

        try:
            self._ser = serial.Serial(self._port, baudrate=self._baudrate, timeout=self._timeout_s)
            try:
                # Firmware waits for DTR before enabling radio.
                self._ser.dtr = True
            except Exception:
                logger.warning("Unable to set DTR on %s", self._port)
        except Exception as e:
            raise RuntimeError(f"Failed to open serial port {self._port}: {e}")

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="zephyr-serial-backend",
            daemon=True,
        )
        self._reader_thread.start()

    def _stop(self) -> None:
        self._stop_evt.set()
        t = self._reader_thread
        self._reader_thread = None

        if t is not None and t.is_alive():
            t.join(timeout=2.0)

        ser = self._ser
        self._ser = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

        # Fail any pending requests.
        if self._loop is not None:
            for req_id, fut in list(self._pending.items()):
                if not fut.done():
                    self._loop.call_soon_threadsafe(fut.set_exception, RuntimeError("Serial backend stopped"))
            self._pending.clear()
            self._discover_state.clear()

    def _reader_loop(self) -> None:
        assert self._loop is not None
        assert self._ser is not None

        _pending: list = []
        _last_flush = time.monotonic()

        while not self._stop_evt.is_set():
            try:
                raw = self._ser.readline()
            except Exception:
                logger.exception("Serial read failed")
                break

            if not raw:
                # readline timeout — flush any pending ads
                if _pending:
                    self._flush_advs(_pending)
                    _pending = []
                    _last_flush = time.monotonic()
                continue

            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            # Fast path: parse ADV lines directly in the reader thread.
            parts = [p.strip() for p in line.split(",")]
            adv_mac = None
            adv_rssi_str = None

            if len(parts) >= 4 and parts[0].upper() == "ADV":
                if "." in parts[1] and parts[2].count(":") == 5:
                    adv_mac, adv_rssi_str = parts[2], parts[3]
                elif parts[1].count(":") == 5:
                    adv_mac, adv_rssi_str = parts[1], parts[2]
            elif len(parts) == 3 and parts[0].count(":") == 5:
                adv_mac, adv_rssi_str = parts[0], parts[1]

            if adv_mac is not None and adv_rssi_str is not None:
                # Validate MAC: must be exactly XX:XX:XX:XX:XX:XX
                if len(adv_mac) != 17 or adv_mac.count(":") != 5:
                    self._loop.call_soon_threadsafe(self._handle_line, line)
                    continue

                try:
                    rssi_f = float(adv_rssi_str)
                    if not (-120 <= rssi_f <= 0):
                        raise ValueError(f"RSSI {rssi_f} out of BLE range")
                    _pending.append(Advertisement(mac_address=adv_mac, rssi=int(rssi_f)))
                except ValueError:
                    logger.debug("Skipping malformed ADV line: %r", line)

                now = time.monotonic()
                # Flush once per 50 ads OR every 10ms — only ~10 asyncio calls/sec
                if len(_pending) >= 50 or (now - _last_flush) >= 0.01:
                    self._flush_advs(_pending)
                    _pending = []
                    _last_flush = now
                continue

            # Slow path (RSP/EVENT/SVC/CHR): flush pending first, then dispatch
            if _pending:
                self._flush_advs(_pending)
                _pending = []
                _last_flush = time.monotonic()

            try:
                self._loop.call_soon_threadsafe(self._handle_line, line)
            except Exception:
                break

    def _flush_advs(self, advs: list) -> None:
        """Send a snapshot of accumulated ads to the asyncio event loop in one call."""
        if not advs or self._on_adv is None:
            return
        snapshot = list(advs)
        try:
            self._loop.call_soon_threadsafe(self._dispatch_adv_batch, snapshot)
        except Exception:
            pass

    def _dispatch_adv_batch(self, advs: list) -> None:
        """Called inside the asyncio event loop — invoke _on_adv for each ad."""
        cb = self._on_adv
        if cb is None:
            return
        for adv in advs:
            try:
                cb(adv)
            except Exception:
                logger.exception("Advertisement callback failed")

    def _handle_line(self, line: str) -> None:
        # Advertisement lines (new compact): ADV,<ts_s.ms>,<mac>,<rssi>
        # Advertisement lines (older): ADV,<mac>,<rssi>,<len>
        # Legacy simple: <mac>,<rssi>,<len>
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4 and parts[0].upper() == "ADV":
            # New compact form: ADV,<ts>,<mac>,<rssi>
            # Older form: ADV,<mac>,<rssi>,<len>
            # Heuristic: if parts[1] contains a '.' it's the timestamp
            if "." in parts[1] and parts[2].count(":") == 5:
                mac, rssi_str = parts[2], parts[3]
                self._emit_adv(mac, rssi_str)
                return
            # fallback to older ordering
            if parts[1].count(":") == 5:
                mac, rssi_str = parts[1], parts[2]
                self._emit_adv(mac, rssi_str)
                return
        if len(parts) == 3 and parts[0].count(":") == 5:
            mac, rssi_str = parts[0], parts[1]
            self._emit_adv(mac, rssi_str)
            return
            return

        if len(parts) >= 3 and parts[0].upper() == "RSP":
            self._handle_rsp(parts)
            return

        if len(parts) >= 3 and parts[0].upper() in {"SVC", "CHR"}:
            self._handle_discover_parts(parts)
            return

        if len(parts) >= 3 and parts[0].upper() == "EVENT":
            if parts[1].upper() == "CONNECT":
                self._emit_conn_event(parts[2], True)
            elif parts[1].upper() == "DISCONNECT":
                self._emit_conn_event(parts[2], False)
            return

        # Unknown line; ignore.

    def _emit_conn_event(self, mac: str, connected: bool) -> None:
        if self._on_conn_event is None:
            return
        try:
            self._on_conn_event(ConnectionEvent(mac_address=mac, connected=connected))
        except Exception:
            logger.exception("Connection event callback failed")

    def _emit_adv(self, mac: str, rssi_str: str) -> None:
        if self._on_adv is None:
            return
        try:
            rssi = int(rssi_str)
        except ValueError:
            return
        try:
            self._on_adv(Advertisement(mac_address=mac, rssi=rssi))
        except Exception:
            logger.exception("Advertisement callback failed")

    def _handle_rsp(self, parts: list[str]) -> None:
        # RSP,<id>,OK[,tag]
        # RSP,<id>,ERR,<message>
        try:
            req_id = int(parts[1])
        except ValueError:
            return

        status = parts[2].upper()

        if status == "ERR":
            msg = parts[3] if len(parts) >= 4 else ""
            fut = self._pending.pop(req_id, None)
            self._discover_state.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_result({"ok": False, "error": msg})
            return

        if status != "OK":
            return

        tag = parts[3].upper() if len(parts) >= 4 else ""

        if tag == "DISCOVER_BEGIN":
            self._discover_state[req_id] = {"services": [], "chars": []}
            return

        if tag == "DISCOVER_END":
            fut = self._pending.pop(req_id, None)
            state = self._discover_state.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_result({"ok": True, "discover": state or {"services": [], "chars": []}})
            return

        # Generic OK response
        fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.set_result({"ok": True, "tag": tag, "parts": parts[3:]})

    def _handle_discover_parts(self, parts: list[str]) -> None:
        kind = parts[0].upper()
        try:
            req_id = int(parts[1])
        except ValueError:
            return

        state = self._discover_state.get(req_id)
        if state is None:
            return

        if kind == "SVC" and len(parts) >= 3:
            state["services"].append({"uuid": parts[2]})
            return

        if kind == "CHR" and len(parts) >= 5:
            state["chars"].append(
                {
                    "svc_uuid": parts[2],
                    "uuid": parts[3],
                    "props": parts[4],
                }
            )

    def _next_request_id(self) -> int:
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        return req_id

    def _write_line(self, line: str) -> None:
        if self._ser is None:
            raise RuntimeError("Serial port is not open")
        data = (line.strip() + "\n").encode("utf-8")
        with self._write_lock:
            self._ser.write(data)
            self._ser.flush()

    async def _request(self, *parts: str, timeout_s: Optional[float] = None) -> dict:
        req_id = self._next_request_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        try:
            self._write_line(",".join(["REQ", str(req_id), *parts]))
        except Exception as e:
            self._pending.pop(req_id, None)
            raise RuntimeError(str(e))

        timeout = self._request_timeout_s if timeout_s is None else float(timeout_s)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            self._discover_state.pop(req_id, None)
            raise RuntimeError(f"Timeout waiting for response to {parts[0]}")

    async def connect(self, mac_address: str) -> None:
        resp = await self._request("CONNECT", mac_address)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "Connect failed")

    async def disconnect(self, mac_address: str) -> None:
        resp = await self._request("DISCONNECT", mac_address)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "Disconnect failed")

    async def discover(self, mac_address: str, *, timeout_s: Optional[float] = None) -> dict:
        resp = await self._request("DISCOVER", mac_address, timeout_s=timeout_s)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "Discover failed")
        return resp.get("discover") or {"services": [], "chars": []}

    async def read(self, mac_address: str, characteristic_uuid: str) -> bytes:
        resp = await self._request("READ", mac_address, characteristic_uuid)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "Read failed")

        # Expected: RSP,id,OK,READ,<hex>
        parts = resp.get("parts") or []
        tag = resp.get("tag")
        if tag != "READ" or len(parts) < 2:
            raise RuntimeError("Malformed READ response")
        hex_str = parts[1]
        try:
            return binascii.unhexlify(hex_str)
        except Exception as e:
            raise RuntimeError(f"Invalid hex in READ response: {e}")

    async def write(self, mac_address: str, characteristic_uuid: str, value: bytes) -> None:
        hex_str = binascii.hexlify(value).decode("ascii")
        resp = await self._request("WRITE", mac_address, characteristic_uuid, hex_str)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "Write failed")

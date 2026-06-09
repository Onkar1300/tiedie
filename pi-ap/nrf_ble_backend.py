import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Advertisement:
    mac_address: str
    rssi: int


@dataclass
class ConnectionState:
    mac_address: str
    conn_handle: int
    char_uuid_map: Dict[str, object]  # uuid_str -> pc_ble_driver_py.ble_driver.BLEUUID


class NrfBleBackend:
    """BLE backend using Nordic pc-ble-driver-py (SoftDevice serialization over UART).

    This backend is synchronous/blocking. Call it from an executor when used
    in an asyncio gRPC server.
    """

    def __init__(
        self,
        *,
        serial_port: Optional[str] = None,
        baud_rate: int = 1_000_000,
        conn_ic_id: str = "NRF52",
    ):
        self._serial_port = serial_port or os.environ.get("NRF_SERIAL_PORT")
        self._baud_rate = int(os.environ.get("NRF_BAUD_RATE", baud_rate))
        self._conn_ic_id = os.environ.get("NRF_CONN_IC_ID", conn_ic_id)
        self._sd_api_version = int(os.environ.get("NRF_SD_API_VERSION", "5"))

        self._driver = None
        self._adapter = None
        self._adv_observer = None

        self._known_addr_types: Dict[str, int] = {}
        self._connections: Dict[str, ConnectionState] = {}

        self._scan_callback: Optional[Callable[[Advertisement], None]] = None
        self._scan_running = False

        self._lock = threading.RLock()

    # -----------------
    # Lifecycle / setup
    # -----------------

    def _ensure_open(self) -> None:
        with self._lock:
            if self._driver is not None:
                return

            if sys.version_info >= (3, 11):
                raise RuntimeError(
                    "pc-ble-driver-py requires Python < 3.11. "
                    "Install Python 3.10 on the Raspberry Pi and recreate the venv using python3.10, then reinstall requirements."
                )

            if not self._serial_port:
                raise RuntimeError(
                    "NRF_SERIAL_PORT is not set (e.g. /dev/ttyACM0)."
                )

            # pc-ble-driver-py requires config.__conn_ic_id__ to be set
            # before importing most of the package.
            import pc_ble_driver_py.config as nrf_config

            nrf_config.__conn_ic_id__ = self._conn_ic_id
            # pc-ble-driver-py (0.17.0) supports SoftDevice API v2 and v5.
            # Default to v5, configurable via NRF_SD_API_VERSION.
            nrf_config.api_version = self._sd_api_version

            from pc_ble_driver_py.ble_adapter import BLEAdapter
            from pc_ble_driver_py.ble_driver import (
                BLEConfig,
                BLEConfigCommon,
                BLEConfigConnGap,
                BLEConfigConnGatt,
                BLEConfigConnGattc,
                BLEConfigConnGatts,
                BLEConfigGapRoleCount,
                BLEDriver,
            )
            from pc_ble_driver_py.observers import BLEDriverObserver

            class _AdvObserver(BLEDriverObserver):
                def __init__(self, outer: "NrfBleBackend"):
                    self._outer = outer

                # Signature follows BLEDriverObserver implementation.
                def on_gap_evt_adv_report(
                    self,
                    ble_driver,
                    conn_handle,
                    peer_addr,
                    rssi,
                    adv_type,
                    adv_data,
                ):
                    try:
                        mac = _format_mac(peer_addr.addr)
                        with self._outer._lock:
                            self._outer._known_addr_types[mac] = _addr_type_to_int(
                                peer_addr.addr_type
                            )
                            cb = self._outer._scan_callback

                        if cb is not None:
                            cb(Advertisement(mac_address=mac, rssi=int(rssi)))
                    except Exception:
                        logger.exception("Failed handling advertisement report")

            logger.info(
                "Opening nRF BLE driver on %s (baud=%s, ic=%s)",
                self._serial_port,
                self._baud_rate,
                self._conn_ic_id,
            )

            self._driver = BLEDriver(
                serial_port=self._serial_port,
                baud_rate=self._baud_rate,
                auto_flash=False,
            )

            # Configure before enabling BLE.
            try:
                self._driver.ble_cfg_set(
                    BLEConfig.role_count,
                    BLEConfigGapRoleCount(
                        central_role_count=1,
                        periph_role_count=0,
                        central_sec_count=1,
                    ),
                )
                self._driver.ble_cfg_set(BLEConfig.conn_gap, BLEConfigConnGap(conn_count=1))
                self._driver.ble_cfg_set(
                    BLEConfig.conn_gatt,
                    BLEConfigConnGatt(att_mtu=int(os.environ.get("NRF_ATT_MTU", "247"))),
                )
                self._driver.ble_cfg_set(BLEConfig.conn_gattc, BLEConfigConnGattc())
                self._driver.ble_cfg_set(BLEConfig.conn_gatts, BLEConfigConnGatts())
                self._driver.ble_cfg_set(
                    BLEConfig.uuid_count,
                    BLEConfigCommon(vs_uuid_count=int(os.environ.get("NRF_VS_UUID_COUNT", "10"))),
                )
            except Exception:
                # Keep running with defaults if the underlying firmware/API
                # doesn't accept a config we tried to set.
                logger.exception("Failed applying BLE config; continuing with defaults")

            self._driver.open()
            self._driver.ble_enable()

            self._adapter = BLEAdapter(self._driver)

            self._adv_observer = _AdvObserver(self)
            self._driver.observer_register(self._adv_observer)

    # ----------
    # Scanning
    # ----------

    def start_scan(self, callback: Callable[[Advertisement], None]) -> None:
        with self._lock:
            self._ensure_open()
            self._scan_callback = callback

            if self._scan_running:
                return

            from pc_ble_driver_py.ble_driver import BLEGapScanParams

            interval_ms = int(os.environ.get("NRF_SCAN_INTERVAL_MS", "100"))
            window_ms = int(os.environ.get("NRF_SCAN_WINDOW_MS", str(interval_ms)))
            active = os.environ.get("NRF_SCAN_ACTIVE", "0").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

            # timeout_s=0 means "no timeout" (continuous scanning)
            scan_params = BLEGapScanParams(
                interval_ms=interval_ms,
                window_ms=window_ms,
                timeout_s=0,
                active=active,
            )

            self._driver.ble_gap_scan_start(scan_params=scan_params)
            self._scan_running = True
            logger.info("nRF scan started")

    def stop_scan(self) -> None:
        with self._lock:
            if not self._driver or not self._scan_running:
                self._scan_callback = None
                return

            try:
                self._driver.ble_gap_scan_stop()
            finally:
                self._scan_running = False
                self._scan_callback = None
                logger.info("nRF scan stopped")

    # ----------------
    # Connections/GATT
    # ----------------

    def connect(self, mac_address: str, timeout_s: float = 30.0) -> bool:
        """Connects (central role). Returns True on success."""
        mac = _normalize_mac(mac_address)
        with self._lock:
            self._ensure_open()
            if mac in self._connections:
                return True

        addr_bytes = _parse_mac(mac)

        from pc_ble_driver_py.ble_driver import BLEGapAddr

        addr_type = self._known_addr_types.get(mac, BLEGapAddr.Types.public.value)
        addr_types_to_try = [addr_type]
        if addr_type != BLEGapAddr.Types.public.value:
            addr_types_to_try.append(BLEGapAddr.Types.public.value)
        # Common in advertisements: random static.
        if addr_type != BLEGapAddr.Types.random_static.value:
            addr_types_to_try.append(BLEGapAddr.Types.random_static.value)

        for candidate_type in addr_types_to_try:
            if self._connect_with_type(
                mac=mac,
                addr_bytes=addr_bytes,
                addr_type=candidate_type,
                timeout_s=timeout_s,
            ):
                return True

        return False

    def _connect_with_type(
        self,
        *,
        mac: str,
        addr_bytes: Tuple[int, int, int, int, int, int],
        addr_type: int,
        timeout_s: float,
    ) -> bool:
        from pc_ble_driver_py.ble_driver import BLEGapAddr

        address = BLEGapAddr(addr_type=addr_type, addr=list(addr_bytes))

        with self._lock:
            before = set(self._adapter.db_conns.keys())
            self._adapter.connect(address)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                # When conn attempt completes, adapter.conn_in_progress will flip.
                if not self._adapter.conn_in_progress:
                    after = set(self._adapter.db_conns.keys())
                    new_handles = list(after - before)
                    for h in new_handles:
                        try:
                            conn = self._adapter.db_conns[h]
                        except KeyError:
                            continue

                        if _format_mac(conn.peer_addr.addr) == mac:
                            self._connections[mac] = ConnectionState(
                                mac_address=mac,
                                conn_handle=int(h),
                                char_uuid_map={},
                            )
                            logger.info("Connected to %s (conn_handle=%s)", mac, h)
                            return True
                    # conn attempt ended but no connection created
                    return False

            time.sleep(0.05)

        logger.warning("Connect timeout to %s", mac)
        return False

    def disconnect(self, mac_address: str, timeout_s: float = 10.0) -> bool:
        mac = _normalize_mac(mac_address)
        with self._lock:
            state = self._connections.get(mac)
            if state is None:
                return True
            conn_handle = state.conn_handle

        try:
            self._adapter.disconnect(conn_handle)
        except Exception:
            logger.exception("Disconnect call failed")

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if conn_handle not in self._adapter.db_conns:
                    self._connections.pop(mac, None)
                    return True
            time.sleep(0.05)

        return False

    def discover(self, mac_address: str, *, cached_ok: bool = True) -> Dict:
        """Runs service discovery and returns a python structure describing it."""
        mac = _normalize_mac(mac_address)
        with self._lock:
            state = self._connections.get(mac)
            if state is None:
                raise RuntimeError("Not connected")
            conn_handle = state.conn_handle

            services_existing = self._adapter.db_conns[conn_handle].services
            if cached_ok and services_existing:
                return self._snapshot_gatt(mac)

        # Blocking discovery.
        self._adapter.service_discovery(conn_handle)

        with self._lock:
            # Build map from uuid string -> BLEUUID object for read/write by UUID.
            uuid_map: Dict[str, object] = {}
            for service in self._adapter.db_conns[conn_handle].services:
                for ch in service.chars:
                    uuid_map[_uuid_to_str(ch.uuid)] = ch.uuid
            self._connections[mac].char_uuid_map = uuid_map
            return self._snapshot_gatt(mac)

    def _snapshot_gatt(self, mac: str) -> Dict:
        state = self._connections[mac]
        conn = self._adapter.db_conns[state.conn_handle]

        services = []
        for s in conn.services:
            chars = []
            for ch in s.chars:
                chars.append(
                    {
                        "uuid": _uuid_to_str(ch.uuid),
                        "flags": _char_props_to_flags(ch.char_props),
                    }
                )
            services.append({"uuid": _uuid_to_str(s.uuid), "characteristics": chars})

        return {"services": services}

    def read(self, mac_address: str, characteristic_uuid: str) -> bytes:
        mac = _normalize_mac(mac_address)
        uuid_str = characteristic_uuid.lower()

        from pc_ble_driver_py.ble_driver import BLEUUID

        with self._lock:
            state = self._connections.get(mac)
            if state is None:
                raise RuntimeError("Not connected")

            conn_handle = state.conn_handle
            uuid_obj = state.char_uuid_map.get(uuid_str)

        if uuid_obj is None:
            # Best-effort parse for standard UUIDs.
            uuid_obj = _parse_uuid(uuid_str)

        status, data = self._adapter.read_req(conn_handle, uuid_obj)
        if data is None:
            raise RuntimeError(f"Read failed with status {status}")
        return bytes(data)

    def write(self, mac_address: str, characteristic_uuid: str, value: bytes) -> None:
        mac = _normalize_mac(mac_address)
        uuid_str = characteristic_uuid.lower()

        with self._lock:
            state = self._connections.get(mac)
            if state is None:
                raise RuntimeError("Not connected")
            conn_handle = state.conn_handle
            uuid_obj = state.char_uuid_map.get(uuid_str)

        if uuid_obj is None:
            uuid_obj = _parse_uuid(uuid_str)

        # write_req raises if status != success.
        self._adapter.write_req(conn_handle, uuid_obj, list(value))


def _normalize_mac(mac: str) -> str:
    return mac.strip().upper()


def _parse_mac(mac: str) -> Tuple[int, int, int, int, int, int]:
    parts = mac.split(":")
    if len(parts) != 6:
        raise ValueError(f"Invalid MAC address '{mac}'")
    return tuple(int(p, 16) for p in parts)  # type: ignore[return-value]


def _format_mac(addr_bytes) -> str:
    return ":".join(f"{b:02X}" for b in addr_bytes)


def _addr_type_to_int(addr_type) -> int:
    return int(getattr(addr_type, "value", addr_type))


def _uuid_to_str(uuid_obj) -> str:
    # uuid_obj is pc_ble_driver_py.ble_driver.BLEUUID
    base = getattr(uuid_obj.base, "base", None)
    if base is None:
        # Fallback: best effort.
        return str(uuid_obj)

    value = uuid_obj.value.value if hasattr(uuid_obj.value, "value") else int(uuid_obj.value)

    full = list(base)
    # Insert 16-bit value into bytes[2:4] (big endian) for standard BLE UUID layout.
    full[2] = (value >> 8) & 0xFF
    full[3] = value & 0xFF

    hexstr = "".join(f"{b:02x}" for b in full)
    return f"{hexstr[0:8]}-{hexstr[8:12]}-{hexstr[12:16]}-{hexstr[16:20]}-{hexstr[20:32]}"


def _parse_uuid(uuid_str: str):
    """Parse a canonical 128-bit UUID string into a pc_ble_driver_py BLEUUID.

    This is best-effort: it assumes the UUID is in standard BLE-base form and
    converts it to a 16-bit BLEUUID. For vendor-specific UUIDs, prefer doing
    `Discover` first so we can reuse BLEUUID objects from discovery.
    """

    from pc_ble_driver_py.ble_driver import BLEUUID, BLEUUIDBase

    u = uuid_str.strip().lower()
    u = u.replace("{", "").replace("}", "")
    if len(u) == 4:
        return BLEUUID(value=int(u, 16), base=BLEUUIDBase())

    if len(u) != 36:
        raise ValueError(f"Invalid UUID '{uuid_str}'")

    hexstr = u.replace("-", "")
    b = [int(hexstr[i : i + 2], 16) for i in range(0, 32, 2)]

    # Standard BLE base: 0000xxxx-0000-1000-8000-00805f9b34fb
    if b[0] == 0x00 and b[1] == 0x00 and b[4] == 0x00 and b[5] == 0x00 and b[6] == 0x10:
        val = (b[2] << 8) | b[3]
        return BLEUUID(value=val, base=BLEUUIDBase())

    # Vendor-specific: keep base bytes, extract 16-bit value from bytes[2:4].
    val = (b[2] << 8) | b[3]
    base = b[:]
    base[0] = 0x00
    base[1] = 0x00
    base[2] = 0x00
    base[3] = 0x00
    return BLEUUID(value=val, base=BLEUUIDBase(vs_uuid_base=base, uuid_type=None))


def _char_props_to_flags(props) -> list[str]:
    # props is pc_ble_driver_py.ble_driver.BLECharProperties namedtuple
    flags = []
    if getattr(props, "read", False):
        flags.append("read")
    if getattr(props, "write", False) or getattr(props, "write_wo_resp", False):
        flags.append("write")
    if getattr(props, "notify", False):
        flags.append("notify")
    if getattr(props, "indicate", False):
        flags.append("indicate")
    return flags

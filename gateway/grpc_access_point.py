# Copyright (c) 2026

"""gRPC-backed AccessPoint (stub).

Checkpoint 1 requires that the gateway can be started with `--device grpc`
without changing REST APIs. This implementation is intentionally minimal:
- Boots cleanly (sets `ready`).
- BLE operations raise the existing BLE*Error types with a clear message.

Later checkpoints will replace these stubs with real gRPC calls.
"""

from __future__ import annotations

from access_point import AccessPoint, BleConnectOptions
from access_point_responses import (
    BleConnectionError,
    BleDiscoveryError,
    BleDisconnectError,
    BleReadError,
    BleSubscribeError,
    BleUnsubscribeError,
    BleWriteError,
)


class GrpcAccessPoint(AccessPoint):
    """Stub AccessPoint that will later forward BLE ops over gRPC."""

    _ERR = "gRPC access point backend not implemented yet"

    def start(self):
        self.log.info("gRPC AP backend selected (stub)")
        self.ready.set()

    def stop(self):
        self.log.info("Stopping gRPC AP backend (stub)")

    def connectable(self):
        return False

    def start_scan(self):
        # Scanning/adv forwarding will be implemented in later checkpoints.
        self.log.info("start_scan ignored for gRPC AP backend (stub)")

    def connect(self, address: str, ble_connect_options: BleConnectOptions, retries: int = 3) -> None:  # noqa: ARG002
        raise BleConnectionError(self._ERR)

    def discover(self, address: str, ble_connect_options: BleConnectOptions, retries: int = 3):  # noqa: ARG002
        raise BleDiscoveryError(self._ERR)

    def read(self, address: str, service_uuid: str, char_uuid: str):  # noqa: ARG002
        raise BleReadError(self._ERR)

    def write(self, address: str, service_uuid: str, char_uuid: str, value: str):  # noqa: ARG002
        raise BleWriteError(self._ERR)

    def subscribe(self, address: str, service_uuid: str, char_uuid: str):  # noqa: ARG002
        raise BleSubscribeError(self._ERR)

    def unsubscribe(self, address: str, service_uuid: str, char_uuid: str):  # noqa: ARG002
        raise BleUnsubscribeError(self._ERR)

    def disconnect(self, address: str) -> None:  # noqa: ARG002
        raise BleDisconnectError(self._ERR)

import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class HealthCheckRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthCheckResponse(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: str
    def __init__(self, status: _Optional[str] = ...) -> None: ...

class BleConnectOptions(_message.Message):
    __slots__ = ("service_uuids", "cached", "cache_idle_purge_seconds")
    SERVICE_UUIDS_FIELD_NUMBER: _ClassVar[int]
    CACHED_FIELD_NUMBER: _ClassVar[int]
    CACHE_IDLE_PURGE_SECONDS_FIELD_NUMBER: _ClassVar[int]
    service_uuids: _containers.RepeatedScalarFieldContainer[str]
    cached: bool
    cache_idle_purge_seconds: int
    def __init__(self, service_uuids: _Optional[_Iterable[str]] = ..., cached: bool = ..., cache_idle_purge_seconds: _Optional[int] = ...) -> None: ...

class ConnectRequest(_message.Message):
    __slots__ = ("mac_address", "options", "retries")
    MAC_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    RETRIES_FIELD_NUMBER: _ClassVar[int]
    mac_address: str
    options: BleConnectOptions
    retries: int
    def __init__(self, mac_address: _Optional[str] = ..., options: _Optional[_Union[BleConnectOptions, _Mapping]] = ..., retries: _Optional[int] = ...) -> None: ...

class ConnectResponse(_message.Message):
    __slots__ = ("connected", "error")
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    connected: bool
    error: str
    def __init__(self, connected: bool = ..., error: _Optional[str] = ...) -> None: ...

class DiscoverRequest(_message.Message):
    __slots__ = ("mac_address", "options", "retries")
    MAC_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    RETRIES_FIELD_NUMBER: _ClassVar[int]
    mac_address: str
    options: BleConnectOptions
    retries: int
    def __init__(self, mac_address: _Optional[str] = ..., options: _Optional[_Union[BleConnectOptions, _Mapping]] = ..., retries: _Optional[int] = ...) -> None: ...

class GattCharacteristic(_message.Message):
    __slots__ = ("uuid", "flags")
    UUID_FIELD_NUMBER: _ClassVar[int]
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    flags: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, uuid: _Optional[str] = ..., flags: _Optional[_Iterable[str]] = ...) -> None: ...

class GattService(_message.Message):
    __slots__ = ("uuid", "characteristics")
    UUID_FIELD_NUMBER: _ClassVar[int]
    CHARACTERISTICS_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    characteristics: _containers.RepeatedCompositeFieldContainer[GattCharacteristic]
    def __init__(self, uuid: _Optional[str] = ..., characteristics: _Optional[_Iterable[_Union[GattCharacteristic, _Mapping]]] = ...) -> None: ...

class DiscoverResponse(_message.Message):
    __slots__ = ("services", "error")
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    services: _containers.RepeatedCompositeFieldContainer[GattService]
    error: str
    def __init__(self, services: _Optional[_Iterable[_Union[GattService, _Mapping]]] = ..., error: _Optional[str] = ...) -> None: ...

class ReadRequest(_message.Message):
    __slots__ = ("mac_address", "service_uuid", "characteristic_uuid")
    MAC_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    SERVICE_UUID_FIELD_NUMBER: _ClassVar[int]
    CHARACTERISTIC_UUID_FIELD_NUMBER: _ClassVar[int]
    mac_address: str
    service_uuid: str
    characteristic_uuid: str
    def __init__(self, mac_address: _Optional[str] = ..., service_uuid: _Optional[str] = ..., characteristic_uuid: _Optional[str] = ...) -> None: ...

class ReadResponse(_message.Message):
    __slots__ = ("value", "error")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    value: bytes
    error: str
    def __init__(self, value: _Optional[bytes] = ..., error: _Optional[str] = ...) -> None: ...

class WriteRequest(_message.Message):
    __slots__ = ("mac_address", "service_uuid", "characteristic_uuid", "value")
    MAC_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    SERVICE_UUID_FIELD_NUMBER: _ClassVar[int]
    CHARACTERISTIC_UUID_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    mac_address: str
    service_uuid: str
    characteristic_uuid: str
    value: bytes
    def __init__(self, mac_address: _Optional[str] = ..., service_uuid: _Optional[str] = ..., characteristic_uuid: _Optional[str] = ..., value: _Optional[bytes] = ...) -> None: ...

class WriteResponse(_message.Message):
    __slots__ = ("success", "error")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    success: bool
    error: str
    def __init__(self, success: bool = ..., error: _Optional[str] = ...) -> None: ...

class DisconnectRequest(_message.Message):
    __slots__ = ("mac_address",)
    MAC_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    mac_address: str
    def __init__(self, mac_address: _Optional[str] = ...) -> None: ...

class DisconnectResponse(_message.Message):
    __slots__ = ("disconnected", "error")
    DISCONNECTED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    disconnected: bool
    error: str
    def __init__(self, disconnected: bool = ..., error: _Optional[str] = ...) -> None: ...

class AdvertisementStreamRequest(_message.Message):
    __slots__ = ("ap_ip",)
    AP_IP_FIELD_NUMBER: _ClassVar[int]
    ap_ip: str
    def __init__(self, ap_ip: _Optional[str] = ...) -> None: ...

class AdvertisementEvent(_message.Message):
    __slots__ = ("mac_address", "rssi", "ap_ip", "timestamp")
    MAC_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    RSSI_FIELD_NUMBER: _ClassVar[int]
    AP_IP_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    mac_address: str
    rssi: int
    ap_ip: str
    timestamp: _timestamp_pb2.Timestamp
    def __init__(self, mac_address: _Optional[str] = ..., rssi: _Optional[int] = ..., ap_ip: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class AdvertisementBatch(_message.Message):
    __slots__ = ("events",)
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[AdvertisementEvent]
    def __init__(self, events: _Optional[_Iterable[_Union[AdvertisementEvent, _Mapping]]] = ...) -> None: ...

class ConnectionStreamRequest(_message.Message):
    __slots__ = ("ap_ip",)
    AP_IP_FIELD_NUMBER: _ClassVar[int]
    ap_ip: str
    def __init__(self, ap_ip: _Optional[str] = ...) -> None: ...

class ConnectionEvent(_message.Message):
    __slots__ = ("mac_address", "connected", "ap_ip", "timestamp")
    MAC_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    AP_IP_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    mac_address: str
    connected: bool
    ap_ip: str
    timestamp: _timestamp_pb2.Timestamp
    def __init__(self, mac_address: _Optional[str] = ..., connected: bool = ..., ap_ip: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

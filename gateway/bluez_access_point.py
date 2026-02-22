import asyncio
import threading
import logging
from typing import Optional

from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

from access_point import AccessPoint, BleConnectOptions, ConnectionRequest, Service
from access_point_responses import (
    BleConnectionError, BleDisconnectError, BleDiscoveryError, BleReadError,
    BleSubscribeError, BleUnsubscribeError, BleWriteError, DiscoverResponse,
    ReadResponse, SubscribeResponse, UnsubscribeResponse, WriteResponse
)
from data_producer import DataProducer

class Characteristic:
    def __init__(self, uuid: str, handle: int, properties: list[str]):
        self.uuid = uuid
        self.handle = handle
        self.properties = properties

class BluezAccessPoint(AccessPoint):
    def __init__(self, data_producer: DataProducer):
        super().__init__(data_producer)
        self.log = logging.getLogger(__name__)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.clients: dict[str, BleakClient] = {}
        self.scanner: Optional[BleakScanner] = None

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run_coro(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result()

    def start(self):
        self.thread.start()
        self.ready.set()

    def stop(self):
        if self.scanner:
            self._run_coro(self.scanner.stop())
        for client in list(self.clients.values()):
            self._run_coro(client.disconnect())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()

    def connectable(self):
        return True

    def start_scan(self):
        asyncio.run_coroutine_threadsafe(self._scan(), self.loop)

    async def _scan(self):
        class AdvEvent:
            def __init__(self, address, data, rssi):
                self.address = address
                self.data = data
                self.rssi = rssi

        def detection_callback(device, advertisement_data):
            # Send data to data_producer
            # This is a simplified version, you might need to format it properly
            # based on what data_producer expects
            data = b''
            if advertisement_data.manufacturer_data:
                for k, v in advertisement_data.manufacturer_data.items():
                    data += k.to_bytes(2, 'little') + v
            evt = AdvEvent(device.address, data, device.rssi)
            self.data_producer.publish_advertisement(evt)
        
        self.scanner = BleakScanner(detection_callback)
        await self.scanner.start()
        # Keep scanning until stopped
        while True:
            await asyncio.sleep(1.0)

    def connect(self, address: str, ble_connect_options: BleConnectOptions, retries: int = 3) -> None:
        if address in self.clients and self.clients[address].is_connected:
            raise BleConnectionError("already connected")
        
        def disconnected_callback(client):
            class Evt:
                reason = "disconnected"
            self.data_producer.publish_connection_status(Evt(), address, False)
            if address in self.clients:
                del self.clients[address]
            if address in self.conn_reqs:
                del self.conn_reqs[address]

        client = BleakClient(address, disconnected_callback=disconnected_callback)
        try:
            self._run_coro(client.connect())
            self.clients[address] = client
            self.conn_reqs[address] = ConnectionRequest(address, 0, {})
            class Evt:
                reason = "connected"
            self.data_producer.publish_connection_status(Evt(), address, True)
        except Exception as e:
            raise BleConnectionError(f"connection failed: {e}")

    def discover(self, address: str, ble_connect_options: BleConnectOptions, retries: int = 3) -> DiscoverResponse:
        if address not in self.clients or not self.clients[address].is_connected:
            raise BleDiscoveryError("not connected")
        
        client = self.clients[address]
        services_dict = {}
        try:
            services = self._run_coro(client.get_services())
            for service in services:
                chars = {}
                for char in service.characteristics:
                    chars[char.uuid] = Characteristic(char.uuid, char.handle, char.properties)
                services_dict[service.uuid] = Service(service.uuid, service.handle, chars)
            
            self.conn_reqs[address].services = services_dict
            return DiscoverResponse(address=address, services=services_dict)
        except Exception as e:
            raise BleDiscoveryError(f"discovery failed: {e}")

    def read(self, address: str, service_uuid: str, char_uuid: str) -> ReadResponse:
        if address not in self.clients or not self.clients[address].is_connected:
            raise BleReadError("not connected")
        
        client = self.clients[address]
        try:
            data = self._run_coro(client.read_gatt_char(char_uuid))
            return ReadResponse(address=address, service_uuid=service_uuid, char_uuid=char_uuid, value=data.hex())
        except Exception as e:
            raise BleReadError(f"read failed: {e}")

    def write(self, address: str, service_uuid: str, char_uuid: str, value: str) -> WriteResponse:
        if address not in self.clients or not self.clients[address].is_connected:
            raise BleWriteError("not connected")
        
        client = self.clients[address]
        try:
            data = bytes.fromhex(value)
            self._run_coro(client.write_gatt_char(char_uuid, data))
            return WriteResponse(address=address, service_uuid=service_uuid, char_uuid=char_uuid)
        except Exception as e:
            raise BleWriteError(f"write failed: {e}")

    def subscribe(self, address: str, service_uuid: str, char_uuid: str) -> SubscribeResponse:
        if address not in self.clients or not self.clients[address].is_connected:
            raise BleSubscribeError("not connected")
        
        client = self.clients[address]
        
        def notification_handler(sender, data):
            self.data_producer.publish_notification(address, service_uuid, char_uuid, data)
            
        try:
            self._run_coro(client.start_notify(char_uuid, notification_handler))
            return SubscribeResponse(address=address, service_uuid=service_uuid, char_uuid=char_uuid)
        except Exception as e:
            raise BleSubscribeError(f"subscribe failed: {e}")

    def unsubscribe(self, address: str, service_uuid: str, char_uuid: str) -> UnsubscribeResponse:
        if address not in self.clients or not self.clients[address].is_connected:
            raise BleUnsubscribeError("not connected")
        
        client = self.clients[address]
        try:
            self._run_coro(client.stop_notify(char_uuid))
            return UnsubscribeResponse(address=address, service_uuid=service_uuid, char_uuid=char_uuid)
        except Exception as e:
            raise BleUnsubscribeError(f"unsubscribe failed: {e}")

    def disconnect(self, address: str) -> None:
        if address not in self.clients:
            raise BleDisconnectError("not connected")
        
        client = self.clients[address]
        try:
            self._run_coro(client.disconnect())
            # disconnected_callback will handle cleanup and publishing
        except Exception as e:
            raise BleDisconnectError(f"disconnect failed: {e}")

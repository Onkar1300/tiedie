# Copyright (c) 2026

"""gRPC-backed AccessPoint."""

from __future__ import annotations

import asyncio
import logging
import threading
import grpc
from typing import Dict, Optional
from datetime import datetime

from access_point import AccessPoint, BleConnectOptions, ConnectionRequest
from access_point_responses import (
    DiscoverResponse,
    BleConnectionError,
    BleDiscoveryError,
    BleDisconnectError,
    BleReadError,
    BleSubscribeError,
    BleUnsubscribeError,
    BleWriteError,
)
from ble_types import Service, Characteristic

from grpc_proto import access_point_pb2
from grpc_proto import access_point_pb2_grpc
from database import session
from nipc_models import BleExtension
from sqlalchemy import select


class GrpcAccessPoint(AccessPoint):
    """AccessPoint that forwards BLE ops over gRPC to Raspberry Pis."""

    def __init__(self, data_producer):
        super().__init__(data_producer)
        self.mac_to_ip: Dict[str, str] = {}
        self.channels: Dict[str, grpc.Channel] = {}
        self.stubs: Dict[str, access_point_pb2_grpc.AccessPointServiceStub] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._load_mapping()
        self._init_grpc_channels()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _load_mapping(self):
        try:
            with open("../mapping.txt", "r") as f:
                current_ip = None
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.count('.') == 3 and ':' not in line:
                        current_ip = line
                    elif current_ip and line.count(':') == 5:
                        self.mac_to_ip[line.upper()] = current_ip
            self.log.info(f"Loaded {len(self.mac_to_ip)} mappings from mapping.txt")
        except FileNotFoundError:
            self.log.warning("mapping.txt not found. Multi-Pi routing will fail.")

    def _init_grpc_channels(self):
        try:
            with open("certs/server.key", "rb") as f:
                client_key = f.read()
            with open("certs/server.crt", "rb") as f:
                client_cert = f.read()
            with open("ca_certificates/ca.pem", "rb") as f:
                root_cert = f.read()

            credentials = grpc.ssl_channel_credentials(
                root_certificates=root_cert,
                private_key=client_key,
                certificate_chain=client_cert
            )
        except Exception as e:
            self.log.error(f"Failed to load mTLS certs: {e}")
            credentials = grpc.local_channel_credentials() # Fallback or just fail

        unique_ips = set(self.mac_to_ip.values())
        for ip in unique_ips:
            # We use an async channel for streaming and operations
            # Note: since grpcio 1.32, async is supported. Here we use synchronous calls wrapped, 
            # or we can use async channel. Since AccessPoint methods are synchronous, we'll use synchronous channels for unary
            # and async for streaming if needed. Actually we can use sync channel for unary.
            channel = grpc.secure_channel(
                f"{ip}:50051",
                credentials,
                options=(('grpc.ssl_target_name_override', 'server'),)
            )
            self.channels[ip] = channel
            self.stubs[ip] = access_point_pb2_grpc.AccessPointServiceStub(channel)

    def _get_stub(self, mac_address: str) -> Optional[access_point_pb2_grpc.AccessPointServiceStub]:
        ip = self.mac_to_ip.get(mac_address.upper())
        if ip and ip in self.stubs:
            return self.stubs[ip]
        return None

    def start(self):
        self.log.info("Starting gRPC AP backend")
        self._thread.start()
        self.ready.set()

    def stop(self):
        self.log.info("Stopping gRPC AP backend")
        for channel in self.channels.values():
            channel.close()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    def connectable(self):
        return True

    def start_scan(self):
        self.log.info("Starting advertisement and connection streams from all Pis...")
        for ip, stub in self.stubs.items():
            asyncio.run_coroutine_threadsafe(self._stream_advs(ip, stub), self._loop)
            asyncio.run_coroutine_threadsafe(self._stream_conn_events(ip, stub), self._loop)

    async def _stream_conn_events(self, ip: str, stub: access_point_pb2_grpc.AccessPointServiceStub):
        class ConnEventDummy:
            def __init__(self, reason):
                self.reason = reason

        try:
            req = access_point_pb2.ConnectionStreamRequest(ap_ip=ip)
            def stream_sync():
                for evt in stub.StreamConnectionEvents(req):
                    self.log.info(f"Received connection event: mac={evt.mac_address} connected={evt.connected} from {ip}")
                    
                    # Update local connection state if needed
                    if not evt.connected:
                        self.conn_reqs.pop(evt.mac_address, None)

                    self.data_producer.publish_connection_status(
                        ConnEventDummy(reason="Streamed event"), 
                        evt.mac_address, 
                        evt.connected
                    )
            await self._loop.run_in_executor(None, stream_sync)
        except Exception as e:
            self.log.error(f"Error streaming connection events from {ip}: {e}")

    async def _stream_advs(self, ip: str, stub: access_point_pb2_grpc.AccessPointServiceStub):
        # We need an async channel for this if we use async generator
        # Since we initialized a sync channel, we can use it in a thread, or just use sync generator.
        # Stub from sync channel returns a normal generator for stream.
        class AdvEvent:
            def __init__(self, address, rssi, data):
                self.address = address
                self.rssi = rssi
                self.data = data
                
        try:
            req = access_point_pb2.AdvertisementStreamRequest(ap_ip=ip)
            # Run blocking iteration in an executor to avoid blocking the event loop
            def stream_sync():
                import queue
                import threading

                # Queue to hold incoming ads so gRPC stream is consumed instantly
                adv_q = queue.Queue(maxsize=100000)

                # Separate MQTT queue: file-writing is fast, MQTT/DB can lag behind
                mqtt_q = queue.Queue(maxsize=100000)

                def mqtt_worker():
                    while True:
                        adv = mqtt_q.get()
                        if adv is None:
                            break
                        self.data_producer.publish_advertisement(
                            AdvEvent(adv.mac_address, adv.rssi, b"")
                        )
                        mqtt_q.task_done()

                def worker():
                    with open("ads_log.txt", "a") as ads_file:
                        while True:
                            adv = adv_q.get()
                            if adv is None:
                                break

                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                            try:
                                adv_ts = adv.timestamp.ToJsonString() if adv.timestamp else ""
                            except Exception:
                                adv_ts = ""

                            # Write to file immediately (fast path - not blocked by DB)
                            ads_file.write(
                                f"{timestamp}: INFO - Received adv: mac={adv.mac_address} rssi={adv.rssi} ap_ip={adv.ap_ip} adv_ts={adv_ts} from {ip}\n"
                            )

                            # Hand off to MQTT worker (non-blocking)
                            try:
                                mqtt_q.put_nowait(adv)
                            except queue.Full:
                                pass  # MQTT can drop; file count is authoritative

                            adv_q.task_done()

                threading.Thread(target=mqtt_worker, daemon=True).start()

                t = threading.Thread(target=worker, daemon=True)
                t.start()

                for batch in stub.StreamAdvertisements(req):
                    for adv in batch.events:
                        try:
                            adv_q.put_nowait(adv)
                        except queue.Full:
                            self.log.warning("Gateway local adv queue full, dropping ad")
                        
            await self._loop.run_in_executor(None, stream_sync)
        except Exception as e:
            self.log.error(f"Error streaming advertisements from {ip}: {e}")

    def connect(self, address: str, ble_connect_options: BleConnectOptions, retries: int = 3) -> None:
        stub = self._get_stub(address)
        if not stub:
            raise BleConnectionError(f"No Pi mapped for MAC {address}")
        
        req = access_point_pb2.ConnectRequest(
            mac_address=address,
            retries=retries,
            options=access_point_pb2.BleConnectOptions(
                service_uuids=ble_connect_options.services,
                cached=ble_connect_options.cached,
                cache_idle_purge_seconds=ble_connect_options.cache_idle_purge
            )
        )
        try:
            self.log.info(f"Initiating gRPC Connect to Pi for {address}...")
            resp = stub.Connect(req, timeout=60)
            self.log.info(f"gRPC Connect returned: connected={resp.connected}, error='{resp.error}'")
            if not resp.connected:
                raise BleConnectionError(resp.error)
            
            # Store AP IP in DB after successful connect
            ip = self.mac_to_ip[address.upper()]
            ext = session.scalar(select(BleExtension).filter_by(device_mac_address=address))
            if ext:
                ext.ap_ip = ip
                session.commit()
                self.log.info(f"Updated ap_ip to {ip} for {address}")

        except Exception as e:
            raise BleConnectionError(str(e))

    def discover(self, address: str, ble_connect_options: BleConnectOptions, retries: int = 3):
        stub = self._get_stub(address)
        if not stub:
            raise BleDiscoveryError(f"No Pi mapped for MAC {address}")
        
        req = access_point_pb2.DiscoverRequest(
            mac_address=address,
            retries=retries,
            options=access_point_pb2.BleConnectOptions(
                service_uuids=ble_connect_options.services,
                cached=ble_connect_options.cached,
                cache_idle_purge_seconds=ble_connect_options.cache_idle_purge
            )
        )
        try:
            self.log.info(f"Initiating gRPC Discover to Pi for {address}...")
            # Discovery can take longer than connect, depending on the peripheral and
            # on how many services/characteristics need to be enumerated.
            resp = stub.Discover(req, timeout=120)
            self.log.info(f"gRPC Discover returned: {len(resp.services)} services found, error='{resp.error}'")
            if resp.error:
                raise BleDiscoveryError(resp.error)
            
            # Convert to BleGattService objects for gateway mapping
            services_list = []
            services_dict = {}
            for s_pb in resp.services:
                chars = {}
                for c_pb in s_pb.characteristics:
                    char = Characteristic(c_pb.uuid, 0, 0)
                    char.properties = list(c_pb.flags)
                    chars[c_pb.uuid] = char
                svc = Service(s_pb.uuid, 0, chars)
                services_list.append(svc)
                services_dict[s_pb.uuid] = svc
                
            self.conn_reqs[address] = ConnectionRequest(address, 0, services_dict)
            
            return DiscoverResponse(address=address, services=services_list)
        except Exception as e:
            raise BleDiscoveryError(str(e))

    def read(self, address: str, service_uuid: str, char_uuid: str):
        stub = self._get_stub(address)
        if not stub:
            raise BleReadError(f"No Pi mapped for MAC {address}")
        
        req = access_point_pb2.ReadRequest(
            mac_address=address,
            service_uuid=service_uuid,
            characteristic_uuid=char_uuid
        )
        try:
            resp = stub.Read(req, timeout=10)
            if resp.error:
                raise BleReadError(resp.error)
            
            class DummyResp:
                def __init__(self, val):
                    self.value = val
            return DummyResp(resp.value)
        except Exception as e:
            raise BleReadError(str(e))

    def write(self, address: str, service_uuid: str, char_uuid: str, value: bytes):
        stub = self._get_stub(address)
        if not stub:
            raise BleWriteError(f"No Pi mapped for MAC {address}")
        
        req = access_point_pb2.WriteRequest(
            mac_address=address,
            service_uuid=service_uuid,
            characteristic_uuid=char_uuid,
            value=value
        )
        try:
            resp = stub.Write(req, timeout=10)
            if not resp.success:
                raise BleWriteError(resp.error)
        except Exception as e:
            raise BleWriteError(str(e))

    def subscribe(self, address: str, service_uuid: str, char_uuid: str):
        raise BleSubscribeError("gRPC subscribe not fully implemented")

    def unsubscribe(self, address: str, service_uuid: str, char_uuid: str):
        raise BleUnsubscribeError("gRPC unsubscribe not fully implemented")

    def disconnect(self, address: str) -> None:
        stub = self._get_stub(address)
        if not stub:
            raise BleDisconnectError(f"No Pi mapped for MAC {address}")
        
        req = access_point_pb2.DisconnectRequest(mac_address=address)
        try:
            resp = stub.Disconnect(req, timeout=5)
            if not resp.disconnected:
                raise BleDisconnectError(resp.error)
            
            # Remove from local connection tracking
            self.conn_reqs.pop(address, None)
        except Exception as e:
            raise BleDisconnectError(str(e))

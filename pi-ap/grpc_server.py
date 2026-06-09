import asyncio
import logging
import os
import queue
from typing import AsyncIterable

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from grpc_proto import access_point_pb2
from grpc_proto import access_point_pb2_grpc

from zephyr_serial_backend import Advertisement, ConnectionEvent, ZephyrSerialBackend


logger = logging.getLogger(__name__)


class AccessPointServiceServicer(access_point_pb2_grpc.AccessPointServiceServicer):
    """Pi Access Point implementation that reads BLE scan results from an nRF52840 dongle.

    The dongle runs custom Zephyr firmware that prints scan results over USB-Serial (CDC ACM)
    as a single line: `MAC,RSSI,PAYLOAD_LEN`.

    The Pi does not do BLE directly; it only forwards these events to the gateway over gRPC.
    """

    def __init__(self, ap_ip: str):
        self.ap_ip = ap_ip

        self._serial_port = os.environ.get("ZEPHYR_SERIAL_PORT", os.environ.get("SERIAL_PORT", "/dev/ttyACM0"))
        self._serial_baudrate = int(
            os.environ.get("ZEPHYR_SERIAL_BAUDRATE", os.environ.get("SERIAL_BAUDRATE", "921600"))
        )
        self._serial_timeout_s = float(os.environ.get("ZEPHYR_SERIAL_TIMEOUT_S", "1.0"))
        # DISCOVER can be slow on some peripherals (multiple services/characteristics),
        # so default higher than CONNECT/READ/WRITE.
        self._request_timeout_s = float(os.environ.get("ZEPHYR_REQUEST_TIMEOUT_S", "90.0"))

        self._backend = ZephyrSerialBackend(
            port=self._serial_port,
            baudrate=self._serial_baudrate,
            timeout_s=self._serial_timeout_s,
            request_timeout_s=self._request_timeout_s,
        )

        # asyncio.Queue is safe here: _queue_advertisement is always called
        # from within the asyncio event loop (via _dispatch_adv_batch).
        self.adv_queue: asyncio.Queue = asyncio.Queue(maxsize=100000)
        self.conn_event_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._streaming = False
        self._streaming_conn = False

    def _queue_conn_event(self, event: ConnectionEvent) -> None:
        if not self._streaming_conn:
            return
        try:
            self.conn_event_queue.put_nowait({"mac_address": event.mac_address, "connected": event.connected})
        except asyncio.QueueFull:
            pass

    def _queue_advertisement(self, adv: Advertisement) -> None:
        if not self._streaming:
            return
        try:
            self.adv_queue.put_nowait({"mac_address": adv.mac_address, "rssi": int(adv.rssi)})
        except asyncio.QueueFull:
            pass

    async def Health(
        self,
        request: access_point_pb2.HealthCheckRequest,
        context,
    ) -> access_point_pb2.HealthCheckResponse:
        return access_point_pb2.HealthCheckResponse(status="SERVING")

    async def StreamAdvertisements(
        self,
        request: access_point_pb2.AdvertisementStreamRequest,
        context,
    ) -> AsyncIterable[access_point_pb2.AdvertisementBatch]:
        self._streaming = True
        logger.info("Started streaming advertisements to gateway.")

        try:
            try:
                await self._backend.acquire(on_adv=self._queue_advertisement)
            except Exception as e:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))

            while True:
                batch_events = []
                # Wait for first item natively in asyncio - no executor needed
                event = await self.adv_queue.get()
                ts = Timestamp()
                ts.GetCurrentTime()
                batch_events.append(
                    access_point_pb2.AdvertisementEvent(
                        mac_address=event["mac_address"],
                        rssi=event["rssi"],
                        ap_ip=self.ap_ip,
                        timestamp=ts,
                    )
                )

                # Drain everything currently queued (non-blocking)
                while len(batch_events) < 200:
                    try:
                        event = self.adv_queue.get_nowait()
                        ts = Timestamp()
                        ts.GetCurrentTime()
                        batch_events.append(
                            access_point_pb2.AdvertisementEvent(
                                mac_address=event["mac_address"],
                                rssi=event["rssi"],
                                ap_ip=self.ap_ip,
                                timestamp=ts,
                            )
                        )
                    except asyncio.QueueEmpty:
                        break

                yield access_point_pb2.AdvertisementBatch(events=batch_events)
        except asyncio.CancelledError:
            logger.info("Client disconnected from advertisement stream.")
        finally:
            self._streaming = False
            try:
                await self._backend.release()
            except Exception:
                logger.exception("Failed to release serial backend")

    async def StreamConnectionEvents(
        self,
        request: access_point_pb2.ConnectionStreamRequest,
        context,
    ) -> AsyncIterable[access_point_pb2.ConnectionEvent]:
        self._streaming_conn = True
        logger.info("Started streaming connection events to gateway.")

        try:
            try:
                await self._backend.acquire(on_conn_event=self._queue_conn_event)
            except Exception as e:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))

            while True:
                event = await self.conn_event_queue.get()
                ts = Timestamp()
                ts.GetCurrentTime()

                yield access_point_pb2.ConnectionEvent(
                    mac_address=event["mac_address"],
                    connected=event["connected"],
                    ap_ip=self.ap_ip,
                    timestamp=ts,
                )
        except asyncio.CancelledError:
            logger.info("Client disconnected from connection event stream.")
        finally:
            self._streaming_conn = False
            try:
                await self._backend.release()
            except Exception:
                logger.exception("Failed to release serial backend")

    async def Connect(
        self,
        request: access_point_pb2.ConnectRequest,
        context,
    ) -> access_point_pb2.ConnectResponse:
        mac_address = request.mac_address
        retries = max(0, int(request.retries))

        last_error: str = ""
        await self._backend.acquire()
        try:
            for attempt in range(retries + 1):
                try:
                    await self._backend.connect(mac_address)
                    return access_point_pb2.ConnectResponse(connected=True)
                except Exception as e:
                    last_error = str(e)
                    logger.error(
                        "Failed to connect to %s (attempt %s/%s): %s",
                        mac_address,
                        attempt + 1,
                        retries + 1,
                        e,
                    )
            return access_point_pb2.ConnectResponse(connected=False, error=last_error)
        finally:
            await self._backend.release()

    async def Discover(
        self,
        request: access_point_pb2.DiscoverRequest,
        context,
    ) -> access_point_pb2.DiscoverResponse:
        mac_address = request.mac_address
        retries = max(0, int(request.retries))

        last_error: str = ""
        await self._backend.acquire()
        try:
            for attempt in range(retries + 1):
                try:
                    remaining = None
                    try:
                        remaining = context.time_remaining()
                    except Exception:
                        remaining = None

                    # Keep a safety margin so we return an explicit error
                    # instead of hitting gRPC DEADLINE_EXCEEDED.
                    per_call_timeout = None
                    if remaining is not None:
                        # time_remaining can be negative if already expired
                        if remaining <= 1.0:
                            last_error = "Discover aborted: gRPC deadline too close"
                            break
                        per_call_timeout = max(0.5, min(self._backend.request_timeout_s, remaining - 1.0))

                    snapshot = await self._backend.discover(mac_address, timeout_s=per_call_timeout)

                    # Assemble services -> characteristics
                    services_by_uuid: dict[str, list[access_point_pb2.GattCharacteristic]] = {}
                    for svc in snapshot.get("services", []):
                        uuid = svc.get("uuid", "")
                        if uuid and uuid not in services_by_uuid:
                            services_by_uuid[uuid] = []

                    for ch in snapshot.get("chars", []):
                        svc_uuid = ch.get("svc_uuid", "")
                        ch_uuid = ch.get("uuid", "")
                        props = ch.get("props", "")
                        if not svc_uuid or not ch_uuid:
                            continue
                        if svc_uuid not in services_by_uuid:
                            services_by_uuid[svc_uuid] = []

                        flags = [p for p in props.split("|") if p]
                        services_by_uuid[svc_uuid].append(
                            access_point_pb2.GattCharacteristic(uuid=ch_uuid, flags=flags)
                        )

                    services_pb: list[access_point_pb2.GattService] = []
                    for svc_uuid, chars_pb in services_by_uuid.items():
                        services_pb.append(
                            access_point_pb2.GattService(uuid=svc_uuid, characteristics=chars_pb)
                        )

                    return access_point_pb2.DiscoverResponse(services=services_pb)
                except Exception as e:
                    last_error = str(e)
                    logger.error(
                        "Failed to discover services for %s (attempt %s/%s): %s",
                        mac_address,
                        attempt + 1,
                        retries + 1,
                        e,
                    )

                    # If we're close to the gRPC deadline, don't keep retrying.
                    try:
                        remaining = context.time_remaining()
                        if remaining is not None and remaining <= 1.0:
                            break
                    except Exception:
                        pass

            return access_point_pb2.DiscoverResponse(error=last_error)
        finally:
            await self._backend.release()

    async def Read(
        self,
        request: access_point_pb2.ReadRequest,
        context,
    ) -> access_point_pb2.ReadResponse:
        mac_address = request.mac_address
        await self._backend.acquire()
        try:
            data = await self._backend.read(mac_address, request.characteristic_uuid)
            return access_point_pb2.ReadResponse(value=data)
        except Exception as e:
            logger.error("Failed to read from %s: %s", mac_address, e)
            return access_point_pb2.ReadResponse(error=str(e))
        finally:
            await self._backend.release()

    async def Write(
        self,
        request: access_point_pb2.WriteRequest,
        context,
    ) -> access_point_pb2.WriteResponse:
        mac_address = request.mac_address
        await self._backend.acquire()
        try:
            await self._backend.write(
                mac_address,
                request.characteristic_uuid,
                bytes(request.value),
            )
            return access_point_pb2.WriteResponse(success=True)
        except Exception as e:
            logger.error("Failed to write to %s: %s", mac_address, e)
            return access_point_pb2.WriteResponse(success=False, error=str(e))
        finally:
            await self._backend.release()

    async def Disconnect(
        self,
        request: access_point_pb2.DisconnectRequest,
        context,
    ) -> access_point_pb2.DisconnectResponse:
        mac_address = request.mac_address
        await self._backend.acquire()
        try:
            await self._backend.disconnect(mac_address)
            return access_point_pb2.DisconnectResponse(disconnected=True)
        except Exception as e:
            logger.error("Failed to disconnect from %s: %s", mac_address, e)
            return access_point_pb2.DisconnectResponse(disconnected=False, error=str(e))
        finally:
            await self._backend.release()

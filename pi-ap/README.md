# TieDie Pi Access Point (Zephyr USB Serial Dongle)

This gRPC server talks to the TieDie gateway and forwards BLE operations to an nRF52840 USB dongle running custom Zephyr firmware (over USB Serial / CDC ACM).

The dongle scans and prints a single CSV line per advertisement over USB Serial (CDC ACM):

- `MAC,RSSI,PAYLOAD_LEN`

The Pi AP opens the serial port, asserts DTR (required by the firmware to start scanning), and:

- streams advertisement events to the gateway
- sends `CONNECT/DISCOVER/READ/WRITE/DISCONNECT` commands to the dongle when the gateway calls those gRPC APIs

## Requirements

- Raspberry Pi
- nRF52840 USB Dongle flashed with your custom Zephyr firmware
- Python 3.9+ on the Pi

## Configuration

Environment variables (optional):

- `ZEPHYR_SERIAL_PORT` (default `/dev/ttyACM0`)
- `ZEPHYR_SERIAL_BAUDRATE` (default `921600`)
- `ZEPHYR_SERIAL_TIMEOUT_S` (default `1.0`)

Note: the firmware waits for DTR before enabling Bluetooth. The Pi AP sets `ser.dtr = True` after opening the port.

## Supported gRPC calls

- `StreamAdvertisements`: supported
- `Connect`/`Disconnect`: supported
- `Discover`: supported (service/characteristic UUIDs + flags)
- `Read`/`Write`: supported by characteristic UUID

Note: the firmware caches discovered characteristic handles in RAM. If `READ/WRITE` returns "UUID not found; run DISCOVER first", call `Discover` once after connecting.

## Firmware

The reference Zephyr firmware implementing the host protocol lives in [tiedie/pi-ap/zephyr-firmware/main.c](tiedie/pi-ap/zephyr-firmware/main.c).

## Run

Install dependencies from `requirements.txt`, then start the server as usual (mTLS args unchanged):

- `python main.py --ip <pi-ip> --port 50051 --ca-cert <ca.pem> --server-cert <server.crt> --server-key <server.key>`

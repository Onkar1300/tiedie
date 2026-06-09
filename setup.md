# TieDie Setup Guide

This document provides a consolidated setup guide for the TieDie project, highlighting the generation of initial certificates, starting the backend/gateway, and setting up the Raspberry Pi access point (gRPC server).

## 1. Initial Certificates and Setup

The gateway uses TLS for the SCIM and NIPC APIs. Certificates must be generated before starting the services.

### Generate a new CA cert for testing
```bash
cd tiedie/gateway/certs
./make-ca-certs.sh
```

### Generate Server Certs
This creates the server private key and certificate required for the gateway:
```bash
./gen_cert.sh server
```

## 2. Start the Backend and gRPC Services (Windows Laptop)

The backend gateway handles operations, SCIM APIs, and data integrations. It also hosts the central gRPC server that the Raspberry Pi access points tunnel into.

### Running Natively on Windows
To run the complete gateway and gRPC server on your Windows host:

1. Create a virtual environment and install the requirements:
   ```bash
   cd tiedie/gateway
   python3 -m venv venv
   source venv/bin/activate
   pip3 install -r requirements.txt
   ```

2. Bring up the required backend services (Mosquitto and Postgres):
   ```bash
   docker compose up mosquitto postgres -d
   ```

3. Start the gateway application:
   ```bash
   # Using newer grpc tunnel
   python3 app.py --device grpc
   ```

## 3. Start and Setup of Raspberry Pi (gRPC Client / Access Point)

The Windows laptop acts as the central gRPC server and Gateway. The Raspberry Pi functions strictly as a BLE worker (Access Point) that connects to the Gateway over a gRPC tunnel to perform device operations (Connect, Disconnect, Read, Write).

### Prerequisites
- Raspberry Pi with Python 3.9+ Host OS
- BlueZ/Bleak available (or nRF52840 USB Dongle flashed with custom Zephyr firmware)
- TLS Certificates generated during the setup steps

### Configuration
You can configure the Pi's local BLE capabilities or serial connection using environment variables (if continuing to use the Zephyr dongle):
- `ZEPHYR_SERIAL_PORT` (default `/dev/ttyACM0`)
- `ZEPHYR_SERIAL_BAUDRATE` (default `921600`)
- `ZEPHYR_SERIAL_TIMEOUT_S` (default `1.0`)

### Installation & Startup Commands
Transfer your required project code and certificates to the Raspberry Pi. From within the `tiedie/pi-ap` directory, install requirements and launch the client connecting *to* your Windows laptop:

```bash
cd tiedie/pi-ap

# Install the necessary python dependencies
pip install -r requirements.txt

# Start the Pi AP, providing the IP and port of the Windows laptop (gRPC server)
python main.py \
  --server-ip <windows-laptop-ip> \
  --server-port 50051 \
  --ca-cert <path-to-ca.pem> \
  --client-cert <path-to-client.crt> \
  --client-key <path-to-client.key>
```
## 4. Experiments
### To check advertisements delivered over time for a certain interval of nrf - 
```bash
#duration - total interval for which experiment needs to be conducted
#output - Name of file. In this case, 7 devices, 700 adv interval
python gateway_logging.py --duration 200 --output 7_700
```
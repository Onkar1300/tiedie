import argparse
import csv
import os
import random
import time
from datetime import datetime

from tiedie.api.auth import ApiKeyAuthenticator
from tiedie.api.control_client import ControlClient
from tiedie.api.onboarding_client import OnboardingClient
from tiedie.models import ListResponse
from tiedie.models.requests import BleConnectRequest, BleProtocolInformation, TiedieConnectRequest
from tiedie.models.responses import BleDiscoverResponse, NipcResponse
from tiedie.models.scim import (
    Application,
    BleExtension,
    Device,
    EndpointApp,
    EndpointAppType,
    EndpointAppsExtension,
)

EXPERIMENTS_DIR = os.path.abspath(os.path.dirname(__file__))
LOGS_DIR = os.path.join(EXPERIMENTS_DIR, "logs")
DEFAULT_MAPPING_PATH = os.path.abspath(os.path.join(EXPERIMENTS_DIR, "..", "..", "mapping.txt"))
DEFAULT_CONFIG_PATH = os.path.abspath(
    os.path.join(EXPERIMENTS_DIR, "..", "sample-python-app", "config", "config.ini")
)


def load_flat_config(path: str) -> dict:
    config: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"").strip("'")
            if key:
                config[key] = value
    return config


def parse_mapping_macs(path: str) -> list[str]:
    macs: list[str] = []
    skipped_first = False
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if not skipped_first:
                skipped_first = True
                continue
            if line.count(":") == 5:
                macs.append(line.upper())
    return macs


def build_onboarding_client(cfg: dict) -> OnboardingClient:
    ca_path = cfg.get("CLIENT_CA_PATH")
    base_url = cfg.get("ONBOARDING_APP_BASE_URL")
    app_id = cfg.get("ONBOARDING_APP_ID")
    api_key = cfg.get("ONBOARDING_APP_API_KEY")

    if not ca_path or not base_url or not app_id or not api_key:
        raise ValueError("Missing required onboarding config values in config.ini")

    authenticator = ApiKeyAuthenticator(app_id, ca_path, api_key)
    return OnboardingClient(base_url, authenticator)


def build_control_client(cfg: dict, onboarding_client: OnboardingClient) -> ControlClient:
    ca_path = cfg.get("CLIENT_CA_PATH")
    base_url = cfg.get("CONTROL_APP_BASE_URL")
    control_app_id = cfg.get("CONTROL_APP_ID")

    if not ca_path or not base_url or not control_app_id:
        raise ValueError("Missing required control config values in config.ini")

    apps = ensure_endpoint_apps(cfg, onboarding_client)
    control_app = next(
        (
            app
            for app in apps
            if app.application_type == EndpointAppType.DEVICE_CONTROL
            and app.application_name == control_app_id
        ),
        None,
    )
    if control_app is None or not control_app.client_token:
        raise RuntimeError("Control app not found or missing client token")

    authenticator = ApiKeyAuthenticator(control_app_id, ca_path, control_app.client_token)
    return ControlClient(base_url, authenticator)


def collect_device_map(onboarding_client: OnboardingClient) -> dict[str, object]:
    device_map: dict[str, Device] = {}

    # SCIM list responses are commonly paginated. If we only fetch the first page,
    # we can miss existing devices and then get 409 Conflict on create.
    start_index = 1
    page_size = 200

    while True:
        response = onboarding_client.http_client.get(
            onboarding_client.base_url + "/Devices",
            headers=onboarding_client.headers,
            params={"startIndex": start_index, "count": page_size},
            verify=False,
        )
        mapped = onboarding_client._map_response(response, ListResponse[Device])
        body = mapped.body
        resources = body.resources if body and body.resources else []

        for device in resources:
            if device.ble_extension and device.ble_extension.device_mac_address:
                device_map[device.ble_extension.device_mac_address.upper()] = device

        if not body:
            break

        total = body.total_results
        if total is not None and len(device_map) >= total:
            break

        # Stop if we got fewer than requested (end of list)
        if len(resources) < page_size:
            break

        start_index += page_size

    return device_map


def ensure_endpoint_apps(cfg: dict, onboarding_client: OnboardingClient) -> list[EndpointApp]:
    control_app_id = cfg.get("CONTROL_APP_ID")
    data_app_id = cfg.get("DATA_APP_ID")

    endpoint_apps_response = onboarding_client.get_endpoint_apps().body
    endpoint_apps = (
        endpoint_apps_response.resources
        if endpoint_apps_response and endpoint_apps_response.resources
        else []
    )

    control_app = next(
        (
            app
            for app in endpoint_apps
            if app.application_type == EndpointAppType.DEVICE_CONTROL
            and app.application_name == control_app_id
        ),
        None,
    )
    if control_app is None:
        created = onboarding_client.create_endpoint_app(
            EndpointApp(
                application_name=control_app_id,
                application_type=EndpointAppType.DEVICE_CONTROL,
            )
        )
        if created.body:
            endpoint_apps.append(created.body)

    data_app = next(
        (
            app
            for app in endpoint_apps
            if app.application_type == EndpointAppType.TELEMETRY
            and app.application_name == data_app_id
        ),
        None,
    )
    if data_app is None and data_app_id:
        created = onboarding_client.create_endpoint_app(
            EndpointApp(
                application_name=data_app_id,
                application_type=EndpointAppType.TELEMETRY,
            )
        )
        if created.body:
            endpoint_apps.append(created.body)

    return endpoint_apps


def ensure_device(
    onboarding_client: OnboardingClient,
    endpoint_apps: list[EndpointApp],
    device_map: dict[str, Device],
    mac: str,
) -> tuple[Device | None, float | None, bool, str]:
    device = device_map.get(mac)
    if device is not None:
        return device, None, False, ""

    endpoint_app_values = [
        Application(value=app.application_id)
        for app in endpoint_apps
        if app.application_id
    ]

    device = Device(
        display_name=f"BLE Device {mac}",
        active=True,
        ble_extension=BleExtension(
            device_mac_address=mac,
            is_random=False,
            version_support=["4.1", "4.2", "5.0", "5.1", "5.2", "5.3"],
        ),
        endpoint_apps_extension=EndpointAppsExtension(applications=endpoint_app_values),
    )

    start_time = time.perf_counter()
    response = onboarding_client.create_device(device)
    end_time = time.perf_counter()

    duration_ms = round((end_time - start_time) * 1000.0, 2)
    created_device = response.body if response and response.body else None

    # 409 usually means the device already exists; refresh map (paged) and proceed.
    if response.status_code == 409:
        refreshed = collect_device_map(onboarding_client)
        device_map.clear()
        device_map.update(refreshed)
        existing = device_map.get(mac)
        if existing is not None:
            return existing, duration_ms, False, ""
        return (
            None,
            duration_ms,
            False,
            "SCIM create_device returned 409 but device not found in paged GET /Devices",
        )

    if created_device is None:
        return None, duration_ms, False, f"SCIM create_device failed (status={response.status_code})"
    if created_device.ble_extension and created_device.ble_extension.device_mac_address:
        device_map[created_device.ble_extension.device_mac_address.upper()] = created_device
    return created_device, duration_ms, True, ""


def post_nipc_connect(
    control_client: ControlClient,
    device: Device,
    retries: int,
) -> tuple[NipcResponse[BleDiscoverResponse | None], float]:
    if device.device_id is None:
        raise ValueError("Device ID is required for connection")

    tiedie_request = TiedieConnectRequest(
        protocol_information=BleProtocolInformation(ble=BleConnectRequest()),
        retries=retries,
    )

    start_time = time.perf_counter()
    response = control_client.post_with_nipc_response(
        f"/devices/{device.device_id}/connections",
        tiedie_request,
        BleDiscoverResponse,
    )
    end_time = time.perf_counter()
    duration_ms = round((end_time - start_time) * 1000.0, 2)
    return response, duration_ms


def write_results_csv(output_path: str, rows: list[dict]):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "device_number",
                "mac_address",
                "device_id",
                "scim_created",
                "scim_post_duration_ms",
                "nipc_post_duration_ms",
                "request_start",
                "ack_received",
                "duration_ms",
                "status",
                "error",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("device_number"),
                    row.get("mac"),
                    row.get("device_id"),
                    row.get("scim_created"),
                    row.get("scim_post_duration_ms"),
                    row.get("nipc_post_duration_ms"),
                    row.get("start_ts"),
                    row.get("end_ts"),
                    row.get("duration_ms"),
                    row.get("status"),
                    row.get("error"),
                ]
            )


def run_connect_timing_for_macs(
    macs: list[str],
    config_path: str,
    output_path: str,
    retries: int,
):
    cfg = load_flat_config(config_path)

    onboarding_client = build_onboarding_client(cfg)
    endpoint_apps = ensure_endpoint_apps(cfg, onboarding_client)
    control_client = build_control_client(cfg, onboarding_client)

    results: list[dict] = []
    for itx in range(3):
        print(f"Iteration number {itx}")
        device_map = collect_device_map(onboarding_client)
        connected_devices: list[Device] = []
        for idx, mac in enumerate(macs, start=1):
            device, scim_post_ms, scim_created, scim_error = ensure_device(
                onboarding_client, endpoint_apps, device_map, mac
            )
            if device is None:
                status = "scim_create_failed" if scim_post_ms is not None else "missing_device"
                results.append(
                    {
                        "device_number": idx,
                        "mac": mac,
                        "device_id": "",
                        "scim_created": scim_created,
                        "scim_post_duration_ms": scim_post_ms or "",
                        "nipc_post_duration_ms": "",
                        "start_ts": "",
                        "end_ts": "",
                        "duration_ms": "",
                        "status": status,
                        "error": scim_error or "Device not found in SCIM list",
                    }
                )
                continue

            start_ts = datetime.now().isoformat(timespec="seconds")
            start_time = time.perf_counter()
            response, nipc_post_ms = post_nipc_connect(control_client, device, retries=retries)
            end_time = time.perf_counter()
            end_ts = datetime.now().isoformat(timespec="seconds")

            duration_ms = round((end_time - start_time) * 1000.0, 2)
            error_text = ""
            status = "ok"
            if response.is_error:
                status = "error"
                if response.error:
                    error_text = f"{response.error.title}: {response.error.detail}"
                else:
                    error_text = "Request failed"

            results.append(
                {
                    "device_number": idx,
                    "mac": mac,
                    "device_id": device.device_id or "",
                    "scim_created": scim_created,
                    "scim_post_duration_ms": scim_post_ms or "",
                    "nipc_post_duration_ms": nipc_post_ms,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "duration_ms": duration_ms,
                    "status": status,
                    "error": error_text,
                }
            )

            if status == "ok":
                connected_devices.append(device)

        for device in connected_devices:
            try:
                control_client.disconnect(device)
            except Exception:
                pass

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    write_results_csv(output_path, results)
    print(f"Saved results to {output_path}")


def run_device_count_experiments(
    mapping_path: str,
    config_path: str,
    retries: int,
):
    macs = parse_mapping_macs(mapping_path)
    if not macs:
        raise ValueError("No MAC addresses found in mapping.txt")

    for device_count in range(1, 20):
        if device_count > len(macs):
            print(
                f"Skipping {device_count}: only {len(macs)} MACs available in mapping.txt"
            )
            continue

        selected_macs = random.sample(macs, device_count)
        output_path = os.path.join(LOGS_DIR, f"{device_count}.csv")
        run_connect_timing_for_macs(selected_macs, config_path, output_path, retries)


def main():
    parser = argparse.ArgumentParser(description="Measure connect timing via gateway control API")
    parser.add_argument(
        "--mapping",
        default=DEFAULT_MAPPING_PATH,
        help="Path to mapping.txt containing MAC addresses",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to sample-python-app config.ini",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV filename (default: logs/connection_timings_<timestamp>.csv)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Number of connection retries to request (default: 3)",
    )
    args = parser.parse_args()

    run_device_count_experiments(args.mapping, args.config, args.retries)


if __name__ == "__main__":
    main()

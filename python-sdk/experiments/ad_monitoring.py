import time
import subprocess
import os
import csv
from datetime import datetime
import re
import shutil
import matplotlib.pyplot as plt

GATEWAY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "gateway"))
ADS_LOG_PATH = os.path.join(GATEWAY_DIR, "ads_log.txt")
EXPERIMENTS_DIR = os.path.abspath(os.path.dirname(__file__))
LOGS_DIR = os.path.join(EXPERIMENTS_DIR, "logs")

LINEAR_INTERVALS_MS = [100, 1200, 2300, 3400, 4500, 5600, 6700, 7800, 8900, 10000]
GEOMETRIC_INTERVALS_MS = [100, 167, 278, 464, 774, 1292, 2154, 3594, 5995, 10000]

results = []

_LOG_RE_NEW = re.compile(
    r"^(?P<gw_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}): INFO - Received adv: "
    r"mac=(?P<mac>\S+) rssi=(?P<rssi>-?\d+) ap_ip=(?P<ap_ip>\S*) adv_ts=(?P<adv_ts>\S*) from (?P<from_ip>\S+)\s*$"
)
_LOG_RE_OLD = re.compile(
    r"^(?P<gw_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}): INFO - Received adv: "
    r"(?P<mac>\S+) RSSI: (?P<rssi>-?\d+) from (?P<from_ip>\S+)\s*$"
)

def clear_log():
    if os.path.exists(ADS_LOG_PATH):
        try:
            os.remove(ADS_LOG_PATH)
        except OSError:
            pass # In case file is locked, though it shouldn't be

def count_ads():
    if not os.path.exists(ADS_LOG_PATH):
        return 0
    with open(ADS_LOG_PATH, "r") as f:
        return sum(1 for line in f if "Received adv" in line)


def parse_ads_log():
    """Parse ads_log.txt into a list of per-packet dicts.

    Supports both the newer key=value format and the older human-readable format.
    """
    if not os.path.exists(ADS_LOG_PATH):
        return []

    events = []
    with open(ADS_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or "Received adv" not in line:
                continue

            m = _LOG_RE_NEW.match(line)
            if m:
                d = m.groupdict()
                events.append(
                    {
                        "gateway_ts": d.get("gw_ts"),
                        "adv_ts": d.get("adv_ts") or "",
                        "ap_ip": d.get("ap_ip") or "",
                        "from_ip": d.get("from_ip") or "",
                        "mac_address": d.get("mac"),
                        "rssi": int(d.get("rssi")),
                    }
                )
                continue

            m = _LOG_RE_OLD.match(line)
            if m:
                d = m.groupdict()
                events.append(
                    {
                        "gateway_ts": d.get("gw_ts"),
                        "adv_ts": "",
                        "ap_ip": "",
                        "from_ip": d.get("from_ip") or "",
                        "mac_address": d.get("mac"),
                        "rssi": int(d.get("rssi")),
                    }
                )

    return events


def write_interval_grouped_csv(csv_path: str, interval_packets):
    """Write a CSV with sections per interval.

    interval_packets: list of tuples (interval_ms, packets)
      packets: list[dict] with keys gateway_ts, adv_ts, ap_ip, from_ip, mac_address, rssi
    """
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["experiment", os.path.basename(__file__)])
        writer.writerow(["generated_at", datetime.now().isoformat(timespec="seconds")])
        writer.writerow([])

        for idx, (interval_ms, packets) in enumerate(interval_packets, start=1):
            writer.writerow([f"Interval {idx} : {interval_ms} ms"])
            writer.writerow(["gateway_ts", "adv_ts", "ap_ip", "from_ip", "mac_address", "rssi"])
            for p in packets:
                writer.writerow(
                    [
                        p.get("gateway_ts", ""),
                        p.get("adv_ts", ""),
                        p.get("ap_ip", ""),
                        p.get("from_ip", ""),
                        p.get("mac_address", ""),
                        p.get("rssi", ""),
                    ]
                )
            writer.writerow([])


def run_interval_set(intervals_ms, experiment_ts: str, label: str):
    interval_packets = []

    for interval in intervals_ms:
        clear_log()

        print(f"\nTesting interval {interval}ms ({label})...")

        # Start gateway with stdout piped so we can wait for it to be ready
        process = subprocess.Popen(
            ["python", "app.py", "--device", "grpc"],
            cwd=GATEWAY_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Wait for the gateway to report that it's streaming advertisements
        ready = False
        start_wait = time.time()

        # Read the stdout to look for the signal that streams have started
        while time.time() - start_wait < 15:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if "Starting advertisement streams from all Pis" in line:
                ready = True
                break

        if not ready:
            print(" -> Gateway failed to start streaming in time. Skipping.")
            process.terminate()
            interval_packets.append((interval, []))
            continue

        print(f" -> Gateway ready. Monitoring for {interval}ms...")

        # Keep the Gateway running in the background for exactly 'interval' milliseconds
        time.sleep(interval / 1000.0)

        # Stop gateway after the monitoring duration has elapsed
        print(" -> Stopping gateway...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

        # Wait a brief moment for file flushing/system cleanup
        time.sleep(1.0)

        packets = parse_ads_log()
        interval_packets.append((interval, packets))
        print(f" -> Success! Received {len(packets)} advertisements in {interval}ms")

        print(" -> Sleeping for 3 seconds to let the system cool down before the next run...\n")
        time.sleep(3.0)

    csv_path = os.path.join(
        os.path.dirname(__file__),
        f"ad_monitoring_packets_{label}_{experiment_ts}.csv",
    )
    write_interval_grouped_csv(csv_path, interval_packets)
    print(f"\nCSV saved to: {csv_path}")

    return interval_packets

def run_experiment():
    global results
    results = []

    print("===============================================")
    print("Starting Advertisement Monitoring Experiment...")
    print("===============================================")

    experiment_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    linear_packets = run_interval_set(LINEAR_INTERVALS_MS, experiment_ts, "linear")
    geometric_packets = run_interval_set(GEOMETRIC_INTERVALS_MS, experiment_ts, "geometric")

    linear_counts = [(interval, len(packets)) for interval, packets in linear_packets]
    geometric_counts = [(interval, len(packets)) for interval, packets in geometric_packets]
        
    print("===============================================")
    print("Experiment Results:")
    print("===============================================")
    print("Linear intervals:")
    for interval, count in linear_counts:
        print(f"Interval: {interval:5d}ms | Ads Received: {count}")

    print("\nGeometric intervals:")
    for interval, count in geometric_counts:
        print(f"Interval: {interval:5d}ms | Ads Received: {count}")
        
    # Plot results (counts) for both interval sets
    if linear_packets:
        intervals = [r[0] for r in linear_packets]
        counts = [len(r[1]) for r in linear_packets]

        plt.figure(figsize=(10, 6))
        plt.plot(intervals, counts, marker="o", linestyle="-", color="b")
        plt.title("Advertisements Received vs Monitoring Interval (Linear)")
        plt.xlabel("Monitoring Interval (ms)")
        plt.ylabel("Number of Advertisements Received")
        plt.grid(True)

        plot_path = os.path.join(os.path.dirname(__file__), "ad_monitoring_results_linear.png")
        plt.savefig(plot_path)
        print(f"\nPlot saved to: {plot_path}")

    if geometric_packets:
        intervals = [r[0] for r in geometric_packets]
        counts = [len(r[1]) for r in geometric_packets]

        plt.figure(figsize=(10, 6))
        plt.plot(intervals, counts, marker="o", linestyle="-", color="b")
        plt.title("Advertisements Received vs Monitoring Interval (Geometric)")
        plt.xlabel("Monitoring Interval (ms)")
        plt.ylabel("Number of Advertisements Received")
        plt.grid(True)

        plot_path = os.path.join(os.path.dirname(__file__), "ad_monitoring_results_geometric.png")
        plt.savefig(plot_path)
        print(f"\nPlot saved to: {plot_path}")


def run_fixed_duration_experiment(duration_seconds: int = 35):
    os.makedirs(LOGS_DIR, exist_ok=True)

    experiment_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_prefix = f"gateway_{duration_seconds}s_{experiment_ts}"
    gateway_stdout_path = os.path.join(LOGS_DIR, f"{log_prefix}_stdout.log")
    ads_log_dest = os.path.join(LOGS_DIR, f"{log_prefix}_ads_log.txt")

    clear_log()
    print(f"\nRunning fixed-duration experiment for {duration_seconds}s...")

    process = subprocess.Popen(
        ["python", "app.py", "--device", "grpc"],
        cwd=GATEWAY_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    ready = False
    start_wait = time.time()
    with open(gateway_stdout_path, "w", encoding="utf-8") as stdout_file:
        while time.time() - start_wait < 15:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                stdout_file.write(line)
            if "Starting advertisement streams from all Pis" in line:
                ready = True
                break

        if not ready:
            stdout_file.write("Gateway failed to start streaming in time.\n")
            print(" -> Gateway failed to start streaming in time. Skipping.")
            process.terminate()
            return

        print(f" -> Gateway ready. Monitoring for {duration_seconds}s...")
        end_time = time.time() + duration_seconds
        while time.time() < end_time:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                stdout_file.write(line)

    print(" -> Stopping gateway...")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

    time.sleep(1.0)
    if os.path.exists(ADS_LOG_PATH):
        shutil.copy2(ADS_LOG_PATH, ads_log_dest)
        print(f" -> Saved ads_log.txt to: {ads_log_dest}")
    else:
        print(" -> ads_log.txt not found; nothing to copy.")

if __name__ == "__main__":
    run_fixed_duration_experiment(35)
    run_experiment()

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

GATEWAY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "gateway"))
ADS_LOG_PATH = os.path.join(GATEWAY_DIR, "ads_log.txt")
EXPERIMENTS_DIR = os.path.abspath(os.path.dirname(__file__))
LOGS_DIR = os.path.join(EXPERIMENTS_DIR, "logs")


def clear_log():
    os.makedirs(os.path.dirname(ADS_LOG_PATH), exist_ok=True)
    try:
        with open(ADS_LOG_PATH, "w", encoding="utf-8"):
            pass
    except OSError:
        pass  # In case file is locked, though it shouldn't be


def run_fixed_duration_experiment(duration_seconds: int = 35, output_name: str | None = None):
    os.makedirs(LOGS_DIR, exist_ok=True)

    experiment_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if output_name:
        base_name = os.path.splitext(output_name)[0]
        log_prefix = base_name
    else:
        log_prefix = f"gateway_{duration_seconds}s_{experiment_ts}"
    ads_log_dest = os.path.join(LOGS_DIR, f"{log_prefix}_ads_log.txt")

    clear_log()
    print(f"\nRunning fixed-duration experiment for {duration_seconds}s...")

    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "app.py", "--device", "grpc"],
        cwd=GATEWAY_DIR,
        env=env,
    )

    print(f" -> Gateway running. Monitoring for {duration_seconds}s...")
    time.sleep(duration_seconds)

    print(" -> Stopping gateway...")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

    time.sleep(1.0)
    if os.path.exists(ADS_LOG_PATH):
        with open(ADS_LOG_PATH, "r", encoding="utf-8", errors="replace") as src:
            with open(ads_log_dest, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        print(f" -> Copied ads_log.txt to: {ads_log_dest}")
    else:
        print(" -> ads_log.txt not found; nothing to copy.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run gateway logging for a fixed duration")
    parser.add_argument(
        "--duration",
        type=int,
        default=35,
        help="Duration to run the gateway in seconds (default: 35)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file name (without path). .txt suffix is added automatically.",
    )
    args = parser.parse_args()

    run_fixed_duration_experiment(args.duration, args.output)

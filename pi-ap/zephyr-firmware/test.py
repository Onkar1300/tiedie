import serial
import time
import csv
from datetime import datetime

# Configuration
PORT = '/dev/ttyACM0'
BAUDRATE = 115200
DURATION_SECONDS = 60
OUTPUT_FILE = 'zephyr_ble_log.csv'

def main():
    print(f"Connecting to Zephyr Gateway on {PORT}...")
    
    try:
        # timeout=1.0 ensures readline() doesn't block forever if no devices are around,
        # allowing our 60-second timer check to evaluate regularly.
        ser = serial.Serial(PORT, baudrate=BAUDRATE, timeout=1.0)
    # ADD THIS LINE: Explicitly tell Zephyr to wake up the radio!
        ser.dtr = True
    except Exception as e:
        print(f"Failed to open port {PORT}. Ensure it is plugged in and permissions are set: {e}")
        return

    print(f"Initializing log file: {OUTPUT_FILE}")
    
    # Open the file in write mode to start fresh (use 'a' to append instead)
    with open(OUTPUT_FILE, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        
        # Write the headers. This matches the printk("%s,%d,%d") from our Zephyr C code
        writer.writerow(["Timestamp", "MAC_Address", "RSSI_dBm", "Payload_Length_Bytes"])

        print(f"Starting BLE collection for exactly {DURATION_SECONDS} seconds...")
        start_time = time.time()
        packet_count = 0

        try:
            # Keep looping until exactly 60 seconds have passed
            while (time.time() - start_time) < DURATION_SECONDS:
                
                # Read raw bytes from the Zephyr dongle
                raw_data = ser.readline()
                
                if raw_data:
                    try:
                        # Decode and clean up the string
                        line = raw_data.decode('utf-8').strip()
                        
                        if line:
                            # Generate a high-precision timestamp
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                            
                            # Split the incoming string to verify it matches our expected format
                            parts = line.split(',')
                            
                            if len(parts) == 3:
                                mac, rssi, length = parts
                                
                                # Save directly to the CSV
                                writer.writerow([timestamp, mac, rssi, length])
                                packet_count += 1
                                
                                # Print to terminal so you can monitor the flow
                                print(f"[{timestamp}] {mac:25} | RSSI: {rssi:4} dBm | Len: {length}")
                            else:
                                print(f"Malformed data ignored: {line}")
                                
                    except UnicodeDecodeError:
                        # Sometimes plugging in the USB causes a split byte to be sent initially
                        print("Garbled serial byte received, ignoring...")
                        
        except KeyboardInterrupt:
            # Allows you to Ctrl+C to stop it earlier than 1 minute if needed
            print("\nCollection interrupted by user.")
            
        finally:
            # Cleanly close the serial port and report stats
            ser.close()
            elapsed_time = time.time() - start_time
            print(f"\nDone! Gateway closed.")
            print(f"Collected {packet_count} packets in {elapsed_time:.1f} seconds.")
            print(f"Data saved to -> {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
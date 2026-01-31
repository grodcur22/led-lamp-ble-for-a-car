# 🚗 Car-LED-Master: Raspberry Pi GPS-Aware Controller

A smart, automated LED controller for vehicles using Raspberry Pi, GPS U-blox, and Bluetooth LE. 

This project was born out of frustration with low-quality Chinese mobile apps. By using a Raspberry Pi integrated into the car, we can control "ELK-BLEDOM" / "LEDBLE" type strips automatically based on real-world environment data.

> **Credit:** The Bluetooth protocol logic is based on the reverse engineering work by [8none1/ledble-ledlamp](https://github.com/8none1/ledble-ledlamp).

---

## 🔥 Why this project?
Standard LED apps are often unreliable and require manual interaction. This system is **autonomous**. 

### The "Tunnel & Garage" Logic:
The system monitors a **GPS U-blox** module via `gpsd`. 
* **GPS Signal Lost (Tunnel/Parking):** The LEDs automatically dim to a specific minimum (`min_target`) to avoid blinding the driver in dark environments.
* **GPS Signal Recovered:** The system restores full brightness if it's daytime.
* **Night Mode:** Using the system clock and month, it calculates sunset/sunrise to adjust intensity automatically.

---

## 🛠️ System Architecture



The script runs three main threads concurrently:
1. **GPS Monitor:** Listens to `gpsd` via sockets to detect signal status.
2. **MQTT Client:** Fully integrates with **Home Assistant** for remote control.
3. **Flask Web Server:** Provides a sleek, mobile-responsive dashboard for manual overrides.

---

## ⚙️ Configuration Variables

| Variable | Description |
| :--- | :--- |
| `DEVICES_CONFIG` | Dictionary containing MAC addresses, custom names, and color orders (RGB/GRB). |
| `min_target` | The specific brightness floor for each strip when in "Dark Mode" (Tunnel/Night). |
| `MQTT_BROKER` | IP of your local MQTT broker (e.g., Home Assistant). |
| `GPSD_HOST` | Usually `127.0.0.1` if the GPS is connected to the Pi. |

---

## 🧠 Logic Breakdown

### 1. Smart Brightness (`auto_brightness_task`)
Every 10 seconds, the system evaluates:
- **Condition A:** Is it daytime? (Calculated based on the current month).
- **Condition B:** Do we have a GPS fix?
- **Result:** If it's day AND we have signal, **100% Brightness**. If we enter a tunnel (No signal) or it's night, it drops to **5% or 1%** automatically.

### 2. Custom Bluetooth Protocol
The Chinese controllers use a specific hex header. This script handles the translation:
- Brightness: `7e0401[LEVEL]00000000ef`
- Color: `7e000503[R][G][B]00ef`

### 3. Integrated Web UI
Access the control panel by navigating to `http://<your-pi-ip>:5000`. It features:
- Dark Mode design.
- Toggle between Auto/Manual modes.
- Individual color pickers and power switches.

---

## 🚀 Quick Start

1. **Install dependencies:**
   ```bash
   pip install asyncio bleak paho-mqtt flask

2. Setup GPSD: Ensure your U-blox module is running:
   ```bash

    sudo gpsd /dev/ttyACM0 -F /var/run/gpsd.sock

3. Run the controller:
   ```bash

    python3 car_led_controller.py

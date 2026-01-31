import asyncio
import threading
import logging
import datetime
import socket
import json
import paho.mqtt.client as mqtt
from bleak import BleakClient, BleakScanner
from flask import Flask, render_template_string

# --- CONFIGURACIÓN DE LOGS ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN MQTT ---
MQTT_BROKER = "192.168.0.14"
MQTT_PORT = 1883
MQTT_USER = "homeassistant"
MQTT_PASS = "PASSWORD"
MQTT_TOPIC_SUB = "car/leds/command"
MQTT_TOPIC_PUB = "car/leds/status"

# --- CONFIGURACIÓN DE DISPOSITIVOS ---
DEVICES_CONFIG = {
    "C0:00:00:00:0A:4E": {"name": "LED Frontal", "order": "RGB", "last_color": "ffffff", "is_on": True, "min_target": 5},
    "A4:C1:38:10:00:2C": {"name": "LED Trasero", "order": "RGB", "last_color": "ffffff", "is_on": True, "min_target": 1}
}

TARGET_MACS = list(DEVICES_CONFIG.keys())
UUID_WRITE = "0000ffe1-0000-1000-8000-00805f9b34fb"

# --- CONFIGURACIÓN GPSD ---
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947
gps_has_signal = False 

# --- ESTADO GLOBAL ---
app = Flask(__name__)
clients = {} 
loop = asyncio.new_event_loop()
auto_brightness_enabled = True
current_brightness_level = 100

# --- LÓGICA MQTT ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("MQTT: Conectado al Broker")
        client.subscribe(MQTT_TOPIC_SUB)
    else:
        logger.error(f"MQTT: Error de conexión {rc}")

def on_message(client, userdata, msg):
    global auto_brightness_enabled, current_brightness_level
    try:
        data = json.loads(msg.payload.decode())
        
        # 1. Control de Modo Auto
        if "auto" in data:
            auto_brightness_enabled = bool(data["auto"])
            logger.info(f"MQTT: Modo Auto -> {auto_brightness_enabled}")
        
        # 2. Control de Brillo (Home Assistant manda 0-255 o 0-100 según config)
        if "brightness" in data:
            # Si HA manda 0-255, lo convertimos a tu escala 0-100
            level = int(data["brightness"])
            if level > 100: level = int((level / 255.0) * 100)
            current_brightness_level = level
            logger.info(f"MQTT: Brillo -> {level}%")
            hex_l = format(level, '02x')
            for mac in TARGET_MACS:
                if DEVICES_CONFIG[mac]["is_on"]:
                    asyncio.run_coroutine_threadsafe(send_to_device(mac, f"7e0401{hex_l}00000000ef"), loop)

        # 3. Control de Estado (ON/OFF)
        if "state" in data:
            state_on = (data["state"] == "ON")
            payload = "7e0404f00001ff00ef" if state_on else "7e0404000000ff00ef"
            for mac in TARGET_MACS:
                DEVICES_CONFIG[mac]["is_on"] = state_on
                asyncio.run_coroutine_threadsafe(send_to_device(mac, payload), loop)
            logger.info(f"MQTT: Estado -> {data['state']}")

        # 4. Control de Color RGB (Soporte Rueda HA)
        if "color" in data:
            r = data["color"].get("r", 255)
            g = data["color"].get("g", 255)
            b = data["color"].get("b", 255)
            hex_rgb = "{:02x}{:02x}{:02x}".format(r, g, b)
            logger.info(f"MQTT: Color -> #{hex_rgb}")
            for mac in TARGET_MACS:
                DEVICES_CONFIG[mac]["last_color"] = hex_rgb
                payload = fix_color_sequence(mac, hex_rgb)
                asyncio.run_coroutine_threadsafe(send_to_device(mac, payload), loop)

    except Exception as e:
        logger.error(f"MQTT: Error procesando mensaje: {e}")

mqtt_client = mqtt.Client()
if MQTT_USER: mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def mqtt_thread():
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_forever()
    except Exception as e:
        logger.error(f"MQTT: No se pudo iniciar el cliente: {e}")

# --- LÓGICA GPS (SOCKET) ---
def gps_monitor_thread():
    global gps_has_signal
    logger.info(f"Monitor GPS iniciado en {GPSD_HOST}:{GPSD_PORT}")
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((GPSD_HOST, GPSD_PORT))
            s.sendall(b'?WATCH={"enable":true,"nmea":true};')
            fp = s.makefile('r')
            while True:
                line = fp.readline()
                if not line: break
                if "$GP" in line or "$GN" in line:
                    if "GGA" in line:
                        parts = line.split(',')
                        if len(parts) > 6:
                            new_signal_state = (parts[6] != '0' and parts[6] != '')
                            if new_signal_state != gps_has_signal:
                                gps_has_signal = new_signal_state
                                logger.info(f"GPS EVENT: {'SEÑAL RECUPERADA' if gps_has_signal else 'SEÑAL PERDIDA'}")
        except Exception:
            if gps_has_signal:
                gps_has_signal = False
                logger.error("GPS ERROR: No se puede conectar a GPSD. Asumiendo SIN SEÑAL.")
            threading.Event().wait(10)

# --- CONTROL BLUETOOTH ---
def fix_color_sequence(mac, hex_rgb):
    order = DEVICES_CONFIG[mac]["order"]
    if order == "RGB": return f"7e000503{hex_rgb}00ef"
    r, g, b = hex_rgb[0:2], hex_rgb[2:4], hex_rgb[4:6]
    m = {"R": r, "G": g, "B": b}
    return f"7e000503{m[order[0]]}{m[order[1]]}{m[order[2]]}00ef"

async def connect_device(mac):
    if mac in clients and clients[mac].is_connected: return clients[mac]
    try:
        device = await BleakScanner.find_device_by_address(mac, timeout=3.0)
        if device:
            new_client = BleakClient(device, timeout=10.0)
            await new_client.connect()
            clients[mac] = new_client
            logger.info(f"Bluetooth: {DEVICES_CONFIG[mac]['name']} conectado.")
            return new_client
    except: pass
    return None

async def send_to_device(mac, hex_val):
    try:
        cli = await connect_device(mac)
        if cli and cli.is_connected:
            await cli.write_gatt_char(UUID_WRITE, bytes.fromhex(hex_val))
    except Exception as e:
        logger.debug(f"Error enviando a {mac}: {e}")
        if mac in clients: del clients[mac]

# --- LÓGICA DE DECISIÓN (SENSORIAL CON LOGS Y REFUERZO) ---
async def auto_brightness_task():
    global current_brightness_level
    while True:
        now = datetime.datetime.now()
        
        if auto_brightness_enabled:
            mes = now.month
            sunrise = 7.2 if mes in [11, 12, 1, 2] else 6.5
            sunset = 18.5 if mes in [11, 12, 1, 2] else 20.0
            current_hour = now.hour + now.minute/60.0
            is_daytime = sunrise <= current_hour <= sunset
            
            # Determinamos el estado global para los logs originales
            reason = ""
            global_target = 5 
            
            if not gps_has_signal:
                global_target = 5 # Representativo para el log
                reason = "SIN SEÑAL GPS (Túnel/Estacionamiento)"
            elif not is_daytime:
                global_target = 5 # Representativo para el log
                reason = f"NOCHE (Hora: {now.strftime('%H:%M')})"
            else:
                global_target = 100
                reason = f"DÍA (Hora: {now.strftime('%H:%M')}, GPS: OK)"

            # LOGS ORIGINALES (Sin eliminar líneas)
            if global_target != current_brightness_level:
                current_brightness_level = global_target
                logger.info(f"[AUTO] Brillo: {global_target}% | Razón: {reason}")
            
            # REFUERZO INDIVIDUAL (Reglas 8 y 9)
            for mac in TARGET_MACS:
                # Si es día y hay señal GPS -> 100%
                if is_daytime and gps_has_signal:
                    final_level = 100
                else:
                    # Si es noche O no hay señal GPS -> Mínimos específicos
                    final_level = DEVICES_CONFIG[mac]["min_target"]
                
                hex_l = format(final_level, '02x')
                # Solo recordamos el BRILLO (Regla 1) y conexión persistente (Regla 11)
                asyncio.run_coroutine_threadsafe(send_to_device(mac, f"7e0401{hex_l}00000000ef"), loop)
        
        else:
            # MODO MANUAL: Refuerzo del brillo elegido por el usuario
            hex_l = format(current_brightness_level, '02x')
            for mac in TARGET_MACS:
                asyncio.run_coroutine_threadsafe(send_to_device(mac, f"7e0401{hex_l}00000000ef"), loop)

        # Publicar estado en MQTT para que HA sepa qué está pasando
        if mqtt_client.is_connected():
            status_payload = {
                "state": "ON" if any(DEVICES_CONFIG[m]["is_on"] for m in DEVICES_CONFIG) else "OFF",
                "brightness": int((current_brightness_level / 100.0) * 255),
                "auto": auto_brightness_enabled,
                "gps": gps_has_signal,
                "color": {"r": int(DEVICES_CONFIG[TARGET_MACS[0]]["last_color"][0:2], 16),
                          "g": int(DEVICES_CONFIG[TARGET_MACS[0]]["last_color"][2:4], 16),
                          "b": int(DEVICES_CONFIG[TARGET_MACS[0]]["last_color"][4:6], 16)}
            }
            mqtt_client.publish(MQTT_TOPIC_PUB, json.dumps(status_payload))
        
        await asyncio.sleep(10)

# --- INTERFAZ WEB ---
@app.route('/')
def home():
    device_controls = ""
    for mac, info in DEVICES_CONFIG.items():
        device_controls += f'''
        <div class="card">
            <h3>{info['name']}</h3>
            <div class="grid">
                <button class="btn" style="background:#ff3333" onclick="device_cmd('{mac}', 'rojo')">R</button>
                <button class="btn" style="background:#33ff33; color:black" onclick="device_cmd('{mac}', 'verde')">V</button>
                <button class="btn" style="background:#3333ff" onclick="device_cmd('{mac}', 'azul')">A</button>
            </div>
            <input type="color" value="#{info['last_color']}" onchange="device_color('{mac}', this.value)" style="width:100%; height:40px; margin-top:10px; border:none; background:none; cursor:pointer;">
            <div class="grid" style="margin-top:10px">
                <button class="btn" style="background:#444" onclick="device_power('{mac}', 'off')">OFF</button>
                <button class="btn" style="background:#888" onclick="device_power('{mac}', 'on')">ON</button>
            </div>
        </div>
        '''
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>LED Control Panel</title>
            <style>
                body { background: #0a0a0a; color: #eee; font-family: sans-serif; text-align: center; margin: 0; }
                .container { max-width: 450px; margin: auto; padding: 20px; }
                .card { background: #161616; padding: 15px; border-radius: 15px; margin-bottom: 15px; border: 1px solid #333; }
                .status-badge { display: inline-block; padding: 5px 15px; border-radius: 20px; font-size: 12px; margin-bottom: 10px; font-weight: bold; }
                .auto-on { background: #004422; color: #00ff88; }
                .auto-off { background: #441111; color: #ff4444; }
                .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
                .btn { padding: 12px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; }
                .toggle-btn { background: #222; border: 1px solid #444; color: white; width: 100%; padding: 12px; border-radius: 10px; margin-bottom: 15px; cursor: pointer; }
                .slider { -webkit-appearance: none; width: 100%; height: 15px; border-radius: 5px; background: #333; outline: none; margin: 15px 0; }
                .slider::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 25px; height: 25px; border-radius: 50%; background: #00d4ff; cursor: pointer; }
            </style>
        </head>
        <body>
            <div class="container">
                <h2 style="color:#00d4ff">LED Master Dual</h2>
                <div id="badge" class="status-badge auto-on">MODO AUTO ACTIVO</div>
                <button class="toggle-btn" onclick="toggleAuto()">Alternar Modo Automático</button>
                
                <div class="card" id="manual_brightness_card" style="display:none">
                    <p>Brillo Manual: <span id="manual_val">100</span>%</p>
                    <input type="range" min="1" max="100" value="100" class="slider" id="brightRange" oninput="updateManualBright(this.value)">
                </div>

                ''' + device_controls + '''

                <div class="card">
                    <p>Brillo en Sistema: <span id="val_text">--</span>%</p>
                    <button class="btn" style="background:#ff4444; width:100%" onclick="fetch('/all_off')">APAGAR TODO</button>
                </div>
            </div>
            <script>
                let isAuto = true;
                function toggleAuto() {
                    isAuto = !isAuto;
                    const b = document.getElementById('badge');
                    const mCard = document.getElementById('manual_brightness_card');
                    b.innerText = isAuto ? 'MODO AUTO ACTIVO' : 'MODO MANUAL';
                    b.className = 'status-badge ' + (isAuto ? 'auto-on' : 'auto-off');
                    mCard.style.display = isAuto ? 'none' : 'block';
                    fetch('/mode/auto/' + (isAuto ? '1' : '0'));
                }
                function updateManualBright(val) {
                    document.getElementById('manual_val').innerText = val;
                    fetch('/set_brightness/' + val);
                }
                function device_cmd(mac, action) { fetch(`/device_control/${mac}/${action}`); }
                function device_color(mac, hex) { fetch(`/device_color/${mac}/${hex.replace('#', '')}`); }
                function device_power(mac, state) { fetch(`/device_power/${mac}/${state}`); }
                
                setInterval(() => {
                    fetch('/get_current_brightness').then(r => r.text()).then(v => {
                        document.getElementById('val_text').innerText = v;
                    });
                }, 3000);
            </script>
        </body>
        </html>
    ''')

# --- RUTAS DE CONTROL ---
@app.route('/mode/auto/<int:s>')
def sa(s):
    global auto_brightness_enabled
    auto_brightness_enabled = (s==1)
    return "OK"

@app.route('/set_brightness/<int:level>')
def set_br(level):
    global current_brightness_level
    current_brightness_level = level
    hex_l = format(level, '02x')
    for mac in TARGET_MACS:
        if DEVICES_CONFIG[mac]["is_on"]:
            asyncio.run_coroutine_threadsafe(send_to_device(mac, f"7e0401{hex_l}00000000ef"), loop)
    return "OK"

@app.route('/device_control/<mac>/<action>')
def dev_ctrl(mac, action):
    if mac in DEVICES_CONFIG:
        colors = {"rojo":"ff0000", "verde":"00ff00", "azul":"0000ff", "blanco":"ffffff"}
        if action in colors:
            DEVICES_CONFIG[mac]["last_color"] = colors[action]
            payload = fix_color_sequence(mac, colors[action])
            asyncio.run_coroutine_threadsafe(send_to_device(mac, payload), loop)
    return "OK"

@app.route('/device_color/<mac>/<hex_color>')
def dev_color(mac, hex_color):
    if mac in DEVICES_CONFIG:
        DEVICES_CONFIG[mac]["last_color"] = hex_color
        payload = fix_color_sequence(mac, hex_color)
        asyncio.run_coroutine_threadsafe(send_to_device(mac, payload), loop)
    return "OK"

@app.route('/device_power/<mac>/<state>')
def dev_power(mac, state):
    if mac in DEVICES_CONFIG:
        DEVICES_CONFIG[mac]["is_on"] = (state == "on")
        payload = ( "7e0404f00001ff00ef" if state == "on" else "7e0404000000ff00ef" )
        asyncio.run_coroutine_threadsafe(send_to_device(mac, payload), loop)
    return "OK"

@app.route('/all_off')
def all_off():
    for mac in TARGET_MACS:
        DEVICES_CONFIG[mac]["is_on"] = False
        asyncio.run_coroutine_threadsafe(send_to_device(mac, "7e0404000000ff00ef"), loop)
    return "OK"

@app.route('/get_current_brightness')
def gcb(): return str(current_brightness_level)

# --- INICIO ---
def start_loop():
    asyncio.set_event_loop(loop)
    loop.create_task(auto_brightness_task())
    loop.run_forever()

if __name__ == '__main__':
    threading.Thread(target=gps_monitor_thread, daemon=True).start()
    threading.Thread(target=mqtt_thread, daemon=True).start()
    threading.Thread(target=start_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)

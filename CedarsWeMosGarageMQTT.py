# CedarsWeMosGarageMQTT - MicroPython port
# Target: WeMos D1 R1 (ESP8266)
# Board: LOLIN(WEMOS) D1 R1
#
# Pin mapping (D1R1 MicroPython GPIO numbers):
#   D3 = GPIO5  -> pinGateSenseA (INPUT_PULLUP)
#   D4 = GPIO4  -> pinGateSenseB (INPUT_PULLUP)
#   D5 = GPIO14 -> pinGateAControl (relay output)
#   D6 = GPIO12 -> pinGateBControl (relay output)
#   D7 = GPIO13 -> pinFridgeControl (relay output)
#   A0          -> NTC thermistor
#
# OTA: Use WebREPL for OTA updates (webrepl_cfg.py must be configured)
# Install deps: upip.install('umqtt.robust') or use Thonny

import network
import time
import math
import machine
from machine import Pin, ADC
from umqtt.robust import MQTTClient
import ubinascii

# ---------------------------------------------------------------------------
# BUILT-IN LED  (GPIO2 on WeMos D1 R1, active-low)
# ---------------------------------------------------------------------------
led = Pin(2, Pin.OUT, value=1)   # off at start

LED_FAST   = 100   # ms half-period  — WiFi connecting
LED_MEDIUM = 500   # ms half-period  — MQTT connecting
LED_SLOW   = 2000  # ms half-period  — all good

_led_interval  = LED_FAST
_led_last_tick = 0

def led_set_rate(ms):
    global _led_interval
    _led_interval = ms

def led_tick():
    """Call frequently from main loop — non-blocking blink."""
    global _led_last_tick
    now = time.ticks_ms()
    if time.ticks_diff(now, _led_last_tick) >= _led_interval:
        led.value(not led.value())
        _led_last_tick = now

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
WIFI_APS = [
    ("19CedarsExt",      "7107165106085"),
    ("19-CEDARS-LTE",    "7107165106085"),
    ("19CedarsStandby",  "7107165106085"),
]
MQTT_SERVER   = "10.1.12.117"
MQTT_PORT     = 1883
HOSTNAME      = "CedarsGarageWeMos"
FW_VERSION    = "fw2.0-upy"

TOPIC_BASE    = b"cedars/garage"
TOPIC_STATUS  = b"cedars/garage"
TOPIC_GATEA   = b"cedars/garage/gatea"
TOPIC_GATEB   = b"cedars/garage/gateb"
TOPIC_FTEMP   = b"cedars/garage/freezertemp"
TOPIC_FSTATE  = b"cedars/garage/freezerstate"
TOPIC_CMD     = b"cedars/garage/#"        # wildcard — handled via prefix check
TOPIC_TIME    = b"sebenza/gate/time"

RELAY_ON  = 0   # active-low relay
RELAY_OFF = 1
RELAY_PULSE_MS = 500

ARM_START = 20  # 20:00
ARM_END   =  9  #  09:00
TZ_OFFSET =  2  # SAST = UTC+2

HEARTBEAT_INTERVAL = 60  # seconds

# ---------------------------------------------------------------------------
# PINS
# ---------------------------------------------------------------------------
pin_gate_a_ctrl  = Pin(14, Pin.OUT, value=RELAY_OFF)
pin_gate_b_ctrl  = Pin(12, Pin.OUT, value=RELAY_OFF)
pin_fridge_ctrl  = Pin(13, Pin.OUT, value=RELAY_OFF)
pin_gate_sense_a = Pin(5,  Pin.IN,  Pin.PULL_UP)
pin_gate_sense_b = Pin(4,  Pin.IN,  Pin.PULL_UP)
adc = ADC(0)

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
gate_a_closed       = False
gate_b_closed       = False
fridge_is_on        = False
gates_status_changed = False
alarm_armed         = False
alarm_arm_override  = False
alarm_disarm_override = False
epoch_time          = 0       # last received Unix epoch (seconds)
mqtt_client         = None

# ---------------------------------------------------------------------------
# WIFI
# ---------------------------------------------------------------------------
def connect_wifi():
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    sta.config(dhcp_hostname=HOSTNAME)
    led_set_rate(LED_FAST)
    while not sta.isconnected():
        for ssid, pw in WIFI_APS:
            print(f"\nTrying {ssid} ...")
            sta.active(False)
            time.sleep_ms(500)
            sta.active(True)
            sta.connect(ssid, pw)
            # wait up to 15s for this AP
            for _ in range(30):
                if sta.isconnected():
                    break
                led_tick()
                time.sleep_ms(250)
                led_tick()
                time.sleep_ms(250)
                print(".", end="")
            if sta.isconnected():
                break
            print(f" no joy with {ssid}")
    print("WiFi connected:", sta.ifconfig())
    return sta

# ---------------------------------------------------------------------------
# THERMISTOR  (Steinhart-Hart, 10k NTC)
# ---------------------------------------------------------------------------
def get_temp_c():
    R1  = 10000.0
    c1  = 1.009249522e-03
    c2  = 2.378405444e-04
    c3  = 2.019202697e-07
    Vo  = adc.read()          # 0-1023
    if Vo == 0:
        return -99.0          # open circuit guard
    R2     = R1 * (1023.0 / float(Vo) - 1.0)
    logR2  = math.log(R2)
    T_K    = 1.0 / (c1 + c2 * logR2 + c3 * logR2 ** 3)
    Tc     = T_K - 273.15
    print(f"Temp: {Tc:.2f} C")
    return Tc

# ---------------------------------------------------------------------------
# RELAY PULSE
# ---------------------------------------------------------------------------
def pulse_relay(pin):
    pin.value(RELAY_ON)
    time.sleep_ms(RELAY_PULSE_MS)
    pin.value(RELAY_OFF)

# ---------------------------------------------------------------------------
# GATE STATE
# ---------------------------------------------------------------------------
def read_gate_states():
    global gate_a_closed, gate_b_closed
    gate_a_closed = (pin_gate_sense_a.value() == 0)
    gate_b_closed = (pin_gate_sense_b.value() == 0)

def publish_gate_states():
    read_gate_states()
    mqtt_client.publish(TOPIC_GATEA, b"closed" if gate_a_closed else b"open")
    mqtt_client.publish(TOPIC_GATEB, b"closed" if gate_b_closed else b"open")
    print(f"Gate A: {'closed' if gate_a_closed else 'open'}  "
          f"Gate B: {'closed' if gate_b_closed else 'open'}")

# ---------------------------------------------------------------------------
# INTERRUPTS  (ISR — keep tiny, set flag only)
# ---------------------------------------------------------------------------
def isr_gate_a(pin):
    global gates_status_changed
    gates_status_changed = True

def isr_gate_b(pin):
    global gates_status_changed
    gates_status_changed = True

pin_gate_sense_a.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=isr_gate_a)
pin_gate_sense_b.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=isr_gate_b)

# ---------------------------------------------------------------------------
# TIME HELPERS
# ---------------------------------------------------------------------------
def hour_from_epoch(epoch_sec):
    """Return UTC hour from Unix epoch seconds."""
    return (epoch_sec % 86400) // 3600

# ---------------------------------------------------------------------------
# MQTT CALLBACK
# ---------------------------------------------------------------------------
def mqtt_callback(topic, msg):
    global fridge_is_on, epoch_time, gates_status_changed

    topic_str = topic.decode()
    pl        = msg.decode().strip()
    print(f"MQTT [{topic_str}] '{pl}'")

    # ---- time sync --------------------------------------------------------
    if topic == TOPIC_TIME:
        try:
            # Node-RED sends epoch in MILLISECONDS
            epoch_time = int(pl[:10])
            print(f"Epoch set: {epoch_time}")
        except Exception as e:
            print("Time parse error:", e)
        return

    # ---- gate / fridge commands -------------------------------------------
    if pl.startswith("opena"):
        if not gate_a_closed:
            print("Gate A already open, ignoring")
        else:
            print("Open A")
            pulse_relay(pin_gate_a_ctrl)

    elif pl.startswith("closea"):
        if gate_a_closed:
            print("Gate A already closed, ignoring")
        else:
            print("Close A")
            pulse_relay(pin_gate_a_ctrl)

    elif pl.startswith("openb"):
        if not gate_b_closed:
            print("Gate B already open, ignoring")
        else:
            print("Open B")
            pulse_relay(pin_gate_b_ctrl)

    elif pl.startswith("closeb"):
        if gate_b_closed:
            print("Gate B already closed, ignoring")
        else:
            print("Close B")
            pulse_relay(pin_gate_b_ctrl)

    elif pl.startswith("fridgeon"):
        print("Fridge ON")
        pin_fridge_ctrl.value(RELAY_ON)
        fridge_is_on = True

    elif pl.startswith("fridgeoff"):
        print("Fridge OFF")
        pin_fridge_ctrl.value(RELAY_OFF)
        fridge_is_on = False

    elif pl.startswith("stop"):
        print("Stop command received (no-op for relay-pulsed gates)")

    elif pl.startswith("closegate"):
        print("Close main roller gate (no-op placeholder)")

    elif topic_str == "cedars/garage/gatestate":
        print(f"Gate A: {'closed' if gate_a_closed else 'open'}, "
              f"Gate B: {'closed' if gate_b_closed else 'open'}")

# ---------------------------------------------------------------------------
# MQTT CONNECT / RECONNECT
# ---------------------------------------------------------------------------
def mqtt_connect():
    global mqtt_client
    led_set_rate(LED_MEDIUM)
    uid = ubinascii.hexlify(machine.unique_id()).decode()
    client_id = f"ESP8266Client-{uid}"
    client = MQTTClient(
        client_id,
        MQTT_SERVER,
        port=MQTT_PORT,
        keepalive=60
    )
    client.set_callback(mqtt_callback)
    client.connect()
    client.subscribe(b"cedars/garage/cmd")      # explicit command topic
    client.subscribe(b"cedars/garage/gatestate")
    client.subscribe(TOPIC_TIME)
    client.publish(TOPIC_STATUS, f"online {FW_VERSION}".encode())
    print("MQTT connected as", client_id)
    led_set_rate(LED_SLOW)
    mqtt_client = client

def ensure_mqtt():
    global mqtt_client
    try:
        mqtt_client.ping()
    except Exception:
        print("MQTT disconnected — reconnecting...")
        try:
            mqtt_connect()
        except Exception as e:
            print("MQTT reconnect failed:", e)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    global gates_status_changed, alarm_armed, epoch_time

    connect_wifi()
    mqtt_connect()

    # initial state publish
    publish_gate_states()
    t = get_temp_c()
    mqtt_client.publish(TOPIC_FTEMP, f"{t:.2f}".encode())

    last_heartbeat = time.time()

    while True:
        # --- MQTT tick ---
        try:
            mqtt_client.check_msg()
        except Exception as e:
            print("MQTT check_msg error:", e)
            led_set_rate(LED_MEDIUM)
            ensure_mqtt()

        # --- gate ISR flag ---
        if gates_status_changed:
            gates_status_changed = False
            publish_gate_states()

        # --- 60-second heartbeat ---
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = now

            mqtt_client.publish(TOPIC_STATUS, f"online {FW_VERSION}".encode())

            t = get_temp_c()
            mqtt_client.publish(TOPIC_FTEMP, f"{t:.2f}".encode())
            mqtt_client.publish(TOPIC_FSTATE, b"on" if fridge_is_on else b"off")

            publish_gate_states()

            # alarm arm/disarm logic
            if epoch_time > 0:
                utc_hour  = hour_from_epoch(epoch_time + now - last_heartbeat)
                local_hour = (utc_hour + TZ_OFFSET) % 24
                print(f"Local hour: {local_hour}")

                if local_hour >= ARM_START or local_hour < ARM_END:
                    alarm_armed = True
                else:
                    alarm_armed = False

                print(f"Alarm armed: {alarm_armed}")

        led_tick()
        time.sleep_ms(100)

main()

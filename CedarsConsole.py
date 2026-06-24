#version 3
import gc, network, time
from machine import Pin, SoftI2C
gc.collect()

from umqtt.simple import MQTTClient
gc.collect()

from i2c_lcd import LCD
gc.collect()

from kpad import Keypad
gc.collect()

SSID      = "19CedarsStandby"
PASSWORD  = "7107165106085"
MQTT_HOST = "10.1.12.117"
TOPIC_SUB = b"cedars/#"
TOPIC_PUB = b"cedars/console/"
CLIENT_ID = "cedarsconsole"

gtemp=0.0; mtemp=0.0; rtemp=0.0; urtemp=0.0
gelement=False; dispmode=0; screenupdtcnt=0
mqtt=None

i2c  = SoftI2C(scl=Pin(5), sda=Pin(4), freq=100000)
lcd  = LCD(i2c, 0x27, 4, 20)
kpad = Keypad(i2c, 0x23)
led  = Pin(2, Pin.OUT, value=1)
gc.collect()

def update_display():
    global screenupdtcnt
    screenupdtcnt = (screenupdtcnt + 1) % 99
    lcd.clear()
    if dispmode == 0:
        lcd.putstr("Geyser Temp:{:>7.2f}".format(gtemp))
        lcd.move_to(0,1); lcd.putstr("Manif Temp :{:>7.2f}".format(mtemp))
        lcd.move_to(0,2); lcd.putstr("Roof Temp  :{:>7.2f}".format(rtemp))
        lcd.move_to(0,3); lcd.putstr("Element    :" + ("ON " if gelement else "OFF"))
        lcd.move_to(18,3); lcd.putstr("{:02d}".format(screenupdtcnt))
    else:
        lcd.putstr("Upstairs   :{:>7.2f}".format(urtemp))
        lcd.move_to(0,1); lcd.putstr("Aircon Tmp :{:>7.2f}".format(mtemp))
        lcd.move_to(0,2); lcd.putstr("Aircon Set :{:>7.2f}".format(rtemp))
        lcd.move_to(0,3); lcd.putstr("Aircon     :" + ("ON " if gelement else "OFF"))
        lcd.move_to(18,3); lcd.putstr("{:02d}".format(screenupdtcnt))

def connect_wifi():
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if w.isconnected(): return
    lcd.clear(); lcd.putstr("WiFi:{}".format(SSID))
    w.connect(SSID, PASSWORD)
    t = 0
    while not w.isconnected():
        led.value(0); time.sleep_ms(100)
        led.value(1); time.sleep_ms(900)
        t += 1
        if t > 30:
            lcd.clear(); lcd.putstr("WiFi FAILED"); return
    lcd.clear(); lcd.putstr(w.ifconfig()[0])
    time.sleep(2)

def mqtt_cb(topic, msg):
    global gtemp, mtemp, rtemp, urtemp, gelement
    t = topic.decode()
    p = msg.decode().replace('"','').strip()
    print(f"t:'{t}' p:'{p}'")
    upd = True
    try:
        if   t == "cedars/geyser/top_temp":            gtemp    = round(float(p),2)
        elif t == "cedars/geyser/manifold":             mtemp    = round(float(p),2)
        elif t == "cedars/geyser/roof_temp":            rtemp    = round(float(p),2)
        elif t == "cedars/geyser/element":              gelement = (p != "0")
        elif t == "cedars/upstairs/roomtemperature":    urtemp   = round(float(p),2)
        else: upd = False
    except: upd = False
    if upd: update_display()

def connect_mqtt():
    global mqtt
    gc.collect()
    try:
        c = MQTTClient(CLIENT_ID, MQTT_HOST, 1883, keepalive=60)
        c.set_callback(mqtt_cb)
        c.connect()
        c.subscribe(b"cedars/geyser/top_temp")
        c.subscribe(b"cedars/geyser/manifold")
        c.subscribe(b"cedars/geyser/roof_temp")
        c.subscribe(b"cedars/geyser/element")
        c.subscribe(b"cedars/upstairs/roomtemperature")
        c.publish(TOPIC_PUB, b"console online fw1.0")
        mqtt = c
        lcd.clear(); lcd.putstr("MQTT OK")
        time.sleep(1)
    except Exception as e:
        mqtt = None
        lcd.clear(); lcd.putstr("MQTT FAIL")
        print(e)

connect_wifi()
gc.collect()
connect_mqtt()
gc.collect()
update_display()

last_ka = time.ticks_ms()

while True:
    key = kpad.get_key()
    if key:
        print(f"Key:{key}:{dispmode}")
        if dispmode == 0 and mqtt:
            if key == 'A': mqtt.publish(b"cedars/console/elementcntl", b"1"); lcd.clear(); lcd.putstr("Element ON"); time.sleep(2)
            if key == 'B': mqtt.publish(b"cedars/console/elementcntl", b"0"); lcd.clear(); lcd.putstr("Element OFF");time.sleep(2)
        if key == '1': dispmode = 0; update_display()
        if key == '2': dispmode = 1; update_display()

    if mqtt:
        try:
            mqtt.check_msg()
        except OSError as e:
            if e.args[0] != 11:  # ignore EAGAIN
                print("MQTT lost:", e)
                mqtt = None
        except Exception as e:
            print("MQTT err:", e)
            mqtt = None

    if time.ticks_diff(time.ticks_ms(), last_ka) >= 30000:
        last_ka = time.ticks_ms()
        if mqtt is None:
            connect_wifi(); gc.collect(); connect_mqtt()
        else:
            try: mqtt.ping()
            except: mqtt = None
        update_display()

    time.sleep_ms(50)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import time
import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import requests  # ensure installed
from iot_integrations import ThingSpeakClient, Telegram, TelegramBotThread, AlertGate

import Adafruit_DHT
import RPi.GPIO as GPIO
from RPLCD.i2c import CharLCD

# ---------------- CONFIG (BOARD mode) ----------------
GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)

# I2C LCD (16x2)
LCD_I2C_ADDR = 0x3f   # from i2cdetect -y 1
LCD_COLS = 16
LCD_ROWS = 2

# Slide switch (master ON/OFF for periodic logic)
SLIDE_SWITCH_PIN = 15   # BOARD 15

# PIR (active LOW: 0 = presence, 1 = no presence)
PIR_PIN = 11            # BOARD 11
PIR_ACTIVE_LEVEL = GPIO.LOW

# DC Motor
DC_MOTOR_PIN = 16       # BOARD 16

# DHT11
DHT_SENSOR = Adafruit_DHT.DHT11
DHT_PIN_BOARD = 40      # BOARD pin (physical) - for your reference
DHT_PIN_BCM   = 21      # BCM pin actually used by Adafruit_DHT

# Ultrasonic (HC-SR04 style)
ULTRA_TRIG = 22         # BOARD 22 (OUTPUT)
ULTRA_ECHO = 13         # BOARD 13 (INPUT - level shift to 3.3 V!)

# Moisture sensor (digital)
MOISTURE_PIN = 7        # BOARD 7
MOISTURE_IS_DRY_LEVEL = GPIO.HIGH   # flip to GPIO.LOW if inverted

# Keypad 4x3 (manual scanner)
KEYPAD_LAYOUT = [
    ["1", "2", "3"],
    ["4", "5", "6"],
    ["7", "8", "9"],
    ["*", "0", "#"]
]

# These BOARD pins are the conversion of your working BCM pins:
#   ROWS (BCM -> BOARD): [6->31, 20->38, 19->35, 13->33]
#   COLS (BCM -> BOARD): [12->32, 5->29, 16->36]
KEYPAD_ROWS = [31, 38, 35, 33]   # inputs with pull-ups
KEYPAD_COLS = [32, 29, 36]       # outputs, driven LOW one by one

# Water / beaker
BEAKER_HEIGHT_CM = 10.0
WATER_LOW_DISTANCE_CM = 7.0  # >7cm == low

# Timings
MAIN_LOOP_SLEEP = 0.25
SENSOR_POLL_INTERVAL = 1.0
KEYPAD_SCAN_INTERVAL = 0.02   # 20ms
KEY_DEBOUNCE_RELEASE_SEC = 0.1

# Sanity limits for DHT11
TEMP_MIN_C = -20
TEMP_MAX_C = 80

# --------------- GLOBALS ----------------
stop_event = threading.Event()
last_distance_cm = None
keypad_last_state = [[False]*len(KEYPAD_COLS) for _ in range(len(KEYPAD_ROWS))]

# Shared readings for ThingSpeak/Telegram
readings_lock = threading.Lock()
readings = {
    "temp_c": None,
    "humidity": None,       # optional
    "distance_cm": None,
    "water_height": None,
    "soil_dry": None,
    "pir_active": None,
    "motor_on": 0,
}

# LCD
lcd = CharLCD('PCF8574', LCD_I2C_ADDR, cols=LCD_COLS, rows=LCD_ROWS,
              charmap='A00', auto_linebreaks=True)
lcd_lock = threading.Lock()

# Integrations (from env)
TS_KEY = os.getenv("THINGSPEAK_WRITE_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

ts_client = ThingSpeakClient(write_key=TS_KEY) if TS_KEY else None
tg_client = Telegram(TG_TOKEN, TG_CHAT) if (TG_TOKEN and TG_CHAT) else None
alerts = AlertGate(tg_client) if tg_client else None
tg_bot = None

# --------------- LCD HELPERS ----------------
def lcd_print(line1: str, line2: str = ""):
    with lcd_lock:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string(line1[:LCD_COLS])
        if LCD_ROWS > 1:
            lcd.cursor_pos = (1, 0)
            lcd.write_string(line2[:LCD_COLS])

# --------------- HELPERS ----------------
def slide_switch_on():
    return GPIO.input(SLIDE_SWITCH_PIN) == GPIO.HIGH

def pir_active():
    # 0 == presence (active LOW)
    val = (GPIO.input(PIR_PIN) == PIR_ACTIVE_LEVEL)
    with readings_lock:
        readings["pir_active"] = 1 if val else 0
    return val

def motor_on():
    GPIO.output(DC_MOTOR_PIN, GPIO.HIGH)
    with readings_lock:
        readings["motor_on"] = 1

def motor_off():
    GPIO.output(DC_MOTOR_PIN, GPIO.LOW)
    with readings_lock:
        readings["motor_on"] = 0

def moisture_is_dry():
    val = GPIO.input(MOISTURE_PIN) == MOISTURE_IS_DRY_LEVEL
    with readings_lock:
        readings["soil_dry"] = 1 if val else 0
    return val

def read_ultrasonic_distance_cm():
    GPIO.output(ULTRA_TRIG, True)
    time.sleep(0.00001)
    GPIO.output(ULTRA_TRIG, False)

    start = time.time()
    timeout = start + 0.02
    while GPIO.input(ULTRA_ECHO) == 0 and time.time() < timeout:
        start = time.time()

    stop = time.time()
    timeout = stop + 0.02
    while GPIO.input(ULTRA_ECHO) == 1 and time.time() < timeout:
        stop = time.time()

    elapsed = stop - start
    distance = (elapsed * 34300) / 2.0
    return distance

def calc_water_height(distance_cm):
    h = BEAKER_HEIGHT_CM - distance_cm
    return max(0.0, min(BEAKER_HEIGHT_CM, h))

def read_dht11_both():
    # IMPORTANT: Adafruit_DHT expects BCM numbering
    humidity, temperature = Adafruit_DHT.read_retry(DHT_SENSOR, DHT_PIN_BCM)
    if temperature is None or not (TEMP_MIN_C <= temperature <= TEMP_MAX_C):
        return None, None
    return humidity, float(temperature)

def check_and_drive_motor():
    if moisture_is_dry():
        motor_on()
        print("[MOTOR] ON (soil dry)")
    else:
        motor_off()
        print("[MOTOR] OFF (soil moist)")

def everything_off():
    motor_off()

def handle_keypress(key):
    # 1: water level (LCD + console)
    if key == "1":
        try:
            d = read_ultrasonic_distance_cm()
            if d <= 0:
                print("[KEYPAD-1] Invalid ultrasonic reading.")
                lcd_print("Water: invalid")
                return
            height = calc_water_height(d)
            with readings_lock:
                readings["distance_cm"] = float(d)
                readings["water_height"] = float(height)
            print(f"[KEYPAD-1] Water height = {height:.2f} cm (distance = {d:.2f} cm)")
            lcd_print(f"Water:{height:6.2f}cm", f"Dist: {d:6.2f}cm")
        except Exception as e:
            print(f"[KEYPAD-1] Ultrasonic read error: {e}")
            lcd_print("Water read err")
        return

    # 2: temperature only (LCD + console)
    if key == "2":
        try:
            h, t = read_dht11_both()
            if t is None:
                print("[KEYPAD-2] Failed / invalid DHT11 reading.")
                lcd_print("Temp read fail")
                return
            with readings_lock:
                readings["temp_c"] = t
                if h is not None:
                    readings["humidity"] = float(h)
            print(f"[KEYPAD-2] Temperature = {t:.2f} °C")
            lcd_print(f"Temp: {t:.2f} C")
        except Exception as e:
            print(f"[KEYPAD-2] DHT11 read error: {e}")
            lcd_print("Temp read err")
        return

    # Other keys: console only
    print(f"[KEYPAD] Key pressed: {key}")

# --------------- TELEGRAM HELPERS -----------------
def get_status_text():
    with readings_lock:
        r = dict(readings)
    lines = [
        "🌡️ Status:",
        f"Temp: {r['temp_c']} °C" if r['temp_c'] is not None else "Temp: n/a",
        f"Humidity: {r['humidity']} %" if r.get('humidity') is not None else "Humidity: n/a",
        f"Water height: {r['water_height']} cm" if r.get('water_height') is not None else "Water height: n/a",
        f"Distance: {r['distance_cm']} cm" if r['distance_cm'] is not None else "Distance: n/a",
        f"Soil: {'DRY' if r['soil_dry'] else 'MOIST' if r['soil_dry'] is not None else 'n/a'}",
        f"PIR: {'ACTIVE' if r['pir_active'] else 'IDLE' if r['pir_active'] is not None else 'n/a'}",
        f"Motor: {'ON' if r['motor_on'] else 'OFF'}",
    ]
    return "\n".join(lines)

def start_telegram_bot():
    global tg_bot
    if tg_client:
        tg_client.send("🔔 System booted.")
        tg_bot = TelegramBotThread(
            token=TG_TOKEN,
            chat_id=TG_CHAT,
            get_status_cb=get_status_text
        )
        tg_bot.start()

# --------------- THREADS -----------------
def keypad_scanner_thread():
    """Always scan; only process keys when PIR is active (0)."""
    global keypad_last_state
    rows = KEYPAD_ROWS
    cols = KEYPAD_COLS

    while not stop_event.is_set():
        active = pir_active()

        for c_idx, c_pin in enumerate(cols):
            GPIO.output(c_pin, GPIO.LOW)

            for r_idx, r_pin in enumerate(rows):
                pressed_now = GPIO.input(r_pin) == GPIO.LOW  # active LOW
                pressed_before = keypad_last_state[r_idx][c_idx]

                if pressed_now and not pressed_before:
                    key = KEYPAD_LAYOUT[r_idx][c_idx]
                    if active:
                        handle_keypress(key)
                    while GPIO.input(r_pin) == GPIO.LOW and not stop_event.is_set():
                        time.sleep(KEY_DEBOUNCE_RELEASE_SEC)

                keypad_last_state[r_idx][c_idx] = pressed_now

            GPIO.output(c_pin, GPIO.HIGH)

        time.sleep(KEYPAD_SCAN_INTERVAL)

def sensors_thread():
    global last_distance_cm
    last_dht_time = 0
    DHT_PERIOD = 5  # seconds; safe for DHT11

    while not stop_event.is_set():
        if not slide_switch_on():
            motor_off()
            time.sleep(SENSOR_POLL_INTERVAL)
            continue

        # PIR (also updates shared state)
        active = pir_active()

        # Ultrasonic (periodic)
        try:
            distance = read_ultrasonic_distance_cm()
            last_distance_cm = distance
        except Exception as e:
            print(f"[ULTRASONIC] Read error: {e}")
            distance = -1

        if distance > 0:
            if distance > WATER_LOW_DISTANCE_CM:
                print(f"[WATER] LOW! Distance={distance:.2f} cm (> {WATER_LOW_DISTANCE_CM} cm)")
                if alerts:
                    alerts.maybe_send("water_low", True,
                        f"⚠️ Water LOW (distance {distance:.2f} cm)")
            else:
                print(f"[WATER] OK. Distance={distance:.2f} cm")
                if alerts:
                    alerts.maybe_send("water_low", False, "✅ Water OK again.")
        else:
            print("[ULTRASONIC] Bad reading.")

        height = calc_water_height(distance if distance > 0 else BEAKER_HEIGHT_CM)

        # Moisture / motor
        was_dry = moisture_is_dry()
        check_and_drive_motor()
        if alerts:
            alerts.maybe_send("soil_dry", was_dry,
                "🌱 Soil is DRY — motor turned ON." if was_dry else "🌧️ Soil MOIST — motor turned OFF.")

        # Periodic DHT refresh
        now = time.time()
        if now - last_dht_time >= DHT_PERIOD:
            last_dht_time = now
            try:
                humidity, temperature = Adafruit_DHT.read_retry(DHT_SENSOR, DHT_PIN_BCM)
            except Exception:
                humidity, temperature = None, None

            if temperature is not None and (TEMP_MIN_C <= temperature <= TEMP_MAX_C):
                with readings_lock:
                    readings["temp_c"] = float(temperature)
                if humidity is not None:
                    with readings_lock:
                        readings["humidity"] = float(humidity)

        # Update shared ultrasonic/water
        with readings_lock:
            readings["distance_cm"] = float(distance) if distance > 0 else None
            readings["water_height"] = float(height)

        time.sleep(SENSOR_POLL_INTERVAL)

def thingspeak_thread():
    if not ts_client:
        return
    while not stop_event.is_set():
        # Respect min 15s interval inside client
        with readings_lock:
            payload = dict(readings)
        ok = ts_client.push(**payload)
        if ok:
            print("[ThingSpeak] Updated.")
        else:
            print("[ThingSpeak] Skipped or failed.")
        time.sleep(5)  # loop frequently; client enforces 15s guard

# --------------- MAIN -------------------
def main():
    # --------------- SETUP ------------------
    GPIO.setup(SLIDE_SWITCH_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    # PIR active-low -> pull-up keeps it at 1 when idle
    GPIO.setup(PIR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.setup(DC_MOTOR_PIN, GPIO.OUT)
    GPIO.output(DC_MOTOR_PIN, GPIO.LOW)

    GPIO.setup(ULTRA_TRIG, GPIO.OUT)
    GPIO.setup(ULTRA_ECHO, GPIO.IN)
    GPIO.output(ULTRA_TRIG, GPIO.LOW)

    GPIO.setup(MOISTURE_PIN, GPIO.IN)

    # Keypad setup
    for c in KEYPAD_COLS:
        GPIO.setup(c, GPIO.OUT)
        GPIO.output(c, GPIO.HIGH)

    for r in KEYPAD_ROWS:
        GPIO.setup(r, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    print("[SYSTEM] Booting. BOARD pin numbering.")
    print("PIR is active-LOW (0 = presence). Keypad only acts when PIR is 0.")
    print("Keypad: 1 -> LCD water level, 2 -> LCD temperature (only).")
    lcd_print("System Ready")

    everything_off()

    kt = threading.Thread(target=keypad_scanner_thread, daemon=True)
    st = threading.Thread(target=sensors_thread, daemon=True)
    ct = threading.Thread(target=thingspeak_thread, daemon=True)
    kt.start()
    st.start()
    ct.start()

    start_telegram_bot()  # start polling bot if configured

    try:
        while True:
            time.sleep(MAIN_LOOP_SLEEP)
    except KeyboardInterrupt:
        print("\n[EXIT] KeyboardInterrupt")
    finally:
        stop_event.set()
        if tg_bot:
            tg_bot.stop()
        time.sleep(0.2)
        everything_off()
        with lcd_lock:
            lcd.clear()
        GPIO.cleanup()
        print("[SYSTEM] Clean exit.")

if __name__ == "__main__":
    main()

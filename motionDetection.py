import RPi.GPIO as GPIO
from time import sleep
import subprocess
import os

# Constants for pins and state file
PIR_PIN = 17
BUZZER_PIN = 18
LED_PIN = 24
BUTTON_PIN = 21
STATE_FILE = "system_state.txt"

# Setup
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

GPIO.setup(PIR_PIN, GPIO.IN)
GPIO.setup(BUZZER_PIN, GPIO.OUT)
GPIO.setup(LED_PIN, GPIO.OUT)
GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

print("Waiting for PIR to stabilize...")
sleep(5)

#different states of the current system
PIR_state = 0
keypad_launched = False
motion_enabled = True
last_button_state = 0

def read_state():
    if not os.path.exists(STATE_FILE):
        return "enabled"  # Default to enabled
    with open(STATE_FILE, "r") as f:
        return f.read().strip()

while True:
    # Check button press (rising edge)
    button_state = GPIO.input(BUTTON_PIN)
    if button_state == 1 and last_button_state == 0:
        motion_enabled = not motion_enabled
        print(f"🔘 Motion sensor manually {'ENABLED' if motion_enabled else 'DISABLED'}")
        # Turn off buzzer/light immediately if disabled
        if not motion_enabled:
            GPIO.output(BUZZER_PIN, GPIO.LOW)
            GPIO.output(LED_PIN, GPIO.LOW)
            PIR_state = 0
        sleep(0.2)  # Debounce delay
    last_button_state = button_state

    # Motion detection logic only if enabled
    if motion_enabled:
        system_state = read_state()
        if GPIO.input(PIR_PIN):  # Motion detected
            if PIR_state == 0:
                print("Motion detected!")
                # turns buzzer and light on if system is enabled
                if system_state == "enabled":
                    GPIO.output(BUZZER_PIN, GPIO.HIGH)
                    GPIO.output(LED_PIN, GPIO.HIGH)
                else:
                    print("System logic DISABLED — not turning on buzzer/light")

                if not keypad_launched:
                    subprocess.Popen(["python3", "keypad_control.py"])
                    keypad_launched = True

                PIR_state = 1

        else:  # No motion
            if PIR_state == 1:
                print("No motion.")
                GPIO.output(BUZZER_PIN, GPIO.LOW)
                GPIO.output(LED_PIN, GPIO.LOW)
                PIR_state = 0

    sleep(0.1)

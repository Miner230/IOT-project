import RPi.GPIO as GPIO
from time import sleep

STATE_FILE = "system_state.txt"

def write_state(state):
    with open(STATE_FILE, "w") as f:
        f.write(state)

# Setup GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

MATRIX = [
    [1, 2, 3],
    [4, 5, 6],
    [7, 8, 9],
    ['*', 0, '#']
]

ROW = [6, 20, 19, 13]
COL = [12, 5, 16]

# Setup columns as outputs
for i in range(3):
    GPIO.setup(COL[i], GPIO.OUT)
    GPIO.output(COL[i], 1)

# Setup rows as inputs with pull-ups
for j in range(4):
    GPIO.setup(ROW[j], GPIO.IN, pull_up_down=GPIO.PUD_UP)

print("Keypad active: # to ENABLE, * to DISABLE")

while True:
    for i in range(3):
        GPIO.output(COL[i], 0)
        for j in range(4):
            if GPIO.input(ROW[j]) == 0:
                key = MATRIX[j][i]
                print(f"Key pressed: {key}")

                if key == '#':
                    print("✅ System logic ENABLED")
                    write_state("enabled")
                elif key == '*':
                    print("❌ System logic DISABLED")
                    write_state("disabled")

                while GPIO.input(ROW[j]) == 0:
                    sleep(0.1)  # debounce

        GPIO.output(COL[i], 1)

    sleep(0.1)

from gpiozero.pins.pigpio import PiGPIOFactory
from gpiozero import AngularServo
from time import sleep


my_factory = PiGPIOFactory()

# Use BCM numbering, not BOARD
servo = AngularServo(17, initial_angle=0, min_angle=0, max_angle=180, min_pulse_width=0.0005, max_pulse_width=0.0025, frame_width=0.02, pin_factory=my_factory)  # Use GPIO 17 (BCM numbering) for pin 11

min_pulse_width = servo.min_pulse_width

try:
    while True:
        # Ask user for angle and turn servo to it
        angle = float(input('Enter angle between 0 & 180: '))
        if 0 <= angle <= 180:
            servo.angle = angle
            sleep(0.5)
            servo.angle = None
        else:
            print("Angle out of range. Please enter a value between 0 and 180.")
            
finally:
    servo.angle = None
    servo.close()
    print("Goodbye!")
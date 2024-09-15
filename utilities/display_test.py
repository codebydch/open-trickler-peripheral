import time
import digitalio
import board
from PIL import Image, ImageDraw, ImageFont
from gpiozero import Button

from adafruit_rgb_display import st7789

# Configuration for CS and DC pins for Raspberry Pi
cs_pin = digitalio.DigitalInOut(board.CE0)
dc_pin = digitalio.DigitalInOut(board.D25)
reset_pin = None
BAUDRATE = 64000000  # The pi can be very fast!
# Create the ST7789 display:
disp = st7789.ST7789(
    board.SPI(),
    cs=cs_pin,
    dc=dc_pin,
    rst=reset_pin,
    baudrate=BAUDRATE,
    width=135,
    height=240,
    x_offset=53,
    y_offset=40,
)

backlight = digitalio.DigitalInOut(board.D22)
backlight.switch_to_output()
backlight.value = True

# Define colors
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)
GREEN = (0, 255, 0)

# Initialize buttons
button1 = Button(23)  # GPIO pin 23
button2 = Button(24)  # GPIO pin 24

# Initialize variables
number = 0.0
start = False
digit_index = 0  # To track which digit is being edited

def update_display():  
    image = Image.new('RGB', (disp.width, disp.height), BLACK)
    draw = ImageDraw.Draw(image)
    
    # Draw the number
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
    text = f"{number:05.2f}"
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_x = (disp.width - text_width) // 2
    text_y = 60
    draw.text((text_x, text_y), text, font=font, fill=WHITE)
    
    # Draw the underline for the current digit
    underline_y = text_y + text_height + 10
    char_widths = [draw.textbbox((0, 0), char, font=font)[2] - draw.textbbox((0, 0), char, font=font)[0] for char in text]
    underline_x = text_x + sum(char_widths[:digit_index])
    draw.line((underline_x, underline_y, underline_x + char_widths[digit_index], underline_y), fill=WHITE, width=2)
    
    # Draw the stop/start squares
    if start:
        draw.rectangle((10, 200, 60, 230), outline=GREEN, fill=GREEN)
    else:
        draw.rectangle((10, 200, 60, 230), outline=RED, fill=RED)
    
    disp.image(image)

def increment_digit():
    global number, digit_index
    number_str = f"{number:05.2f}"
    digits = list(number_str)
    
    if digits[digit_index].isdigit():
        digits[digit_index] = str((int(digits[digit_index]) + 1) % 10)
    
    number = float("".join(digits))
    update_display()

def move_to_next_digit():
    global digit_index
    digit_index = (digit_index + 1) % 5
    if digit_index == 2: #skip over the decimal
        digit_index = 3
    update_display()

def toggle_start_stop():
    global start
    start = not start
    update_display()

# Main loop
update_display()
while True:
    if button1.is_pressed and button2.is_pressed:
        toggle_start_stop()
        time.sleep(0.5)  # Debounce delay
    elif button1.is_pressed:
        increment_digit()
        time.sleep(0.1)  # Debounce delay
    elif button2.is_pressed:
        move_to_next_digit()
        time.sleep(0.1)  # Debounce delay
    time.sleep(0.1)  # Sleep for a short period to avoid high CPU usage

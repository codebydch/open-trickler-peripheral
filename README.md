# Open Trickler — two-trickler build

A fork of [Ammolytics' Open Trickler](https://github.com/ammolytics/open-trickler-peripheral),
rebuilt around a different machine: a servo-driven powder measure, **two** vibratory
tricklers, a Mini PiTFT screen with buttons, and browser control instead of Bluetooth.

Runs on a Raspberry Pi under Raspberry Pi OS as a set of systemd services. Everything is
reachable at [http://opentrickler.local](http://opentrickler.local).

## How a charge is thrown

1. You set a target weight — on the screen, or from the control panel in a browser — and
   turn auto mode on.
2. When the pan is on the scale, settled, and under target, the servo trips the powder
   measure for a coarse drop.
3. Both tricklers run under PID control until the charge is within `fine_trickle_weight`
   of target, then trickler 2 shuts off and trickler 1 continues alone.
4. Inside `pulse_trickle_weight`, continuous feeding stops for good and the **pulse
   feeder** finishes the charge.

The pulse feeder is the part that decides accuracy. A vibratory motor can't be driven
slower than its stall point, so the only way to control how much powder lands is to
control how long it runs. Each pulse is aimed at a fraction of what's left, fired, and
then weighed once the scale reports stable — nothing is fed until the last thing fed has
been measured. The measured dose corrects a running estimate of grains per second of
motor on-time, so pulse length adapts to the powder instead of being configured. It stops
when another pulse would miss the target by more than stopping short does.

That learned rate is kept in memcache between charges and shown on the tuning page. If
you change powder, clear it there and it re-learns within a few charges.

## Pages

| URL | What it is |
| --- | --- |
| `/` | Index and links to the log viewers |
| `/app/` | Control panel: set target weight, toggle auto mode |
| `/app/config/` | Tuning page: trickler settings, live scale readout, learned feed rate |
| `/servo/` | Servo control panel, for setting up the powder measure |
| `/opentrickler.html` | Trickler log |
| `/screen.html` | Screen log |
| `/flask.html` | Control panel log |
| `/system.html` | Full system log |

Changes made on the tuning page apply to the **next charge** without restarting anything,
and are written back to `opentrickler_config.ini` so they survive a reboot.

## Install

See [`setup.txt`](setup.txt) for the full walkthrough — swap size, the `/code` directory,
the virtualenv, Adafruit Blinka for the screen, memcached, nginx, websocketd, and
enabling the services in [`system/`](system).

## Configuration

`opentrickler_config.ini` is the single source of truth, and every value is commented in
place. The sections worth knowing:

- `[scale]` — model, serial port, baud rate. Supports A&D, Creedmoor and U.S. Solid.
- `[motor1]` / `[motor2]` — GPIO pin and PWM limits per trickler. `trickler_min_pwm` is
  the floor the motor is driven at; set it just above the speed where powder stops
  moving.
- `[trickler]` — the final approach. `fine_trickle_weight`, `pulse_trickle_weight`,
  pulse timing, and the learned-rate seed. All weights are in **grains** and converted
  automatically if the scale is set to grams.
- `[servo]` — powder measure travel and pulse widths.
- `[PID]` — gains for the continuous phases only. The pulse feeder does not use the PID.

**Tuning for accuracy:** `pulse_trickle_weight` must be comfortably larger than what a
trickler can throw during the time the scale takes to report a change — feed rate ×
scale lag. If charges run heavy, raise that first. `pulse_min_on_time` sets the finest
dose the machine can place, and so the best accuracy it can reach.

## Services

| Unit | Runs |
| --- | --- |
| `opentrickler` | `trickler/main.py` — the control loop |
| `opentrickler_screen` | `trickler/screen.py` — Mini PiTFT and buttons |
| `opentrickler_flask_app` | `trickler/app.py` — control panel and tuning page (:5000) |
| `opentrickler_flask_servo_app` | `trickler/servo_app.py` — servo panel (:5001) |
| `websocketd-1,2,4,5` | Log streams for the browser viewers |

## Debugging

```bash
journalctl -u opentrickler -f
systemctl status opentrickler --no-pager
```

The trickler daemon logs each pulse's on-time and measured dose during the final
approach, which is the fastest way to see whether the feed rate has been learned sensibly.

Deploy is a `git pull` into `/code/open-trickler-peripheral` followed by a restart. Make
sure the whole tree updates — `trickler/main.py`, `scales.py` and `helpers.py` depend on
each other, and a partial update fails in ways that are hard to read:

```bash
cd /code/open-trickler-peripheral && git status --short   # should be empty
sudo systemctl restart opentrickler
```

## Developer setup

```bash
sudo apt install memcached
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-to-freeze.txt
```

`utilities/` holds standalone hardware tests for the servo, the display, and logging.
`trickler/motors.py` and `trickler/scales.py` can each be run directly against a config
file to exercise the hardware on its own.

## References

- https://onion.io/2bt-pid-control-python/
- https://github.com/ivmech/ivPID/blob/master/PID.py
- https://gpiozero.readthedocs.io/en/stable/api_output.html#gpiozero.PWMOutputDevice
- https://pythonhosted.org/pyserial/shortintro.html
- https://pymemcache.readthedocs.io/en/latest/
- https://learn.adafruit.com/adafruit-arduino-lesson-13-dc-motors?view=all

## License

MIT, as inherited from the upstream Ammolytics project. See [LICENSE](LICENSE).
`trickler/PID.py` is from [ivPID](https://github.com/ivmech/ivPID) and is GPL-licensed,
as noted in its header.

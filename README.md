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
   measure for a coarse drop. The drop is **weighed**: a kernel of powder caught in the
   drum stops the measure dead, and a jam delivers nothing at all, so anything under
   `stall_drop_weight` on the scale means it never cycled. The measure gets worked again
   up to `max_dump_attempts` times, and if nothing ever comes out the machine stops --
   auto mode off, with the reason on the control panel -- rather than asking the
   tricklers to build the whole charge by vibration.
3. Both tricklers run under PID control until the charge is within `fine_trickle_weight`
   of target, then trickler 2 shuts off and trickler 1 continues alone.
4. Inside `pulse_trickle_weight`, continuous feeding stops for good and the **pulse
   feeder** finishes the charge.

The pulse feeder is the part that decides accuracy. A vibratory motor can't be driven
slower than its stall point, so the only way to control how much powder lands is to
control how long it runs. Each pulse is aimed at a fraction of what's left, fired, and
then weighed once the scale reports stable — nothing is fed until the last thing fed has
been measured.

Weighing is what costs the time: a pulse takes a fraction of a second to fire and about
half a second to weigh, whatever it delivered. So pulses fired while there is still a way
to go run at `pulse_fast_pwm`, and only the last ones — inside `pulse_fast_until` — drop
to the fine `pulse_pwm`. Fewer, larger pulses, without coarsening the smallest dose the
machine can place, which is what the accuracy rests on. Simply running the motor faster
throughout does the opposite: it is quicker and it misses. It waits `settle_min_time` before believing that "stable", because for the
first moment after a pulse the powder is still in the air and the undisturbed pan reads as
settled at the old weight. The measured dose corrects a running estimate of grains per second of
motor on-time, so pulse length adapts to the powder instead of being configured. It stops
when another pulse would miss the target by more than stopping short does.

Each speed has its own measured rate, since a vibratory feeder's throughput against
drive is not reliably linear, and a pulse at a speed nothing has been weighed at is a
short probe rather than an aimed dose. Both account for the motor's spin-up
(`pulse_dead_time`): without that, a rate measured from a short pulse reads low — most of
that pulse was spin-up — and the pulse sized from it comes out too long.

**Powder arrives as whole grains, and the feeder is built around that.** One grain of
stick powder and one division of a 0.02 gn scale are about the same weight, so a pulse
delivers no grain, one, or three — never 0.01 gn. Three consequences: the feed rate is
measured over a *window* of pulses rather than one at a time (judging each pulse alone,
and discarding the ones that read zero, keeps the hits and throws away the misses, which
overestimated the rate threefold on the bench); a run of pulses without a grain is
ordinary and does not mean the hopper is empty; and inside a few divisions of target the
feeder stops calculating doses that cannot exist and simply places one grain at a time.
±0.02 gn is the floor, and no setting gets below it.

That learned rate is kept between charges — and across reboots, in `learned.json` beside
the charge history — and shown on the tuning page, scoped to the selected **powder
profile**, so switching from a stick powder to a ball powder switches the estimate rather
than blending the two into an average that fits neither. Profiles are
created from the tuning page and stored as `[profile:Name]` sections in the config file.

Every charge is recorded: target, what it actually weighed, the error, how many pulses it
took and how long. `/app/history` shows the last hundred with the mean error, standard
deviation, and the share that landed inside ±0.02 gn — which is the number that answers
whether the machine is accurate enough.

Every **pulse** is recorded too — motor speed, how long it ran, what it delivered. From
pulses at two different lengths the history page solves for the steady feed rate and the
motor spin-up, which is the sum that set `pulse_dead_time` and used to be done by hand from
the journal. The daemon writes the pulse file once per charge, not once per pulse, to spare
the SD card.

## Pages

| URL | What it is |
| --- | --- |
| `/` | Index and links to the log viewers |
| `/app/` | Control panel: set target weight, toggle auto mode |
| `/app/config/` | Tuning page: trickler settings, live scale readout, learned feed rate, powder profiles |
| `/app/history` | Every charge thrown, with mean error, spread, and how many were in tolerance |
| `/servo/` | Servo control panel, for setting up the powder measure |
| `/opentrickler.html` | Trickler log |
| `/screen.html` | Screen log |
| `/flask.html` | Control panel log |
| `/system.html` | Full system log |

Changes made on the tuning page apply to the **next charge** without restarting anything,
and are written back to `opentrickler_config.ini` so they survive a reboot.

## Install

On a fresh Raspberry Pi OS image, in two parts. The split is where Adafruit Blinka
forces a reboot:

```bash
git clone https://github.com/codebydch/open-trickler-peripheral.git
./open-trickler-peripheral/install-part1.sh    # system packages, venv, Blinka
sudo reboot
/code/open-trickler-peripheral/install-part2.sh # websocketd, nginx, the services
```

Run them as your normal login user, not with `sudo` — they call it themselves. Both are
safe to re-run, and part 2 checks that part 1 has been done before it starts.

[`setup.txt`](setup.txt) documents the same steps by hand, if you would rather do it
yourself or want to see what the scripts are doing.

### Swap, on a Pi with little RAM

**Set this up before running part 1 if your Pi has 512 MB of RAM or less** (a Pi Zero, Pi
Zero 2, or an older Model A). The `apt full-upgrade` in part 1 is memory-hungry enough to
crash such a board outright. The install scripts deliberately leave swap alone rather than
reconfiguring it behind your back.

[Pi My Life Up's swap file guide](https://pimylifeup.com/raspberry-pi-swap-file/) walks
through it. Which mechanism you have depends on the OS version:

- **Bookworm and earlier** use `dphys-swapfile`. Set `CONF_SWAPSIZE=2048` in
  `/etc/dphys-swapfile`, then `sudo dphys-swapfile setup && sudo dphys-swapfile swapon`.
- **Trixie and later** use [`rpi-swap`](https://github.com/raspberrypi/rpi-swap), which is
  configured in `/etc/rpi/swap.conf` (or a drop-in under `/etc/rpi/swap.conf.d/`) and
  documented in `man swap.conf`.

Two things catch people out on Trixie. The default `Mechanism=auto` resolves to
`zram+file`, where the swap is compressed RAM and `/var/swap` is only a *writeback target*
— so a 2 GB `/var/swap` is not 2 GB of usable swap. And `[File] RamMultiplier=1` sizes the
file from your RAM, so `MaxSizeMiB=2048` is only a ceiling: a 512 MB Pi gets 512 MB. For a
plain disk-backed swap file like the old behaviour, set `Mechanism=swapfile` under
`[Main]` and `FixedSizeMiB=2048` under `[File]`.

Check what you actually ended up with using `swapon --show` and `free -h`.

## Configuration

`opentrickler_config.ini` is the single source of truth, and every value is commented in
place. It is **not** in git — the control panel writes tuning back to it, so a tracked
copy would put every value you set in the way of the next update. `install-part2.sh`
creates it from `opentrickler_config.ini.example`, which is the tracked file holding the
shipped defaults; `update.sh` leaves your copy alone and tells you about settings a new
version added. The sections worth knowing:

- `[scale]` — model, serial port, baud rate. Supports A&D, Creedmoor and U.S. Solid.
- `[motor1]` / `[motor2]` — GPIO pin and PWM limits per trickler. `trickler_min_pwm` is
  the floor the motor is driven at; set it just above the speed where powder stops
  moving.
- `[trickler]` — the final approach. `fine_trickle_weight`, `pulse_trickle_weight`,
  the two pulse speeds (`pulse_pwm` and `pulse_fast_pwm`, with `pulse_fast_until`
  deciding where one hands over to the other), pulse timing, and the learned-rate seed.
  Setting `pulse_fast_pwm` equal to `pulse_pwm` gives single-speed pulsing back. All weights are in **grains** and converted
  automatically if the scale is set to grams.
- `[history]` — where charges and pulses are recorded and the learned feed rates kept
  (`/var/lib/opentrickler/charges.csv`, `pulses.csv` and `learned.json` by default,
  outside the repo so a `git pull` can't disturb them) and how many rows to keep.
- `[profiles]` — the powder profile in use; each is a `[profile:Name]` section.
- `[servo]` — powder measure travel and pulse widths, in **microseconds**. Set
  `servo_angle` from the servo page: work up until the measure gives a full drop, and
  stop there rather than driving it into its stop.
- `[PID]` — gains for the continuous phases only. The pulse feeder does not use the PID.

**Tuning for accuracy:** `pulse_trickle_weight` must be comfortably larger than what a
trickler can throw during the time the scale takes to report a change — feed rate ×
scale lag. If charges run heavy, raise that first.

**Tuning for speed:** raise `pulse_pwm`. A vibratory trickler at its stall speed can be
slow enough that the final approach takes over a minute, and since one grain is already a
whole scale division, running the motor harder costs nothing in accuracy until a single
pulse starts dropping several grains at once. On the reference machine — Frankford
Arsenal tricklers with 11,000 RPM vibration motors — 25% delivered 0.036 gn/s and took 77
seconds; 30% roughly tripled that.

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

If `opentrickler_screen` fails with `/dev/spidev0.0 does not exist`, the SPI interface is
off — the screen is the only thing that needs it, so everything else will be running fine:

```bash
sudo raspi-config nonint do_spi 0 && sudo reboot
```

To update, use [`update.sh`](update.sh) rather than a bare `git pull`:

```bash
/code/open-trickler-peripheral/update.sh
```

A pull on its own is not enough. nginx serves *copies* of the pages from `/var/www/html`
and the unit files live in `/etc/systemd/system`, so both go stale. The script pulls,
republishes the pages, refreshes the services and restarts them.

It refuses to run with a dirty working tree, and says which files are modified. Your
tuning is not among them — `opentrickler_config.ini` is git-ignored — so anything listed
is a real edit to the code. Take it seriously: a partial update is how the hardest bug in
this project happened: `trickler/main.py`, `scales.py` and `helpers.py` depend on each
other, and a mismatched set fails in ways that are hard to read.

## Developer setup

On a Pi, where the full requirements install:

```bash
sudo apt install memcached
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-to-freeze.txt
```

Anywhere else, install only what the tests import -- `requirements-to-freeze.txt` is the
Pi's list, C extensions included:

```bash
python3.13 -m venv .venv && .venv/bin/pip install pymemcache flask pyserial gpiozero pillow
```

Run the tests from the repository root:

```bash
python -m unittest discover -t . -s tests
```

They drive the real scale, motor and control-loop classes against a fake serial port and
a simulated machine, so they run anywhere and cover the parts that are awkward to check
on the bench: frame parsing, motor clamping, every exit path from a charge, and whether a
charge actually lands on target. `tests/fakes.py` holds the simulated hardware.

Run them on **Python 3.13**, which is what Raspberry Pi OS Trixie ships and what CI runs.
A passing run on an older interpreter is not proof: stacking `@classmethod` on `@property`
worked until 3.13 removed it, and the scales module hit that on Trixie and nowhere else.

The screen is covered too, by stubbing the two Pi-only modules `screen.py` imports and
drawing into an in-memory image, so the digit editor and the jam banner are checked
without hardware. Those tests skip themselves if Pillow or the DejaVu font is missing.

`utilities/` holds standalone hardware tests for the servo, the display, and logging.
`trickler/motors.py` and `trickler/scales.py` can each be run directly against a config
file to exercise the hardware on its own.

## A note on GPIO libraries

The trickler motors go through [gpiozero](https://gpiozero.readthedocs.io/), which selects
its own pin factory and prefers `lgpio`. The **servo does not** — it talks to
[lgpio](https://abyz.me.uk/lg/py_lgpio.html) directly. That is worth explaining, because it
is the one place this project reaches past gpiozero.

A servo is positioned by the width of a pulse repeated every 20 ms, and 1 µs of pulse is
roughly a tenth of a degree. gpiozero drives one with a PWM device, and its lgpio backend
sets the duty cycle like this:

```python
self._pwm = (freq, int(value * 100))     # gpiozero/pins/lgpio.py
```

The duty cycle is truncated to a **whole percent**, which at 50 Hz is a 200 µs step in
pulse width, always rounding down. On this machine a commanded 1522 µs arrived as 1400 µs
and the measure dropped half a charge; commanding one more degree — 98° to 99° — crossed a
percent boundary and swung the horn a quarter turn. `lgpio.tx_servo()` takes microseconds,
which is what [pigpio](https://github.com/joan2937/pigpio)'s `set_servo_pulsewidth()` did
before its author archived it in favour of [lg](https://abyz.me.uk/lg/rgpiod.html), so the
timing is back to what the measure was set up against.

Owning the `gpiochip` handle also fixes pin sharing. gpiozero's `Device.close()` does not
release an lgpio line — `LGPIOPin.close()` re-claims it as an input, and the line is only
freed when the factory's chip handle closes — so the trickler held GPIO17 for the life of
the process and `/servo/` answered `GPIO busy`. The trickler now claims the line only while
it is actually dumping powder, and closes the handle afterwards.

Two consequences worth knowing. The servo is released once it has finished moving rather
than held at its initial angle: an idle servo on a software-timed signal buzzes and hunts,
and the measure holds its own position mechanically. And `/servo/` works whenever the
trickler is idle, reporting a readable message if you try to use it mid-charge. If your
measure relies on the servo actively holding the lever, remove the `servo_motor.off()` call
after the powder dump in `trickler/main.py` — but be aware that leaving it holding the line
locks the servo page out.

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

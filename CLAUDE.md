# CLAUDE.md

A fork of Ammolytics' OpenTrickler: a Raspberry Pi Zero 2 W that throws a powder charge
with a servo-driven Lyman Brass Smith powder measure, then trickles it up to weight with
two Frankford Arsenal vibratory tricklers (uxcell 3 V vibration motors) on an A&D scale
reading in 0.02 gn steps. Mini PiTFT screen, Flask control panel behind nginx, memcache
for state shared between the daemons. Target accuracy is one scale division, 0.02 gn.
`README.md` covers install, pages, services and debugging for people; this file covers
what a new session would otherwise get wrong.

## How work happens here

- **You cannot reach the Pi.** No SSH, no network path. Every piece of hardware evidence
  comes from logs the owner pastes, normally `journalctl -u opentrickler | grep remainder`.
- **Bench first, code second.** The owner's rule, stated more than once: test the setting
  before coding or planning. The loop that has worked: change one value on the tuning page
  (`/app/config/`), get three charges of log, analyse, then decide whether code follows.
  Don't write code ahead of the data, and don't propose two changes in one bench run.
- **Shipped defaults need bench evidence**, and the evidence goes in the comment beside the
  value in `opentrickler_config.ini.example` (see `pulse_dead_time` there for the pattern).
- **`develop` is the default branch.** The owner merges Claude branches themselves.
- Report what the log actually shows, including when an earlier diagnosis was wrong.

## Running the tests

```bash
python3.13 -m venv .venv && .venv/bin/pip install pymemcache flask pyserial gpiozero pillow
.venv/bin/python -m unittest discover -t . -s tests      # from the repo root; 193 tests
```

No pytest. Use 3.13 -- it's what the Pi runs, and 3.13 has broken this code before when
older interpreters passed (see `classproperty` in `scales.py`). Don't install
`requirements-to-freeze.txt` in a container: it's the Pi's full list, C extensions included.
A venv avoids Debian's own `blinker`, which blocks a system-wide pip install.

`tests/fakes.py` is a simulated machine -- real scale, motor and loop classes against a
fake serial port, delivering powder as **whole grains** (0.02 gn nominal, ±25%) with a
0.12 s motor spin-up. Tests that reason about specific arithmetic pin the settings they
use (`pulse_dead_time=0.02` in `GranularDeliveryTest`) rather than inheriting the shipped
defaults, so they keep testing the same thing when a default moves.

## Layout

- `trickler/main.py` -- the charge loop: continuous PID trickling, then `PulseFeeder` for
  the final approach. Most of the hard-won logic is here. `FeedModel` is what a powder has
  taught the machine, kept across charges; `run_pass` is one pass of the daemon's idle
  loop, where a Phase 2 calibration routine would sit beside `trickler_loop`.
- `trickler/helpers.py` -- `TRICKLER_SETTINGS` (tuning-page fields, defaults, ranges),
  `load_config`, `update_ini_section` (rewrites the ini without losing its comments).
- `trickler/scales.py`, `motors.py` (servo through `lgpio.tx_servo`, not gpiozero),
  `screen.py`, `app.py` (control panel), `servo_app.py` (servo setup page).
- `opentrickler_config.ini.example` is tracked; the live `opentrickler_config.ini` is
  git-ignored because the tuning page writes to it. `install-part2.sh` and `update.sh`
  create it from the example when it's missing.

## What the machine does -- measured, don't re-derive

- **Powder arrives in whole grains of ~0.02 gn, which is also one scale division.** Every
  dose ever measured is 0.00, 0.02, 0.04... Never model delivery as continuous; the
  simulator did, and that hid four separate defects.
- **Learn the rate as accumulated weight / accumulated moving time over a window.**
  Judging pulses one at a time and dropping the zero-dose ones biased it 3x high.
- **At 30% pulse drive: steady feed 0.217 gn/s, motor spin-up 0.118 s.** From a two-point
  fit -- 120 pulses at 0.2 s averaged 0.0178 gn, 32 at 0.4 s averaged 0.0613. Twice the
  pulse gave 3.4x the powder, and only a long spin-up explains that.
- **The first pulse after the tube has been running is a burst** -- 2-10x the steady rate,
  because the tube is loaded. On a short pulse a burst implies a huge rate and drags the
  stored rate with it (seen: 0.302 -> 0.665 gn/s from one four-grain pulse).
- **Charge time is set by where continuous trickling hands over**, at roughly 17 s per
  grain left for the pulse feeder. `rate_window = 12` (~0.5 s) keeps that handover steady;
  at 4 samples it measured the scale's 0.02 step instead of a rate.
- Current performance: ~10-13 s a charge, finishing 54.98-55.02 on a 55.00 target.

## Decided, with the reason -- don't relitigate without new evidence

- **`cutoff_weight = 0.02`** -- stops a grain light on purpose. A light charge gets
  trickled up; a heavy one has to be dumped. The owner's call.
- **`pulse_on_time = 0.4`** -- at 0.2 the cap bound nearly every pulse; past 0.4 the gain
  flattens and clumps start to overshoot.
- **Two-speed pulsing was tested and found not to pay**: 45% drive gave about 1.1x the
  powder per pulse of 30%, not the 2.5x drive suggests, and the fast pulses load the tube
  so the first fine pulse bursts. The code stays in, dormant when `pulse_fast_pwm` =
  `pulse_pwm`. The owner is currently running the shipped 25/45 and changes such values by
  hand on the tuning page; don't move the shipped defaults without asking.
- An "E" on the scale when the pan is lifted was a scale fault, fixed by resetting the
  scale -- not code.

## Things that bit before

- gpiozero's lgpio backend truncates PWM duty to whole percent (200 µs at 50 Hz), so the
  servo goes through `lgpio.tx_servo()`. gpiozero's `close()` doesn't free an lgpio line;
  only closing the chip handle does.
- `configparser.read()` ignores a missing file and returns an empty config. Always load
  through `helpers.load_config`.
- `--verbose` uses `default=None`; with `store_true` alone it's `False` when absent and the
  config file's value never applied. And the config value has to be read with
  `getboolean`: `config['general']['verbose']` is the *string* `'False'`, which is true, so
  every daemon ran at DEBUG whatever the ini said and journald rotated the charge lines out
  within minutes. `helpers.log_level` is the one place the level is decided now.
- `done()` must not use `min_dose` until a rate has been measured -- a high seed rate
  made it declare charges complete before firing anything.
- Stopping a grain light (`cutoff_weight = 0.02`) left the pan reading under target, and
  the idle loop's "weight < target" started a new charge on it every pass -- each complete
  before its first pulse and each recorded, 28 in 30 simulated passes. `run_pass` now
  requires the remainder to exceed `cutoff_weight`, the same rule the charge finishes on.
- Learned rates live in a `FeedModel` per profile (`main.py`), saved to `learned.json`
  beside the charge history so they survive a reboot, and mirrored to memcache for the
  tuning page. "Clear learned feed rate" deletes the memcache copy and sends the daemon a
  `reset_learned` command through the `TRICKLER_COMMAND` memcache key, which is also where
  Phase 2's "calibrate" will go. Every pulse is in `pulses.csv`; `/app/history` fits rate
  and spin-up from it, so bench evidence no longer has to be pasted from the journal.
- Reading pulse logs: `remainder: R ... scale: W ... pulsed T s -> D (rate X/s)` -- R is
  before the pulse, W after, and X is the **fine** rate only. Fast-pulse learning isn't
  logged.

## Phase 2: learning and adjusting on the fly

The next piece of work. What's known going in:

- **Per-powder calibration** was the owner's original idea: pick a powder, run a short
  routine, store its constants per profile. Rate and spin-up both come out of ~30 pulses
  at two lengths. Also worth capturing: dump weight and stall PWM. Powders differ in grain
  shape, length and weight, and the measure's drop volume changes per powder and calibre.
- **Owner's constraints for any calibration routine:** empty the pan between runs (cups
  hold ~200 gn and powder bounces off the pile onto the scale); an empty tube takes about
  10 s of running to fill; a full-power run fills the tube while pulsing lays the grains
  out, so delivery starts slow and then settles.
- **Known weakness to fix there:** each charge's rate window starts empty
  (`FeedModel.new_charge` clears it), so one burst on a short pulse can swing the stored
  rate a long way -- the logged 0.302 -> 0.665 is exactly one 0.066 s pulse's 0.08 gn
  blended at `PULSE_RATE_LEARN`. Persisting the window (the model already outlives the
  charge, so that is a one-line change) or bounding a single update are the candidates.
- **Endgame stalls:** a pulse sized for exactly one grain delivers nothing about half the
  time. Aiming the last pulses at `remainder - cutoff_weight` is safe and untested.
- Untested lever: `settle_min_time` (0.3 s of each ~0.8 s pulse cycle). Too low and a
  pulse is weighed before its powder lands.

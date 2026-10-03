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
.venv/bin/python -m unittest discover -t . -s tests      # from the repo root; 350 tests
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

`SimulatedMachine(tube=True)` switches to the **lumpy model** fitted to the bench record
of 2026-10-03: a `Tube` per motor whose lip loads while running and sheds grains in
events and clumps, a jolt on motor start that sheds a clump from a loaded lip, and a
landing tail. `tests/test_simulator.py` pins it to the record (zero rate, mean dose, burst
rate per regime) and is the simulator's contract with the bench -- if it fails, the model
has drifted from the machine, and nothing proved on it counts. `flicker=True` adds the
scale's idle ±1-division wander. The default model is the older grain-by-grain one, kept
so the charge tests keep their meaning until the endgame is redesigned on the lumpy one.

## Layout

- `trickler/main.py` -- the charge loop: continuous PID trickling, then `PulseFeeder` for
  the final approach. Most of the hard-won logic is here. `FeedModel` is what a powder has
  taught the machine, kept across charges; `run_pass` is one pass of the daemon's idle
  loop, where a Phase 2 calibration routine would sit beside `trickler_loop`.
- `trickler/calibrate.py` -- the calibration routine: a `Calibration` the idle loop steps
  (prime, stall search, sweep with container pauses, fit, recommend), started by the
  `calibrate` command and steered by `calibrate_continue` / `calibrate_abort`; status under
  the `CALIBRATION_STATUS` key. `/app/calibrate/` (`app.py`, `templates/calibrate.html`)
  starts it, polls the status, shows the cell table and the recommendation, and **Apply**
  writes the values (live overrides + the profile's section, or `[trickler]` with no
  name) and sends `calibrate_apply`, which seeds the profile's `FeedModel` from the
  calibration's per-speed rates at the speeds now in force. The screen shows an amber
  CALIBRATING band while it runs and a red EMPTY CUP / PAN MISSING one on a pause. `trickler/powder_model.py` -- a powder as the sweep measured
  it (`EmpiricalPowder`) and charges simulated against it through the real `PulseFeeder`
  on a virtual clock (`ChargeSimulator`, `recommend`).
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
  pulse gave 3.4x the powder, and only a long spin-up explains that. That fit was taken at
  a 0.3 s settle. With each pulse's tail credited to the pulse that fired it (0.6 s settle,
  2026-10-03), 30% measured 0.10-0.18 gn/s over a run and 45% 0.20-0.26 -- lower, and
  varying charge to charge with the state of the tube.
- **Dose is only weakly coupled to pulse length.** On 2026-10-03, pulses of 0.15, 0.25 and
  0.40 s all dropped anywhere from 0 to 4 grains: ~45% of pulses with 0.28 s of movement
  dropped nothing, ~20% dropped four or more. The state of the tube decides, not the
  length, so no length between the floor and the cap makes a final pulse safe.
- **The first pulse after the tube has been running is a burst** -- 2-10x the steady rate,
  because the tube is loaded. On a short pulse a burst implies a huge rate and drags the
  stored rate with it (seen: 0.302 -> 0.665 gn/s from one four-grain pulse). Part of that,
  at a 0.3 s settle, is the *previous* pulse's tail landing in the next pulse's window and
  being credited to it: the fine rate learned that way ran 0.66-1.25 gn/s against a real
  0.1-0.2. Harmless while fine pulses clamp to the 0.15 s minimum; wrong as a stored number.
- **The rate learned in the endgame collapses on a run of zero-dose pulses** (0.179 ->
  0.068 over 15 pulses, charge 03:11:10), which sizes the "one-grain" pulse at the 0.4 s
  cap, which then drops four grains at 0.06 remaining. The learning loop steered into the
  overshoot; it did not just wander.
- **An idle pan's *stable* reading flickers +/-1 division every few seconds.** A single
  pulse's dose therefore carries +/-0.02 of scale noise, and a rate from one short pulse is
  noise with a signal in it.
- **Charge time is set by where continuous trickling hands over**, at roughly 17 s per
  grain left for the pulse feeder. `rate_window = 12` (~0.5 s) keeps that handover steady;
  at 4 samples it measured the scale's 0.02 step instead of a rate.
- Current performance (2026-10-03, 30/45, settle 0.3): 4-15 pulses a charge, pulse phase
  3-12 s, 12-15 s from dump to complete. The completion reading is 54.98 by design, but
  the display afterwards read 55.00-55.04: in 0 of 4 charges did the reading match what
  landed, with tails of 1-3 divisions arriving after "complete". At a 0.6 s settle, 2 of
  3 matched. `charges.csv` records the completion reading, not what landed; only the
  display knows that.

## Decided, with the reason -- don't relitigate without new evidence

- **`cutoff_weight = 0.02`** -- stops a grain light on purpose. A light charge gets
  trickled up; a heavy one has to be dumped -- or, as the owner now does, the extra grain
  is lifted out by hand. Light-by-design stays the aim; when it conflicts with speed, speed
  wins (see `settle_min_time`).
- **`pulse_on_time = 0.4`** -- at 0.2 the cap bound nearly every pulse; past 0.4 the gain
  flattens and clumps start to overshoot.
- **Two-speed pulsing stays on, at `pulse_pwm = 30` / `pulse_fast_pwm = 45`** -- that is
  what the owner runs (the pulse record says so; earlier notes saying 30/30 or 25/45 were
  wrong). 45% drive gives 1.1-1.4x the powder per pulse of 30%, not the 2.5x drive
  suggests, but on 2026-10-03 one speed at 30/30 took 3-5 more pulses a charge and landed
  heavy just as often (1 in 3 either way), including a four-grain burst with no fast pulse
  before it. Fast pulses are not the burst source; the continuous phase loads the tube
  too. The shipped example still says 25/45; don't move shipped defaults without asking.
- **`settle_min_time = 0.3` -- the owner's call, for speed.** Tested 2026-10-03: at 0.3 the
  reading at "complete" matched what landed in 0 of 4 charges (tails of 1-3 divisions
  arrived afterwards); at 0.6 it matched in 2 of 3, and the fine-rate measurement became
  honest -- at +0.2-0.3 s a pulse, 1.5-3 s a charge. The owner prefers the seconds and
  lifts a heavy grain by hand. Consequence for code: doses recorded at 0.3 under-credit the
  pulse that fired them, so Phase 2 must not learn a rate from single short pulses, or must
  make the wait adaptive (short while the reading is still, long only while powder is
  landing) -- which is the way to have both.
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
  within minutes. `helpers.log_level` is the one place the level is decided now. The same
  `DEFAULTS[x] or config[...]` shape made `screen.py` ignore `[buttons]` and `[screen]`
  entirely (hidden because the shipped ini equals the defaults), and `scales.py`'s bench
  tool ignore the scale model. Never write `default or config[...]`; read the config and
  pass the default as `fallback=`.
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
  the calibration commands go. Every pulse is in `pulses.csv`; `/app/history` fits rate
  and spin-up from it, so bench evidence no longer has to be pasted from the journal.
  A profile's `learned.json` entry is shared: the model owns `rate` / `fast_rate` /
  `updated`, the routine owns `calibration`. `FeedModel._write` merges, and `reset` keeps
  the calibration block -- the first version replaced the entry whole, and the first
  pulse learned after a calibration erased its results.
- Reading pulse logs: `remainder: R ... scale: W ... pulsed T s -> D (rate X/s)` -- R is
  before the pulse, W after, and X is the **fine** rate only. Fast-pulse learning isn't
  logged; `pulses.csv` has both, with the rate at the speed each pulse used, and a
  `source` column (`charge` / `calibration`).
- `charges.csv` has `final` (the reading at "complete") **and `landed`** (the pan
  `landed_wait` seconds later, once the powder in the air came down). Judge heavy/light by
  `landed`; `helpers.charge_error` does. `PulseFeeder` and `settled_weight` take a `clock`
  (and the feeder a `sleep`) so a charge can be simulated on simulated time without
  patching the time module.
- At a 0.3 s settle, one pulse's powder is often credited to the next: `pulses.csv` and
  the learned rate then disagree with the scale display, and the display is right. Check
  the display after a charge before trusting a dose in the record.

## Phase 2: learning and adjusting on the fly

The next piece of work. The bench session of 2026-10-03 -- nine charges, every pulse in
`pulses.csv`, the display read after each charge -- changed its order.

- **Owner's priorities: speed and accuracy together; when they conflict, speed.** A heavy
  grain is lifted by hand. Don't trade seconds for a lower heavy rate without asking.
- **First target: the endgame, not calibration.** What decides light or heavy is not the
  feed rate but the *dose distribution* of a pulse -- how often zero, how often four
  grains -- and how it depends on what ran just before. The rate learned from one-grain
  pulses is noise (0.09 to 1.25 gn/s in one evening), and its collapse on a run of zeros
  sized the pulse that landed heavy. In order:
  1. Simulator grain model to match the record: ~45% of pulses with 0.28 s of movement
     drop nothing, ~20% drop four or more grains, and the odds depend on tube state (loaded
     by the continuous phase or by fast pulses). The current model's one clump chance and
     +/-25% per grain cannot produce 0, 0, 0, 0, 0.02 and then 0.08.
  2. Stop learning the rate from one-grain pulses: size the last pulses from the aimed
     pulses' rate, which sat at 0.13-0.26 all night, or persist the window so a single
     short pulse cannot move it (`FeedModel` outlives the charge; `new_charge` clears the
     window, so that is a one-line change). The logged 0.302 -> 0.665 is one 0.066 s
     pulse's 0.08 gn blended at `PULSE_RATE_LEARN`.
  3. Adaptive settle wait: short while the reading is still, long only while powder is
     still landing. That is how to have the speed of 0.3 and the honesty of 0.6.
  4. A stop rule that knows a pulse can drop four grains: at 0.06 remaining no pulse length
     is safe, so the choice is where to stop light and how often to accept heavy -- and
     the owner has said which way that goes.
  5. Then **per-powder calibration**, the owner's original idea: pick a powder, run a short
     routine, store its constants per profile. Rate and spin-up both come out of ~30 pulses
     at two lengths. Also worth capturing: dump weight and stall PWM. Powders differ in
     grain shape, length and weight, and the measure's drop volume changes per powder and
     calibre. The `TRICKLER_COMMAND` key is where "calibrate" goes.
- **Owner's constraints for any calibration routine:** empty the pan between runs (cups
  hold ~200 gn and powder bounces off the pile onto the scale); an empty tube takes about
  10 s of running to fill; a full-power run fills the tube while pulsing lays the grains
  out, so delivery starts slow and then settles.
- **Endgame stalls:** a pulse sized for one grain delivers nothing about half the time
  (seven zeros in fifteen pulses on 03:11:10). Aiming the last pulses at
  `remainder - cutoff_weight` is safe and untested.
- Every code idea above is tested in the simulator first, then one change per bench run,
  three charges, display noted after each.

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
.venv/bin/python -m unittest discover -t . -s tests      # from the repo root; 433 tests
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
  writes the values to the profile's section (or `[trickler]` with no name) and sends
  `calibrate_apply`, which seeds the profile's `FeedModel` from the
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
- **By the read, a pulse has landed almost nothing** (sweep of 2026-10-16, settle 0.3):
  45% / 0.40 s read 0.004 gn by the read and 0.074 in the second after; 30% / 0.40 s,
  0.004 and 0.022. On 2026-10-05 it was 0.025 against 0.048 at 45%. At this settle the
  feeder sizes each pulse with ~5% of the previous one on the scale. The evening sweep
  agreed: 45% / 0.40 s, 0.010 by the read and 0.078 after.
- **Performance at the owner's baseline** (2026-10-16 evening, three charges, nothing
  changed): final approach 5.4 / 5.2 / 5.1 s, 5 / 4 / 4 pulses, whole trickle 9.1 / 10.1 /
  8.7 s; landed 55.00 all three, display 55.02 / 55.00 / 55.00. The best on record. The
  learned fine rate (0.1136) did not move over the three: the endgame's pulses delivered
  too little inside the window to learn from, by design. In the pulse record, charges at
  trickle 0.5 hand over at 0.36-0.38 gn and the one at 0.8 at 0.62.

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

## The owner's baseline: Hodgdon_H1000 (confirmed 2026-10-16)

What the owner runs, set on the Pi and confirmed setting by setting. Bench changes start
from here, one at a time; calibrations are run at it. If the tuning page shows anything
else, ask -- don't assume it was meant. The 10-04 calibration's Apply had left trickle 0.8,
aim 0.7 and stall 15 in this profile, hidden behind live values for twelve days. Pulse
feeding starts at 0.5 is what the 10-03 pulse record handed over at (0.28-0.52 gn); aim
0.85 is the shipped value, and nothing records another before 10-04; stall 17 is the
10-16 sweep's measurement, inert at 30/45 (it only floors pulse drive and scales the first
fast-pulse guess after a restart). The starting feed rate only seeds a model that has
measured nothing.

| # | Tuning page | Key | Value |
|---|---|---|---|
| 1 | Coarse trickler off at | `fine_trickle_weight` | 1.5 |
| 2 | Pulse feeding starts at | `pulse_trickle_weight` | 0.5 |
| 3 | Longest pulse | `pulse_on_time` | 0.4 |
| 4 | Motor spin-up | `pulse_dead_time` | 0.12 |
| 5 | Shortest pulse | `pulse_min_on_time` | 0.15 |
| 6 | Pause after each pulse | `pulse_off_time` | 0.1 |
| 7 | Minimum settle wait | `settle_min_time` | 0.3 |
| 8 | Pulse speed | `pulse_pwm` | 30 |
| 9 | Fast pulse speed | `pulse_fast_pwm` | 45 |
| 10 | Fine pulses below | `pulse_fast_until` | 0.1 |
| 11 | Stall speed | `stall_pwm` | 17 |
| 12 | Starting feed rate | `pulse_rate` | 0.0625 |
| 13 | Pulse aim | `pulse_aim` | 0.85 |
| 14 | Settle timeout | `settle_timeout` | 1.0 |
| 15 | Stop short by | `cutoff_weight` | 0.02 |
| 16 | Confirm what landed | `landed_wait` | 2.0 |
| 17 | Feed rate samples | `rate_window` | 12 |
| 18 | Look ahead | `lookahead_time` | 0.35 |
| 19 | Measure jammed under | `stall_drop_weight` | 0.5 |
| 20 | Dump attempts | `max_dump_attempts` | 3 |
| 21 | Pause between attempts | `dump_retry_pause` | 1.0 |

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
  pulse learned after a calibration erased its results. The other way round, the
  routine writes only its block: until 2026-10-16 it also wrote its fitted rates, in the
  file only, so a calibration nobody applied changed the stored rate behind the model.
  The daemon loads the active profile's model on the idle pass, not only at startup and
  at a charge; otherwise the page says "not learned yet" after Use or a reboot.
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
- **The first calibration recommended settings that could not finish a charge**
  (2026-10-04): 25% / 0.15 s, which is 0.03 s of movement after spin-up -- not strong
  enough for long enough to move powder out of the tube (the owner's reading; the tube is
  never empty during a session, so don't reach for "depleted tube" to explain a dead
  pulse). Two causes, both fixed. The sweep measured the landing tail on every third
  pulse only and the fit ignored tails altogether, while on this machine the tail is as
  large as the dose (0.048 vs 0.025 gn at 45%): every speed read at half its delivery and
  every candidate simulated slow. Now every pulse gets its tail measured (a second each)
  and the fits count dose + tail. And the recommender, with nothing meeting the heavy
  limit, fell back to "least heavy", which is the setting that never delivers: its own
  prediction said 99.7% of charges would not finish, and the page did not show that
  number. Now a non-finisher is never ranked first, the page shows "Did not finish", and
  Apply is offered only for a recommendation that finished every simulated charge within
  the limit. The check that matters is on the page: the simulator's prediction for the
  *current* settings beside what the history page measured. If they disagree by much,
  the model is not this machine yet, whatever it recommends.
- **The second calibration (2026-10-05) was the first whose model fitted the machine:**
  predicted 12.0 s / 6.7 pulses / 68% heavy for 30/45, against 10.9 s / 7.0 pulses /
  3 of 5 heavy measured. Its recommendation (25/45, cap 0.15, trickle 0.3, 13.7 s, 13%
  heavy) was *slower*, because 30/45 misses the 25% heavy limit -- a trade the owner
  decides, not the recommender (the owner's rule: speed wins). It also carried a fitted spin-up of
  0.00: the two-point fit lands anywhere when dose barely grows with length. A fit outside
  `CREDIBLE_DEAD_TIME` (0.05-0.25 s) is now set aside and the configured spin-up kept, for
  the rates as well as the result, and the page says so. And since bench runs change one
  thing at a time, the recommendation also lists `single_changes` -- the current settings
  with one value moved, simulated the same way -- as "Change one thing" on the page.
- **Profile recall did not work until 2026-10-16**, three ways. The trickler loaded the
  ini once at startup, so a profile saved from the page reached it only after a restart;
  now `helpers.ConfigFile` re-reads it whenever it changes (one stat per pass;
  `[memcache_vars]` still needs a restart). Live overrides (`TRICKLER_SETTINGS`) won over
  the profile and selecting a profile never cleared them, so switching left the settings
  where they were; now switching to a *different* profile drops them, and Apply (tuning
  page and calibration page) no longer sets them at all -- it writes the file and drops
  any left over; live values are only the fallback when the file cannot be written.
  Overrides set by every Apply had hidden, for the twelve days since, that Hodgdon_H1000
  still held the 10-04 calibration's trickle weight 0.8, aim 0.7 and stall 15. The tuning page showed and wrote `[trickler]`, which
  a saved profile hides completely, so tuning with a profile selected vanished at the next
  reboot; now the page layers overrides, profile, `[trickler]`, defaults like
  `main.trickler_settings`, and Apply saves to the selected profile. The rule: **the page
  edits the selected profile; the trickler follows the file.** Profiles can be created
  (new-name field) and deleted from the tuning page; delete removes the section
  (`helpers.remove_ini_section`; `config.read` never drops a section, so the app also
  calls `remove_section`) and sends `delete_profile`, which drops the whole
  `learned.json` entry. Charge and pulse history stay.
- **The browser blocked Apply on values the app wrote itself** (bench, 2026-10-16).
  Profile Save stores the learned rate (0.0625) as `pulse_rate`, a field stepped 0.01,
  and the calibration page's Apply form carries the fitted rate and spin-up to four
  decimals. Chrome's step check refused the form, so Apply did nothing, with no message
  from the server. Both settings forms are `novalidate` now: `clean_trickler_settings`
  is the only check (it clamps and says so), and `step` only drives the arrows. Don't
  put browser validation back on a form the app fills in itself.
- **The third calibration's fit check (2026-10-16) failed for reasons outside the model.**
  Predicted 14.7 s / 43% heavy / 10.9 pulses for the current settings against 9.2 s /
  17% / 8.0 measured. The "measured" side was every completed charge for the profile --
  six over twelve days, at settings nobody can now reconstruct, since charges did not
  record them and the profile's own values were hidden for part of that time -- and its
  seconds were the whole trickle, while the prediction's were the final
  approach plus a continuous phase guessed at twice the 45% primed rate (real trickling
  runs much faster). Offline on the same rows, trickle 0.5 predicts 7.6 pulses, close to
  the old charges' 8.0. Now each charge records the final-approach settings and
  `pulse_seconds`, the simulator reports `pulse_seconds` apart, and the check uses only
  charges thrown at exactly the settings the calibration simulated as current (and its
  settle), final approach to final approach. Old rows never match; three new charges and
  a reload give the check without recalibrating.
- **The fourth calibration (2026-10-16 evening, at the baseline) gave the first fit check
  like for like, and the model is still pessimistic:** predicted 6.6 s / 6.5 pulses / 56%
  heavy for the final approach, against 5.2 s / 4.3 pulses / 0 of 3 heavy by `landed`
  (1 of 3 by the display) over three charges. Three charges cannot judge a heavy rate,
  but the pulse count is half again too high. Offline on the same 90 rows: seeding the
  simulation with the learned rates instead of the fit changes nothing (6.4 pulses); it
  reaches ~4.5 only with trickle 0.3. The charges' own pulse rows answered part of it.
  They handed over at 0.34 / 0.36 / 0.38 gn (and 0.36-0.38 on 10-04/05), where the
  simulator draws `handover = trickle x U(0.6, 1.05)`, 0.30-0.525 for 0.5, from the
  10-03 record; with the bench's 0.34-0.38 it predicts 5.7 pulses / 5.8 s instead of
  6.5 / 6.6 -- about 40% of the gap. Their first pulses did *not* drop more than the
  sweep: 45% / 0.40 s averaged 0.089 gn a pulse in the charges (the previous pulse's tail
  credited, as always at 0.3) against the sweep's 0.088 total. The rest of the gap is at
  the end: each real charge finished with one short fast pulse at ~0.12 remaining and at
  most one fine pulse at 0.04 (two of two dropped a grain), where the simulation often
  fires two to four fine pulses because the 30% cells hold 30-60% zeros. Three charges
  cannot settle that, or the heavy rate; a full session at the baseline can, and its
  first remainders give the handover distribution to simulate from. Until the model
  matches, its recommendation (here cap 0.25, trickle 0.3, aim 0.7: same seconds,
  predicted heavy 23%) is not worth trying, and the owner keeps the baseline; nothing in
  "Change one thing" was faster by more than noise.
- **A run of empty pulses never ends a charge -- the owner's call (2026-10-05).** Eight
  zero-dose pulses in a row used to end it `empty`, and the idle loop then started a new
  charge on the same light pan: thirty in five minutes on 2026-10-04, each a fresh
  history row with the learned window emptied and the first pulse a probe again. A
  stand-down (auto mode off, like a jam) was tried and dropped: the next pulse often
  delivers, and the owner would rather it kept going. Now the charge keeps pulsing,
  `EMPTY_PULSE_WARNING` puts one line in the log per run of eight, and the only stop is
  `MAX_PULSE_PHASE_SECONDS` (120 s, outcome `timeout`), which is where an empty hopper
  ends up. Old `charges.csv` rows can still say `empty`.

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

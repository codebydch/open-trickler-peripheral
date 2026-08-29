#!/usr/bin/env python3
"""
Copyright (c) Ammolytics and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

OpenTrickler
https://github.com/ammolytics/projects/tree/develop/trickler

OpenTrickler forked and updated here:
https://github.com/codebydch/open-trickler-peripheral
"""
import collections
import datetime
import decimal
import enum
import logging
import time

import helpers
import PID
import motors
import scales


# Components:
# 0. Server (Pi)
# 1. Scale (serial)
# 2. Trickler (gpio/PWM)
# 3. Dump (gpio/servo)
# 4. API
# 6. Bluetooth?
# 7: Powder pan/cup?


# Grains per gram, used to convert the grain-based trickler thresholds in the
# config file when the scale is set to grams.
GRAINS_PER_GRAM = decimal.Decimal('15.4323583529')


# Fallbacks for the [trickler] section, so an existing config file written before
# that section was added still runs instead of crashing on startup.
DEFAULT_TRICKLER_SETTINGS = {
    'fine_trickle_weight': '2.0',
    'pulse_trickle_weight': '0.15',
    'pulse_on_time': '0.03',
    'pulse_off_time': '0.15',
    'pulse_pwm': '32',
    'cutoff_weight': '0.0',
    'rate_window': '4',
    'lookahead_time': '0.35',
}


TricklerSettings = collections.namedtuple('TricklerSettings', (
    'fine_trickle_weight',
    'pulse_trickle_weight',
    'pulse_on_time',
    'pulse_off_time',
    'pulse_pwm',
    'cutoff_weight',
    'rate_window',
    'lookahead_time',
))


class FeedRateEstimator:
    """Estimates how fast powder is landing in the pan, in target units per second.

    Scale readings lag behind reality: powder is still in the air, and the scale needs
    time to settle before it reports what has landed. Knowing the current feed rate
    lets the loop work out how much powder is already on its way and stop the motors
    before the reading reaches the target, instead of after.
    """

    def __init__(self, window):
        """Constructor. Keeps the last `window` readings to average the rate over."""
        self._samples = collections.deque(maxlen=max(2, window))

    def add(self, weight, timestamp=None):
        """Records a scale reading."""
        self._samples.append((timestamp if timestamp is not None else time.time(), weight))

    def rate(self):
        """Returns the weight gained per second over the sample window, never negative."""
        if len(self._samples) < 2:
            return decimal.Decimal('0')
        old_time, old_weight = self._samples[0]
        new_time, new_weight = self._samples[-1]
        elapsed = decimal.Decimal(str(new_time - old_time))
        if elapsed <= 0:
            return decimal.Decimal('0')
        return max(decimal.Decimal('0'), (new_weight - old_weight) / elapsed)


def trickler_settings(config, scale, target_unit):
    """Reads the trickler thresholds from the config file, converted to the target unit.

    The weights in the config file are given in grains, since that's the unit they were
    tuned in, so they need converting when the scale is set to grams.
    """
    # Fall back to the defaults above for anything the config file doesn't set.
    configured = config['trickler'] if config.has_section('trickler') else {}
    section = collections.ChainMap(configured, DEFAULT_TRICKLER_SETTINGS)
    factor = decimal.Decimal('1')
    if target_unit == scale.Units.GRAMS:
        factor = decimal.Decimal('1') / GRAINS_PER_GRAM
    return TricklerSettings(
        fine_trickle_weight=decimal.Decimal(section['fine_trickle_weight']) * factor,
        pulse_trickle_weight=decimal.Decimal(section['pulse_trickle_weight']) * factor,
        pulse_on_time=float(section['pulse_on_time']),
        pulse_off_time=float(section['pulse_off_time']),
        pulse_pwm=float(section['pulse_pwm']),
        cutoff_weight=decimal.Decimal(section['cutoff_weight']) * factor,
        rate_window=int(section['rate_window']),
        lookahead_time=decimal.Decimal(section['lookahead_time']))


def pulse_trickler(motor, settings):
    """Runs a trickler motor for a single short pulse, then pauses.

    The motor can't be driven below its minimum PWM without stalling, so the only way
    to feed less powder per scale reading is to run it for less time. The pause after
    the pulse gives the powder time to land and the scale time to report it, so the
    next reading reflects what was just dispensed rather than lagging behind it.
    """
    motor.set_speed(settings.pulse_pwm / 100)
    time.sleep(settings.pulse_on_time)
    motor.off()
    time.sleep(settings.pulse_off_time)


def trickler_loop(config, memcache, constants, pid, trickler_motor1, trickler_motor2, scale, target_weight, target_unit, pidtune_logger): # pylint: disable=too-many-arguments,too-many-branches;
    """Main trickler control loop run when all devices are ready, target weight is set, and auto-mode is on."""
    settings = trickler_settings(config, scale, target_unit)
    logging.debug('trickler settings: %r', settings)
    feed_rate = FeedRateEstimator(settings.rate_window)
    pidtune_logger.info('timestamp, input (motor %), output (weight %)')
    logging.info('Starting trickling process...')

    # Note(eric): All `break` calls will exit the loop and this function.
    # The `finally` block below stops both motors on every exit path.
    try:
        while 1:
            # Stop running if auto mode is disabled.
            if not memcache.get(constants.AUTO_MODE.value):
                logging.debug('auto mode disabled.')
                break

            # Read scale values (weight/unit/stable)
            scale.update()

            # Stop running if scale's unit no longer matches target unit.
            if scale.unit != target_unit:
                logging.debug('Target unit does not match scale unit.')
                break

            # Stop running if pan removed.
            if scale.weight < 0:
                logging.debug('Pan removed.')
                break

            feed_rate.add(scale.weight)
            remainder_weight = target_weight - scale.weight
            # Powder that is already in the air or that the scale hasn't caught up with
            # yet. Every decision below is made against what the charge is about to
            # weigh, not what the scale is reporting right now.
            in_flight_weight = feed_rate.rate() * settings.lookahead_time
            projected_remainder = remainder_weight - in_flight_weight
            logging.debug(
                'remainder_weight: %r, in_flight_weight: %r, projected_remainder: %r',
                remainder_weight,
                in_flight_weight,
                projected_remainder)

            pidtune_logger.info(
                '%s, %s, %s',
                datetime.datetime.now().timestamp(),
                trickler_motor1.speed,
                scale.weight / target_weight)

            # Trickling complete. Stop both motors here, before anything else in this
            # iteration can command them again, so no more powder goes into a pan that
            # has already reached the target weight.
            if remainder_weight <= settings.cutoff_weight:
                trickler_motor1.off()
                trickler_motor2.off()
                logging.debug('Trickling complete, motors turned off and PID reset.')
                break

            # Enough powder is already on its way to finish the charge, even though the
            # scale hasn't reported it yet. Stop feeding and let the reading catch up
            # instead of piling more on top of it. If the charge lands short, the next
            # pass picks the trickling back up.
            if projected_remainder <= settings.cutoff_weight:
                trickler_motor1.off()
                trickler_motor2.off()
                logging.debug('Projected weight has reached target, waiting for the scale.')
                time.sleep(settings.pulse_off_time)
                continue

            # Final approach. Running a motor continuously this close to the target
            # overshoots it, so feed in short pulses and re-read the scale after each
            # one.
            if projected_remainder <= settings.pulse_trickle_weight:
                trickler_motor2.off()
                pulse_trickler(trickler_motor1, settings)
                logging.info(
                    'remainder: %s %s scale: %s %s pulsing motor1 for %ss',
                    remainder_weight,
                    target_unit,
                    scale.weight,
                    scale.unit,
                    settings.pulse_on_time)
                continue

            # PID controller requires float value instead of decimal.Decimal
            pid.update(float(scale.weight / target_weight) * 100)
            trickler_motor1.update(pid.output)
            # The second trickler only runs while there's still a meaningful amount of
            # powder left to throw. It feeds too fast to be used near the target.
            if projected_remainder <= settings.fine_trickle_weight:
                trickler_motor2.off()
            else:
                trickler_motor2.update(pid.output)
            logging.debug('trickler_motor1.speed: %r, trickler_motor2.speed: %r, pid.output: %r', trickler_motor1.speed, trickler_motor2.speed, pid.output)
            logging.info(
                'remainder: %s %s scale: %s %s motor1: %s motor2: %s',
                remainder_weight,
                target_unit,
                scale.weight,
                scale.unit,
                trickler_motor1.speed,
                trickler_motor2.speed)
    finally:
        # Clean up tasks.
        trickler_motor1.off()
        trickler_motor2.off()
        # Clear PID values.
        pid.clear()
    logging.info('Trickling process stopped.')


def main(config, memcache, args, pidtune_logger):
    """Main trickler function. This runs everything."""
    constants = enum.Enum('memcache_vars', config['memcache_vars'])

    # Set up the PID controller.
    pid = PID.PID(
        float(config['PID']['Kp']),
        float(config['PID']['Ki']),
        float(config['PID']['Kd']))
    logging.debug('pid: %r', pid)

    # Set up the trickler motor controller.
    trickler_motor1 = motors.TricklerMotor(1, config, memcache=memcache)
    logging.debug('trickler_motor1: %r', trickler_motor1)
    trickler_motor2 = motors.TricklerMotor(2, config, memcache=memcache)
    logging.debug('trickler_motor2: %r', trickler_motor2)
    servo_motor = motors.ServoMotor(config, memcache=memcache)
    logging.debug('servo_motor: %r', servo_motor)

    # Set up the scale controller.
    scale_cls = scales.SCALES[config['scale']['model']]
    # Wait until the scale is ready.
    while 1:
        try:
            scale = scale_cls(config, memcache=memcache)
        except scales.ScaleNotReady:
            logging.info('Scale not ready, trying again...')
            time.sleep(10)
        else:
            logging.debug('scale: %r', scale)
            break

    # Set initial values in memcache.
    memcache.set_multi({
        constants.AUTO_MODE.value: args.auto_mode or False,
        constants.TARGET_WEIGHT.value: args.target_weight or decimal.Decimal('0.0'),
        constants.TARGET_UNIT.value: scale.unit_map.get(args.target_unit, 'GN'),
    })

    # Outer-most control loop for the whole trickler system.
    while 1:
        # Update settings from memcache.
        auto_mode = memcache.get(constants.AUTO_MODE.value)
        target_weight = memcache.get(constants.TARGET_WEIGHT.value)
        target_unit = memcache.get(constants.TARGET_UNIT.value)
        # Use percentages for PID control to avoid complexity w/ different units of weight.
        pid.SetPoint = 100.0
        scale.update()

        # Set scale to match target unit.
        if target_unit != scale.unit:
            logging.info('scale.unit: %r, target_unit: %r', scale.unit, target_unit)
            scale.change_unit()

        logging.info(
            'target: %s %s scale: %s %s auto_mode: %s',
            target_weight,
            target_unit,
            scale.weight,
            scale.unit,
            auto_mode)

        # Powder pan in place, scale stable, ready to trickle.
        if (scale.weight >= 0 and
                scale.weight < target_weight and
                scale.unit == target_unit and
                scale.is_stable and
                auto_mode):
            # Stops the servo from dumping powder twice if the scale weight dips below the target weight
            if ((target_weight - scale.weight) / target_weight) >= 0.5:
                # Wait a second to dump powder and start trickling.
                time.sleep(1)
                logging.info('Starting powder dump...')
                servo_motor.run_servo()
                time.sleep(1.5)
                servo_motor.set_initial_angle()
                # Required since the larger powder drop hitting the cup may overshoot the weight until settling
                time.sleep(1)
                logging.info('Completed powder dump.')
            # Run trickler loop.
            trickler_loop(config, memcache, constants, pid, trickler_motor1, trickler_motor2, scale, target_weight, target_unit, pidtune_logger)


if __name__ == '__main__':
    import argparse
    import configparser

    # Default argument values.
    DEFAULTS = dict(
        verbose = False,
    )

    parser = argparse.ArgumentParser(description='Run OpenTrickler.')
    parser.add_argument('config_file')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--auto_mode', action='store_true')
    parser.add_argument('--pid_tune', action='store_true')
    parser.add_argument('--target_weight', type=decimal.Decimal, default=0)
    parser.add_argument('--target_unit', choices=('g', 'GN'), default='GN')
    args = parser.parse_args()

    config = configparser.ConfigParser()
    config.optionxform = str
    if args.config_file:
        config.read(args.config_file)

    # Order of priority is 1) command-line argument, 2) config file, 3) default.
    VERBOSE = DEFAULTS['verbose'] or config['general']['verbose']
    if args.verbose is not None:
        VERBOSE = args.verbose

    # Configure Python logging.
    LOG_LEVEL = logging.INFO
    if VERBOSE:
        LOG_LEVEL = logging.DEBUG
    helpers.setup_logging(LOG_LEVEL)

    # Setup memcache.
    memcache_client = helpers.get_mc_client()

    # Set up a separate logger for PID tuning with it's own format.
    pidtune_logger = logging.getLogger('pid_tune')
    pid_handler = logging.StreamHandler()
    pid_handler.setFormatter(logging.Formatter('%(message)s'))

    # Configure the log level based on if the tuner feature should be active.
    pidtune_logger.setLevel(logging.ERROR)
    if args.pid_tune or config['PID'].getboolean('pid_tuner_mode'):
        pidtune_logger.setLevel(logging.INFO)

    # Run the main trickler program.
    main(config, memcache_client, args, pidtune_logger)

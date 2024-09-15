"""
Copyright (c) codebydch and contributors. All rights reserved.
Released under the MIT license. See LICENSE file in the project root for details.

https://github.com/codebydch/open-trickler-peripheral
"""
import logging
import helpers
import argparse
import configparser

from flask import Flask, render_template, request, redirect, url_for
from pymemcache.client import base
from decimal import Decimal, InvalidOperation

# Default argument values.
DEFAULTS = dict(
    verbose = False,
)

parser = argparse.ArgumentParser(description='Run OpenTrickler Flask App.')
parser.add_argument('--target_weight', type=Decimal, default=0.0)
parser.add_argument('config_file')
parser.add_argument('--verbose', action='store_true')
parser.add_argument('--auto_mode', action='store_true')
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
    
logging.info('Starting OpenTrickler Flask App daemon...')
target_weight = Decimal('0.0')
if args.target_weight is not None:
    target_weight = args.target_weight
 
logging.info('Target Weight is set as %s', target_weight)
auto_mode = False
if args.auto_mode is not None:
    auto_mode = args.auto_mode   

logging.info('Auto Mode is set as %s', auto_mode)

app = Flask(__name__)
memcache_client = helpers.get_mc_client()

def get_memcache_value(key, default):
    value = memcache_client.get(key)
    if value is None:
        return default
    else:
        return value

def set_memcache_value(key, value):
    logging.info('Changing %s to %s', key, value)
    memcache_client.set(key, value)

@app.route('/app/')
def index():
    target_weight = get_memcache_value('target_weight', Decimal('0.00'))
    auto_mode = get_memcache_value('auto_mode', False)
    return render_template('index.html', target_weight=target_weight, auto_mode=auto_mode)

@app.route('/app/update', methods=['POST'])
def update():
    if 'set_weight' in request.form:
        weight_str = request.form['target_weight']
        try:
            target_weight = Decimal(weight_str).quantize(Decimal('0.01'))
            set_memcache_value('target_weight', target_weight)
        except InvalidOperation:
            pass  # Handle invalid input gracefully
    elif 'toggle' in request.form:
        auto_mode = not get_memcache_value('auto_mode', False)
        set_memcache_value('auto_mode', auto_mode)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import _thread
from datetime import datetime
from tzlocal import get_localzone
import threading
import socket
import os
import subprocess
import uuid
import ssl
import sys
import re
import json
import os.path
import argparse
from time import time, sleep, localtime, strftime
from collections import OrderedDict
from colorama import init as colorama_init
from colorama import Fore, Back, Style
from configparser import ConfigParser
from unidecode import unidecode
import paho.mqtt.client as mqtt
from signal import signal, SIGPIPE, SIG_DFL
signal(SIGPIPE,SIG_DFL)

script_version = "1.2.2"
script_name = 'ISP-Omega2-mqtt-daemon.py'
script_info = '{} v{}'.format(script_name, script_version)
project_name = 'Omega2 Reporter MQTT2HA Daemon'
project_url = 'https://github.com/ironsheep/Omega2-Reporter-MQTT2HA-Daemon'

# we'll use this throughout
local_tz = get_localzone()

# TODO:
#  - add announcement of free-space and temperatore endpoints

if False:
    # will be caught by python 2.7 to be illegal syntax
    print_line('Sorry, this script requires a python3 runtime environment.', file=sys.stderr)

# Argparse
opt_debug = False
opt_verbose = False

# Logging function
def print_line(text, error=False, warning=False, info=False, verbose=False, debug=False, console=True, sd_notify=False, log=False):
    timestamp = strftime('%Y-%m-%d %H:%M:%S', localtime())
    if console:
        if error:
            print(Fore.RED + Style.BRIGHT + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL, file=sys.stderr)
        elif warning:
            print(Fore.YELLOW + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL)
        elif info or verbose:
            if opt_verbose:
                print(Fore.GREEN + '[{}] '.format(timestamp) + Fore.YELLOW  + '- ' + '{}'.format(text) + Style.RESET_ALL)
        elif log:
            if opt_debug:
                print(Fore.MAGENTA + '[{}] '.format(timestamp) + '- (DBG): ' + '{}'.format(text) + Style.RESET_ALL)
        elif debug:
            if opt_debug:
                print(Fore.CYAN + '[{}] '.format(timestamp) + '- (DBG): ' + '{}'.format(text) + Style.RESET_ALL)
        else:
            print(Fore.GREEN + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL)

# Identifier cleanup
def clean_identifier(name):
    clean = name.strip()
    for this, that in [[' ', '-'], ['ä', 'ae'], ['Ä', 'Ae'], ['ö', 'oe'], ['Ö', 'Oe'], ['ü', 'ue'], ['Ü', 'Ue'], ['ß', 'ss']]:
        clean = clean.replace(this, that)
    clean = unidecode(clean)
    return clean

# Argparse            
parser = argparse.ArgumentParser(description=project_name, epilog='For further details see: ' + project_url)
parser.add_argument("-v", "--verbose", help="increase output verbosity", action="store_true")
parser.add_argument("-d", "--debug", help="show debug output", action="store_true")
parser.add_argument("-s", "--stall", help="TEST: report only the first time", action="store_true")
parser.add_argument("-c", '--config_dir', help='set directory where config.ini is located', default=sys.path[0])
parse_args = parser.parse_args()

config_dir = parse_args.config_dir
opt_debug = parse_args.debug
opt_verbose = parse_args.verbose
opt_stall = parse_args.stall

print_line(script_info, info=True)
if opt_verbose:
    print_line('Verbose enabled', info=True)
if opt_debug:
    print_line('Debug enabled', debug=True)
if opt_stall:
    print_line('TEST: Stall (no-re-reporting) enabled', debug=True)

# Eclipse Paho callbacks - http://www.eclipse.org/paho/clients/python/docs/#callbacks
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print_line('* MQTT connection established', console=True, sd_notify=True)
        print_line('')  # blank line?!
        #_thread.start_new_thread(afterMQTTConnect, ())
    else:
        print_line('! Connection error with result code {} - {}'.format(str(rc), mqtt.connack_string(rc)), error=True)
        #kill main thread
        os._exit(1)

def on_publish(client, userdata, mid):
    #print_line('* Data successfully published.')
    pass

def on_log(client, userdata, level, buf):
    #print_line('* Data successfully published.')
    print_line("log: {}".format(buf), debug=True, log=True)

# Load configuration file
config = ConfigParser(delimiters=('=', ), inline_comment_prefixes=('#'))
config.optionxform = str
try:
    with open(os.path.join(config_dir, 'config.ini')) as config_file:
        config.read_file(config_file)
except IOError:
    print_line('No configuration file "config.ini"', error=True, sd_notify=True)
    sys.exit(1)

daemon_enabled = config['Daemon'].getboolean('enabled', True)

# default domain when hostname -f doesn't return it
#default_domain = home
default_domain = ''
fallback_domain = config['Daemon'].get('fallback_domain', default_domain).lower()

# This script uses a flag file containing a date/timestamp of when the system was last updated
default_update_flag_filespec = '/home/pi/bin/lastupd.date'
update_flag_filespec = config['Daemon'].get('update_flag_filespec', default_update_flag_filespec)

default_base_topic = 'home/nodes'
default_sensor_name = 'dvc-reporter'

base_topic = config['MQTT'].get('base_topic', default_base_topic).lower()
sensor_name = config['MQTT'].get('sensor_name', default_sensor_name).lower()

# report our IoT values every 5min 
min_interval_in_minutes = 2
max_interval_in_minutes = 30
default_interval_in_minutes = 5
interval_in_minutes = config['Daemon'].getint('interval_in_minutes', default_interval_in_minutes)

# Check configuration
#
if (interval_in_minutes < min_interval_in_minutes) or (interval_in_minutes > max_interval_in_minutes):
    print_line('ERROR: Invalid "interval_in_minutes" found in configuration file: "config.ini"! Must be [{}-{}] Fix and try again... Aborting'.format(min_interval_in_minutes, max_interval_in_minutes), error=True, sd_notify=True)
    sys.exit(1)    

### Ensure required values within sections of our config are present
if not config['MQTT']:
    print_line('ERROR: No MQTT settings found in configuration file "config.ini"! Fix and try again... Aborting', error=True, sd_notify=True)
    sys.exit(1)

print_line('Configuration accepted', console=False, sd_notify=True)

# -----------------------------------------------------------------------------
#  IoT variables monitored 
# -----------------------------------------------------------------------------

dvc_model_raw = ''
dvc_model = ''
dvc_connections = ''
dvc_hostname = ''
dvc_fqdn = ''
dvc_linux_release = ''
dvc_linux_version = ''
dvc_uptime_raw = ''
dvc_uptime = ''
dvc_last_update_date = datetime.min
dvc_filesystem_space_raw = ''
dvc_filesystem_space = ''
dvc_filesystem_percent = ''
dvc_system_temp = ''
dvc_mqtt_script = script_info
dvc_mac_raw = ''
dvc_interfaces = []

# -----------------------------------------------------------------------------
#  monitor variable fetch routines
#
def getDeviceModel():
    global dvc_model
    global dvc_model_raw
    global dvc_connections
    out = subprocess.Popen("/bin/grep sysfs /etc/config/system | /usr/bin/awk '{ print $3 }' | /usr/bin/cut -f1 -d:", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_model_raw = stdout.decode('utf-8').replace("'",'').rstrip()
    # now reduce string length (just more compact, same info)
    dvc_model = dvc_model_raw.replace('p', '+')

    # now decode interfaces
    dvc_connections = 'w' # default

    print_line('dvc_model_raw=[{}]'.format(dvc_model_raw), debug=True)
    print_line('dvc_model=[{}]'.format(dvc_model), debug=True)
    print_line('dvc_connections=[{}]'.format(dvc_connections), debug=True)

def getLinuxRelease():
    global dvc_linux_release
    dvc_linux_release = 'openWrt'
    print_line('dvc_linux_release=[{}]'.format(dvc_linux_release), debug=True)

def getLinuxVersion():
    global dvc_linux_version
    out = subprocess.Popen("/bin/uname -r", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_linux_version = stdout.decode('utf-8').rstrip()
    print_line('dvc_linux_version=[{}]'.format(dvc_linux_version), debug=True)
    
def getHostnames():
    global dvc_hostname
    global dvc_fqdn
    #  BUG?! our Omega2 doesn't know our domain name so we append it
    out = subprocess.Popen("/bin/cat /etc/config/system | /bin/grep host | /usr/bin/awk '{ print $3 }'", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_hostname = stdout.decode('utf-8').rstrip().replace("'", '')
    print_line('dvc_hostname=[{}]'.format(dvc_hostname), debug=True)
    if len(fallback_domain) > 0:
        dvc_fqdn = '{}.{}'.format(dvc_hostname, fallback_domain)
    else:
        dvc_fqdn = dvc_hostname
    print_line('dvc_fqdn=[{}]'.format(dvc_fqdn), debug=True)

def getUptime():    # RERUN in loop
    global dvc_uptime_raw
    global dvc_uptime
    out = subprocess.Popen("/usr/bin/uptime", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_uptime_raw = stdout.decode('utf-8').rstrip().lstrip()
    print_line('dvc_uptime_raw=[{}]'.format(dvc_uptime_raw), debug=True)
    basicParts = dvc_uptime_raw.split()
    timeStamp = basicParts[0]
    lineParts = dvc_uptime_raw.split(',')
    if('user' in lineParts[1]):
        dvc_uptime_raw = lineParts[0]
    else:
        dvc_uptime_raw = '{}, {}'.format(lineParts[0], lineParts[1])
    dvc_uptime = dvc_uptime_raw.replace(timeStamp, '').lstrip().replace('up ', '')
    print_line('dvc_uptime=[{}]'.format(dvc_uptime), debug=True)

def getNetworkIFs():    # RERUN in loop
    global dvc_interfaces
    global dvc_mac_raw
    out = subprocess.Popen('/sbin/ifconfig | egrep "Link|flags|inet|ether" | egrep -v -i "lo:|loopback|inet6|\:\:1|127\.0\.0\.1"', 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    lines = stdout.decode('utf-8').split("\n")
    trimmedLines = []
    for currLine in lines:
        trimmedLine = currLine.lstrip().rstrip()
        trimmedLines.append(trimmedLine)

    #print_line('trimmedLines=[{}]'.format(trimmedLines), debug=True)
    #
    # OLDER SYSTEMS
    #  eth0      Link encap:Ethernet  HWaddr b8:27:eb:c8:81:f2  
    #    inet addr:192.168.100.41  Bcast:192.168.100.255  Mask:255.255.255.0
    #  wlan0     Link encap:Ethernet  HWaddr 00:0f:60:03:e6:dd  
    # NEWER SYSTEMS
    #  The following means eth0 (wired is NOT connected, and WiFi is connected)
    #  eth0: flags=4099<UP,BROADCAST,MULTICAST>  mtu 1500
    #    ether b8:27:eb:1a:f3:bc  txqueuelen 1000  (Ethernet)
    #  wlan0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500
    #    inet 192.168.100.189  netmask 255.255.255.0  broadcast 192.168.100.255
    #    ether b8:27:eb:4f:a6:e9  txqueuelen 1000  (Ethernet)
    #
    tmpInterfaces = []
    haveIF = False
    imterfc = ''
    for currLine in trimmedLines:
        lineParts = currLine.split()
        #print_line('- currLine=[{}]'.format(currLine), debug=True)
        #print_line('- lineParts=[{}]'.format(lineParts), debug=True)
        if len(lineParts) > 0:
            if 'flags' in currLine:  # NEWER ONLY
                haveIF = True
                imterfc = lineParts[0].replace(':', '')
                print_line('newIF=[{}]'.format(imterfc), debug=True)
            elif 'Link' in currLine:  # OLDER ONLY
                haveIF = True
                imterfc = lineParts[0].replace(':', '')
                newTuple = (imterfc, 'mac', lineParts[4])
                if dvc_mac_raw == '':
                    dvc_mac_raw = lineParts[4]
                #print_line('newIF=[{}]'.format(imterfc), debug=True)
                tmpInterfaces.append(newTuple)
                #print_line('newTuple=[{}]'.format(newTuple), debug=True)
            elif haveIF == True:
                print_line('IF=[{}], lineParts=[{}]'.format(imterfc, lineParts), debug=True)
                if 'ether' in currLine: # NEWER ONLY
                    newTuple = (imterfc, 'mac', lineParts[1])
                    tmpInterfaces.append(newTuple)
                    #print_line('newTuple=[{}]'.format(newTuple), debug=True)
                elif 'inet' in currLine:  # OLDER & NEWER
                    newTuple = (imterfc, 'IP', lineParts[1].replace('addr:',''))
                    tmpInterfaces.append(newTuple)
                    #print_line('newTuple=[{}]'.format(newTuple), debug=True)

    dvc_interfaces = tmpInterfaces
    print_line('dvc_interfaces=[{}]'.format(dvc_interfaces), debug=True)

def getFileSystemSpace():
    global dvc_filesystem_space_raw
    global dvc_filesystem_space
    global dvc_filesystem_percent
    out = subprocess.Popen("/bin/df -m | /bin/grep root", 
            shell=True,
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_filesystem_space_raw = stdout.decode('utf-8').rstrip()
    print_line('dvc_filesystem_space_raw=[{}]'.format(dvc_filesystem_space_raw), debug=True)
    lineParts = dvc_filesystem_space_raw.split()
    print_line('lineParts=[{}]'.format(lineParts), debug=True)
    filesystem_1GBlocks = int(lineParts[1],10) / 1024
    if filesystem_1GBlocks > 32:
        dvc_filesystem_space = '64GB'
    elif filesystem_1GBlocks > 16:
        dvc_filesystem_space = '32GB'
    elif filesystem_1GBlocks > 8:
        dvc_filesystem_space = '16GB'
    elif filesystem_1GBlocks > 4:
        dvc_filesystem_space = '8GB'
    elif filesystem_1GBlocks > 2:
        dvc_filesystem_space = '4GB'
    elif filesystem_1GBlocks > 1:
        dvc_filesystem_space = '2GB'
    else:
        dvc_filesystem_space = '1GB'
    print_line('dvc_filesystem_space=[{}]'.format(dvc_filesystem_space), debug=True)
    dvc_filesystem_percent = lineParts[4].replace('%', '')
    print_line('dvc_filesystem_percent=[{}]'.format(dvc_filesystem_percent), debug=True)

def getSystemTemperature():
    global dvc_system_temp
    dvc_system_temp = ''    # NOT avial on Omega2

def getLastUpdateDate():    # RERUN in loop
    global dvc_last_update_date
    apt_log_filespec = '/var/opkg-lists/omega2_base.sig'
    try:
        mtime = os.path.getmtime(apt_log_filespec)
    except OSError:
        mtime = 0
    last_modified_date = datetime.fromtimestamp(mtime, tz=local_tz)
    dvc_last_update_date  = last_modified_date
    print_line('dvc_last_update_date=[{}]'.format(dvc_last_update_date), debug=True)

def getFirmwareVersion():
    global dvc_firmware_version
    out = subprocess.Popen("/usr/bin/oupgrade -v | tr -d '>'", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    fw_version_raw = stdout.decode('utf-8').rstrip()
    lineParts = fw_version_raw.split(':')
    dvc_firmware_version = lineParts[1].lstrip()
    print_line('dvc_firmware_version=[{}]'.format(dvc_firmware_version), debug=True)
    
def getProcessorType():
    global dvc_processor_family
    out = subprocess.Popen("/bin/uname -m", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_processor_family = stdout.decode('utf-8').rstrip()
    print_line('dvc_processor_family=[{}]'.format(dvc_processor_family), debug=True)


# get model so we can use it too in MQTT
getDeviceModel()
getFirmwareVersion()
# get our hostnames so we can setup MQTT
getHostnames()
getLastUpdateDate()
getLinuxRelease()
getLinuxVersion()
getNetworkIFs()
getProcessorType()

# -----------------------------------------------------------------------------
#  timer and timer funcs for ALIVE MQTT Notices handling
# -----------------------------------------------------------------------------

ALIVE_TIMOUT_IN_SECONDS = 60

def publishAliveStatus():
    print_line('- SEND: yes, still alive -', debug=True)
    mqtt_client.publish(lwt_topic, payload=lwt_online_val, retain=False)

def aliveTimeoutHandler():
    print_line('- MQTT TIMER INTERRUPT -', debug=True)
    _thread.start_new_thread(publishAliveStatus, ())
    startAliveTimer()

def startAliveTimer():
    global aliveTimer
    global aliveTimerRunningStatus
    stopAliveTimer()
    aliveTimer = threading.Timer(ALIVE_TIMOUT_IN_SECONDS, aliveTimeoutHandler) 
    aliveTimer.start()
    aliveTimerRunningStatus = True
    print_line('- started MQTT timer - every {} seconds'.format(ALIVE_TIMOUT_IN_SECONDS), debug=True)

def stopAliveTimer():
    global aliveTimer
    global aliveTimerRunningStatus
    aliveTimer.cancel()
    aliveTimerRunningStatus = False
    print_line('- stopped MQTT timer', debug=True)

def isAliveTimerRunning():
    global aliveTimerRunningStatus
    return aliveTimerRunningStatus

# our ALIVE TIMER
aliveTimer = threading.Timer(ALIVE_TIMOUT_IN_SECONDS, aliveTimeoutHandler) 
# our BOOL tracking state of ALIVE TIMER
aliveTimerRunningStatus = False



# -----------------------------------------------------------------------------
#  MQTT setup and startup
# -----------------------------------------------------------------------------

# MQTT connection
lwt_topic = '{}/sensor/{}/status'.format(base_topic, sensor_name.lower())
lwt_online_val = 'online'
lwt_offline_val = 'offline'

print_line('Connecting to MQTT broker ...', verbose=True)
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_publish = on_publish
mqtt_client.on_log = on_log

mqtt_client.will_set(lwt_topic, payload=lwt_offline_val, retain=True)

if config['MQTT'].getboolean('tls', False):
    # According to the docs, setting PROTOCOL_SSLv23 "Selects the highest protocol version
    # that both the client and server support. Despite the name, this option can select
    # “TLS” protocols as well as “SSL”" - so this seems like a resonable default
    mqtt_client.tls_set(
        ca_certs=config['MQTT'].get('tls_ca_cert', None),
        keyfile=config['MQTT'].get('tls_keyfile', None),
        certfile=config['MQTT'].get('tls_certfile', None),
        tls_version=ssl.PROTOCOL_SSLv23
    )

mqtt_username = os.environ.get("MQTT_USERNAME", config['MQTT'].get('username'))
mqtt_password = os.environ.get("MQTT_PASSWORD", config['MQTT'].get('password', None))

if mqtt_username:
    mqtt_client.username_pw_set(mqtt_username, mqtt_password)
try:
    mqtt_client.connect(os.environ.get('MQTT_HOSTNAME', config['MQTT'].get('hostname', 'localhost')),
                        port=int(os.environ.get('MQTT_PORT', config['MQTT'].get('port', '1883'))),
                        keepalive=config['MQTT'].getint('keepalive', 60))
except:
    print_line('MQTT connection error. Please check your settings in the configuration file "config.ini"', error=True, sd_notify=True)
    sys.exit(1)
else:
    mqtt_client.publish(lwt_topic, payload=lwt_online_val, retain=False)
    mqtt_client.loop_start()
    sleep(1.0) # some slack to establish the connection
    startAliveTimer()


# -----------------------------------------------------------------------------
#  Perform our MQTT Discovery Announcement...
# -----------------------------------------------------------------------------

mac_basic = dvc_mac_raw.lower().replace(":", "")
mac_left = mac_basic[:6]
mac_right = mac_basic[6:]
print_line('mac lt=[{}], rt=[{}], mac=[{}]'.format(mac_left, mac_right, mac_basic), debug=True)
uniqID = "IoT-{}Mon{}".format(mac_left, mac_right)

# our IoT Reporter device
LD_MONITOR = "monitor" # KeyError: 'home310/sensor/rpi-pi3plus/values' let's not use this 'values' as topic
LD_FS_USED = "disk_used"
LDS_PAYLOAD_NAME = "info"

# Publish our MQTT auto discovery
#  table of key items to publish:
detectorValues = OrderedDict([
    (LD_MONITOR, dict(title="IoT Monitor {}".format(dvc_hostname), device_class="timestamp", no_title_prefix="yes", json_value="timestamp", json_attr="yes", icon='mdi:raspberry-pi', device_ident="IoT-{}".format(dvc_fqdn))),
    (LD_FS_USED, dict(title="IoT Used {}".format(dvc_hostname), no_title_prefix="yes", json_value="fs_free_prcnt", unit="%", icon='mdi:sd')),
])

print_line('Announcing IoT Monitoring device to MQTT broker for auto-discovery ...')

base_topic = '{}/sensor/{}'.format(base_topic, sensor_name.lower())
values_topic_rel = '{}/{}'.format('~', LD_MONITOR)
values_topic = '{}/{}'.format(base_topic, LD_MONITOR) 
activity_topic_rel = '{}/status'.format('~')     # vs. LWT
activity_topic = '{}/status'.format(base_topic)    # vs. LWT

command_topic_rel = '~/set'

for [sensor, params] in detectorValues.items():
    discovery_topic = 'homeassistant/sensor/{}/{}/config'.format(sensor_name.lower(), sensor)
    payload = OrderedDict()
    if 'no_title_prefix' in params:
        payload['name'] = "{}".format(params['title'].title())
    else:
        payload['name'] = "{} {}".format(sensor_name.title(), params['title'].title())
    payload['uniq_id'] = "{}_{}".format(uniqID, sensor.lower())
    if 'device_class' in params:
        payload['dev_cla'] = params['device_class']
    if 'unit' in params:
        payload['unit_of_measurement'] = params['unit']
    if 'json_value' in params:
        payload['stat_t'] = values_topic_rel
        payload['val_tpl'] = "{{{{ value_json.{}.{} }}}}".format(LDS_PAYLOAD_NAME, params['json_value'])
    payload['~'] = base_topic
    payload['pl_avail'] = lwt_online_val
    payload['pl_not_avail'] = lwt_offline_val
    if 'icon' in params:
        payload['ic'] = params['icon']
    payload['avty_t'] = activity_topic_rel
    if 'json_attr' in params:
        payload['json_attr_t'] = values_topic_rel
        payload['json_attr_tpl'] = '{{{{ value_json.{} | tojson }}}}'.format(LDS_PAYLOAD_NAME)
    if 'device_ident' in params:
        payload['dev'] = {
                'identifiers' : ["{}".format(uniqID)],
                'manufacturer' : 'Raspberry Pi (Trading) Ltd.',
                'name' : params['device_ident'],
                'model' : '{}'.format(dvc_model),
                'sw_version': "{} {}".format(dvc_linux_release, dvc_linux_version)
        }
    else:
         payload['dev'] = {
                'identifiers' : ["{}".format(uniqID)],
         }
    mqtt_client.publish(discovery_topic, json.dumps(payload), 1, retain=True)

    # remove connections as test:                  'connections' : [["mac", mac.lower()], [interface, ipaddr]],

# -----------------------------------------------------------------------------
#  timer and timer funcs for period handling
# -----------------------------------------------------------------------------

TIMER_INTERRUPT = (-1)
TEST_INTERRUPT = (-2)

def periodTimeoutHandler():
    print_line('- PERIOD TIMER INTERRUPT -', debug=True)
    handle_interrupt(TIMER_INTERRUPT) # '0' means we have a timer interrupt!!!
    startPeriodTimer()

def startPeriodTimer():
    global endPeriodTimer
    global periodTimeRunningStatus
    stopPeriodTimer()
    endPeriodTimer = threading.Timer(interval_in_minutes * 60.0, periodTimeoutHandler) 
    endPeriodTimer.start()
    periodTimeRunningStatus = True
    print_line('- started PERIOD timer - every {} seconds'.format(interval_in_minutes * 60.0), debug=True)

def stopPeriodTimer():
    global endPeriodTimer
    global periodTimeRunningStatus
    endPeriodTimer.cancel()
    periodTimeRunningStatus = False
    print_line('- stopped PERIOD timer', debug=True)

def isPeriodTimerRunning():
    global periodTimeRunningStatus
    return periodTimeRunningStatus



# our TIMER
endPeriodTimer = threading.Timer(interval_in_minutes * 60.0, periodTimeoutHandler) 
# our BOOL tracking state of TIMER
periodTimeRunningStatus = False
reported_first_time = False

# -----------------------------------------------------------------------------
#  MQTT Transmit Helper Routines
# -----------------------------------------------------------------------------
SCRIPT_TIMESTAMP = "timestamp"
DVC_MODEL = "dvc_model"
DVC_CONNECTIONS = "ifaces"
DVC_HOSTNAME = "host_name"
DVC_FQDN = "fqdn"
DVC_LINUX_RELEASE = "ux_release" 
DVC_LINUX_VERSION = "ux_version" 
DVC_UPTIME = "up_time"
DVC_DATE_LAST_UPDATE = "last_update"
DVC_FS_SPACE = 'fs_total_gb' # "fs_space_gbytes"
DVC_FS_AVAIL = 'fs_free_prcnt' # "fs_available_prcnt"
DVC_TEMP = "temperature_c"
DVC_SCRIPT = "reporter"
DVC_NETWORK = "networking"
DVC_INTERFACE = "interface"
SCRIPT_REPORT_INTERVAL = "report_interval"

def send_status(timestamp, nothing):
    global dvc_model
    global dvc_connections
    global dvc_hostname
    global dvc_fqdn
    global dvc_linux_release
    global dvc_linux_version
    global dvc_uptime
    global dvc_last_update_date
    global dvc_filesystem_space
    global dvc_filesystem_percent
    global dvc_system_temp
    global dvc_mqtt_script

    dvcData = OrderedDict()
    dvcData[SCRIPT_TIMESTAMP] = timestamp.astimezone().replace(microsecond=0).isoformat()
    dvcData[DVC_MODEL] = dvc_model
    dvcData[DVC_CONNECTIONS] = dvc_connections
    dvcData[DVC_HOSTNAME] = dvc_hostname
    dvcData[DVC_FQDN] = dvc_fqdn
    dvcData[DVC_LINUX_RELEASE] = dvc_linux_release
    dvcData[DVC_LINUX_VERSION] = dvc_linux_version
    dvcData[DVC_UPTIME] = dvc_uptime

    #  DON'T use V1 form of getting date (my dashbord mech)
    #actualDate = datetime.strptime(dvc_last_update_date, '%y%m%d%H%M%S')
    #actualDate.replace(tzinfo=local_tz)
    #dvcData[DVC_DATE_LAST_UPDATE] = actualDate.astimezone().replace(microsecond=0).isoformat()
    if dvc_last_update_date != datetime.min:
        dvcData[DVC_DATE_LAST_UPDATE] = dvc_last_update_date.astimezone().replace(microsecond=0).isoformat()
    else:
        dvcData[DVC_DATE_LAST_UPDATE] = ''
    dvcData[DVC_FS_SPACE] = int(dvc_filesystem_space.replace('GB', ''),10)
    dvcData[DVC_FS_AVAIL] = int(dvc_filesystem_percent,10)

    dvcData[DVC_NETWORK] = getNetworkDictionary()

    dvcData[DVC_TEMP] = dvc_system_temp
    dvcData[DVC_SCRIPT] = dvc_mqtt_script.replace('.py', '')
    dvcData[SCRIPT_REPORT_INTERVAL] = interval_in_minutes

    dvcTopDict = OrderedDict()
    dvcTopDict[LDS_PAYLOAD_NAME] = dvcData

    _thread.start_new_thread(publishMonitorData, (dvcTopDict, values_topic))

def getNetworkDictionary():
    global dvc_interfaces
    # TYPICAL:
    # dvc_interfaces=[[
    #   ('eth0', 'mac', 'b8:27:eb:1a:f3:bc'), 
    #   ('wlan0', 'IP', '192.168.100.189'), 
    #   ('wlan0', 'mac', 'b8:27:eb:4f:a6:e9')
    # ]]
    networkData = OrderedDict()

    priorIFKey = ''
    tmpData = OrderedDict()
    for currTuple in dvc_interfaces:
        currIFKey = currTuple[0]
        if priorIFKey == '':
            priorIFKey = currIFKey
        if currIFKey != priorIFKey:
            # save off prior if exists
            if priorIFKey != '':
                networkData[priorIFKey] = tmpData
                tmpData = OrderedDict()
                priorIFKey = currIFKey
        subKey = currTuple[1]
        subValue = currTuple[2]
        tmpData[subKey] = subValue
    networkData[priorIFKey] = tmpData
    print_line('networkData:{}"'.format(networkData), debug=True)
    return networkData

def publishMonitorData(latestData, topic):
    print_line('Publishing to MQTT topic "{}, Data:{}"'.format(topic, json.dumps(latestData)))
    mqtt_client.publish('{}'.format(topic), json.dumps(latestData), 1, retain=False)
    sleep(0.5) # some slack for the publish roundtrip and callback function  


def update_values():
    # nothing here yet
    getUptime()
    getFileSystemSpace()
    getSystemTemperature()
    getLastUpdateDate()

    

# -----------------------------------------------------------------------------

# Interrupt handler
def handle_interrupt(channel):
    global reported_first_time
    sourceID = "<< INTR(" + str(channel) + ")"
    current_timestamp = datetime.now(local_tz)
    print_line(sourceID + " >> Time to report! (%s)" % current_timestamp.strftime('%H:%M:%S - %Y/%m/%d'), verbose=True)
    # ----------------------------------
    # have PERIOD interrupt!
    update_values()

    if (opt_stall == False or reported_first_time == False and opt_stall == True):
        # ok, report our new detection to MQTT
        _thread.start_new_thread(send_status, (current_timestamp, ''))
        reported_first_time = True
    else:
        print_line(sourceID + " >> Time to report! (%s) but SKIPPED (TEST: stall)" % current_timestamp.strftime('%H:%M:%S - %Y/%m/%d'), verbose=True)
    
def afterMQTTConnect():
    print_line('* afterMQTTConnect()', verbose=True)
    #  NOTE: this is run after MQTT connects
    # start our interval timer
    startPeriodTimer()
    # do our first report
    handle_interrupt(0)

# TESTING AGAIN
getNetworkIFs()
#getLastUpdateDate()

# TESTING, early abort
#stopAliveTimer()
#exit(0)

afterMQTTConnect()  # now instead of after?

# now just hang in forever loop until script is stopped externally
try:
    while True:
        #  our INTERVAL timer does the work
        sleep(10000)
        
finally:
    # cleanup used pins... just because we like cleaning up after us
    stopPeriodTimer()   # don't leave our timers running!
    stopAliveTimer()

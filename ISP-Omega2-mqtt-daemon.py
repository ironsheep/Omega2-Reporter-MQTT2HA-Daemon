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

script_version = "1.1.0"
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

# -----------------------------------------------------------------------------
#  MQTT handlers
# -----------------------------------------------------------------------------

# Eclipse Paho callbacks - http://www.eclipse.org/paho/clients/python/docs/#callbacks
mqtt_client_connected = False
print_line('* init mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True)
mqtt_client_should_attempt_reconnect = True

def on_connect(client, userdata, flags, rc):
    global mqtt_client_connected
    if rc == 0:
        print_line('* MQTT connection established', console=True, sd_notify=True)
        print_line('')  # blank line?!
        #_thread.start_new_thread(afterMQTTConnect, ())
        mqtt_client_connected = True
        print_line('on_connect() mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True)
    else:
        print_line('! Connection error with result code {} - {}'.format(str(rc), mqtt.connack_string(rc)), error=True)
        print_line('MQTT Connection error with result code {} - {}'.format(str(rc), mqtt.connack_string(rc)), error=True, sd_notify=True)
        mqtt_client_connected = False   # technically NOT useful but readying possible new shape...
        print_line('on_connect() mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True, error=True)
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
dvc_last_fw_check_date = datetime.min
dvc_filesystem_space_raw = ''
dvc_filesystem_space = ''
dvc_filesystem_percent = ''
dvc_system_temp = ''
dvc_mqtt_script = script_info
dvc_mac_raw = ''
dvc_interfaces = []
dvc_filesystem = []
# Tuple (Total, Free, Avail.)
dvc_memory_tuple = ''
# Tuple (Hardware, Model Name, NbrCores, BogoMIPS)
dvc_cpu_tuple = ''

# -----------------------------------------------------------------------------
#  monitor variable fetch routines
#
def getDeviceCpuInfo():
    global dvc_cpu_tuple
    #  cat /proc/meminfo | egrep -i 'mem[tfa]'
    #  system type             : MediaTek MT7688 ver:1 eco:2
    #  machine                 : Onion Omega2+
    #  cpu model               : MIPS 24KEc V5.5
    #  BogoMIPS                : 385.84
    out = subprocess.Popen("cat /proc/cpuinfo | egrep -i 'system|cpu|bogo'",
           shell=True,
           stdout=subprocess.PIPE,
           stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    lines = stdout.decode('utf-8').split("\n")
    trimmedLines = []
    for currLine in lines:
        trimmedLine = currLine.lstrip().rstrip()
        trimmedLines.append(trimmedLine)
    cpu_hardware = ''
    cpu_cores = 1
    cpu_model = ''
    cpu_bogoMIPS = ''
    for currLine in trimmedLines:
        lineParts = currLine.split(':')
        if 'system' in currLine:
            cpu_hardware = currLine.replace('system type','').replace(': ','').lstrip().rstrip()
        if 'cpu' in currLine:
            cpu_model = lineParts[1].lstrip().rstrip()
        if 'Bogo' in currLine:
            cpu_bogoMIPS = float(lineParts[1])
    # Tuple (Hardware, Model Name, NbrCores, BogoMIPS)
    dvc_cpu_tuple = ( cpu_hardware, cpu_model, cpu_cores, cpu_bogoMIPS )
    print_line('dvc_cpu_tuple=[{}]'.format(dvc_cpu_tuple), debug=True)

def getDeviceMemory():
    global dvc_memory_tuple
    #  cat /proc/meminfo | egrep -i 'mem[tfa]'
    #  MemTotal:         124808 kB
    #  MemFree:           45264 kB
    #  MemAvailable:      41640 kB
    out = subprocess.Popen("cat /proc/meminfo | egrep -i 'mem[tfa]'",
           shell=True,
           stdout=subprocess.PIPE,
           stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    lines = stdout.decode('utf-8').split("\n")
    trimmedLines = []
    for currLine in lines:
        trimmedLine = currLine.lstrip().rstrip()
        trimmedLines.append(trimmedLine)
    mem_total = ''
    mem_free = ''
    mem_avail = ''
    for currLine in trimmedLines:
        lineParts = currLine.split()
        if 'MemTotal' in currLine:
            mem_total = float(lineParts[1]) / 1024
        if 'MemFree' in currLine:
            mem_free = float(lineParts[1]) / 1024
        if 'MemAvail' in currLine:
            mem_avail = float(lineParts[1]) / 1024
    # Tuple (Total, Free, Avail.)
    dvc_memory_tuple = ( mem_total, mem_free, mem_avail )
    print_line('dvc_memory_tuple=[{}]'.format(dvc_memory_tuple), debug=True)

def getDeviceModel():
    global dvc_model
    global dvc_model_raw
    global dvc_connections
    out = subprocess.Popen("cat /proc/cpuinfo | grep machine",
           shell=True,
           stdout=subprocess.PIPE,
           stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    dvc_model_raw = stdout.decode('utf-8').lstrip().rstrip()
    # now reduce string length (just more compact, same info)
    lineParts = dvc_model_raw.split(':')
    if len(lineParts) > 1:
        dvc_model = lineParts[1].lstrip().rstrip()
    else:
        dvc_model = ''

    # now decode interfaces
    dvc_connections = 'w' # default

    print_line('dvc_model_raw=[{}]'.format(dvc_model_raw), debug=True)
    print_line('dvc_model=[{}]'.format(dvc_model), debug=True)
    print_line('dvc_connections=[{}]'.format(dvc_connections), debug=True)

def getLinuxRelease():
    global dvc_linux_release
    dvc_linux_release = 'OpenWrt'
    print_line('dvc_linux_release=[{}]'.format(dvc_linux_release), debug=True)

def getLinuxVersion():
    global dvc_linux_version
    out = subprocess.Popen("/bin/uname -r",
           shell=True,
           stdout=subprocess.PIPE,
           stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
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
    stdout, _ = out.communicate()
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
    stdout, _ = out.communicate()
    dvc_uptime_raw = stdout.decode('utf-8').rstrip().lstrip()
    print_line('dvc_uptime_raw=[{}]'.format(dvc_uptime_raw), debug=True)
    basicParts = dvc_uptime_raw.split()
    timeStamp = basicParts[0]

    # uptime<RET>
    #  03:29:23 up 12 min,  load average: 0.02, 0.07, 0.07
    lineParts = dvc_uptime_raw.split(',')
    #print_line('lineParts=[{}]'.format(lineParts), debug=True)
    dvc_uptime_raw = lineParts[0]
    print_line('dvc_uptime_raw=[{}]'.format(dvc_uptime_raw), debug=True)
    dvc_uptime = dvc_uptime_raw.replace(timeStamp, '').lstrip().replace('up ', '').lstrip()
    print_line('dvc_uptime=[{}]'.format(dvc_uptime), debug=True)

def getNetworkIFs():    # RERUN in loop
    global dvc_interfaces
    global dvc_mac_raw
    out = subprocess.Popen('/sbin/ifconfig | egrep "Link|flags|inet|ether" | egrep -v -i "lo:|loopback|inet6|\:\:1|127\.0\.0\.1"',
           shell=True,
           stdout=subprocess.PIPE,
           stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
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

def getFileSystemDrives():
    global dvc_filesystem_space_raw
    global dvc_filesystem_space
    global dvc_filesystem_percent
    global dvc_filesystem
    out = subprocess.Popen("/bin/df -m | /usr/bin/tail -n +2 | /bin/egrep -v 'tmpfs|boot|mmcblk|mtdblock|/rom'",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    lines = stdout.decode('utf-8').split("\n")
    trimmedLines = []
    for currLine in lines:
        trimmedLine = currLine.lstrip().rstrip()
        if len(trimmedLine) > 0:
            trimmedLines.append(trimmedLine)

    print_line('getFileSystemDrives() trimmedLines=[{}]'.format(trimmedLines), debug=True)

    #  EXAMPLES
    #  /dev/root          59998   9290     48208  17% /
    #  /dev/sda1         937872 177420    712743  20% /media/data
    # or
    #  /dev/root          59647  3328     53847   6% /
    #  /dev/sda1           3703    25      3472   1% /media/pi/SANDISK
    # or
    #  xxx.xxx.xxx.xxx:/srv/c2db7b94 200561 148655 41651 79% /

    # FAILING Case v1.4.0:
    # Here is the output of 'df -m'

    # Sys. de fichiers blocs de 1M Utilisé Disponible Uti% Monté sur
    # /dev/root 119774 41519 73358 37% /
    # devtmpfs 1570 0 1570 0% /dev
    # tmpfs 1699 0 1699 0% /dev/shm
    # tmpfs 1699 33 1667 2% /run
    # tmpfs 5 1 5 1% /run/lock
    # tmpfs 1699 0 1699 0% /sys/fs/cgroup
    # /dev/mmcblk0p1 253 55 198 22% /boot
    # tmpfs 340 0 340 0% /run/user/1000

    tmpDrives = []
    for currLine in trimmedLines:
        lineParts = currLine.split()
        print_line('lineParts({})=[{}]'.format(len(lineParts), lineParts), debug=True)
        if len(lineParts) < 6:
            print_line('BAD LINE FORMAT, Skipped=[{}]'.format(lineParts), debug=True, warning=True)
            continue
        # tuple { total blocks, used%, mountPoint, device }
        total_size_in_gb = '{:.0f}'.format(next_power_of_2(lineParts[1]))
        newTuple = ( total_size_in_gb, lineParts[4].replace('%',''), lineParts[5],  lineParts[0] )
        tmpDrives.append(newTuple)
        print_line('newTuple=[{}]'.format(newTuple), debug=True)
        if newTuple[2] == '/':
            dvc_filesystem_space_raw = currLine
            dvc_filesystem_space = newTuple[0]
            dvc_filesystem_percent = newTuple[1]
            print_line('dvc_filesystem_space=[{}GB]'.format(newTuple[0]), debug=True)
            print_line('dvc_filesystem_percent=[{}]'.format(newTuple[1]), debug=True)

    dvc_filesystem = tmpDrives
    print_line('dvc_filesystem=[{}]'.format(dvc_filesystem), debug=True)

def next_power_of_2(size):
    size_as_nbr = int(size) - 1
    return 1 if size == 0 else (1<<size_as_nbr.bit_length()) / 1024

def getSystemTemperature():
    global dvc_system_temp
    dvc_system_temp = 'n/a'    # NOT avial on Omega2

def getLastUpdateDate():    # RERUN in loop
    global dvc_last_update_date
    global dvc_last_fw_check_date
    opkg_log_filespec = '/var/opkg-lists/omega2_base.sig'
    try:
        mtime = os.path.getmtime(opkg_log_filespec)
    except OSError:
        mtime = 0
    last_modified_date = datetime.fromtimestamp(mtime, tz=local_tz)
    dvc_last_update_date  = last_modified_date
    print_line('dvc_last_update_date=[{}]'.format(dvc_last_update_date), debug=True)

    oupgrade_log_filespec = '/var/oupgrade.log'
    try:
        mtime = os.path.getmtime(oupgrade_log_filespec)
    except OSError:
        mtime = 0
    last_modified_date = datetime.fromtimestamp(mtime, tz=local_tz)
    dvc_last_fw_check_date  = last_modified_date
    print_line('dvc_last_fw_check_date=[{}]'.format(dvc_last_fw_check_date), debug=True)

def getFirmwareVersion():
    global dvc_firmware_version
    out = subprocess.Popen("/usr/bin/oupgrade -v | tr -d '>'",
           shell=True,
           stdout=subprocess.PIPE,
           stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
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
    stdout, _ = out.communicate()
    dvc_processor_family = stdout.decode('utf-8').rstrip()
    print_line('dvc_processor_family=[{}]'.format(dvc_processor_family), debug=True)


# get model so we can use it too in MQTT
getDeviceModel()
getFirmwareVersion()
# get our hostnames so we can setup MQTT
getHostnames()
getDeviceCpuInfo()
getProcessorType()
getLastUpdateDate()
getLinuxRelease()
getLinuxVersion()
getNetworkIFs()



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
if sensor_name == default_sensor_name:
    sensor_name = 'dvc-{}'.format(dvc_hostname.lower())
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

    while mqtt_client_connected == False: #wait in loop
        print_line('* Wait on mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True)
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
    (LD_MONITOR, dict(title="Monitor {}".format(dvc_hostname), device_class="timestamp", no_title_prefix="yes", json_value="timestamp", json_attr="yes", icon='mdi:raspberry-pi', device_ident="IoT-{}".format(dvc_fqdn))),
    (LD_FS_USED, dict(title="Used {}".format(dvc_hostname), no_title_prefix="yes", json_value="fs_free_prcnt", unit="%", icon='mdi:sd')),
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
                'manufacturer' : 'Onion Corporation',
                'name' : params['device_ident'],
                'model' : '{}'.format(dvc_model),
                'sw_version': "v{}".format(dvc_firmware_version)
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
DVC_MODEL = "rpi_model"
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
# new drives dictionary
DVC_DRIVES = "drives"
DVC_DRV_BLOCKS = "size_gb"
DVC_DRV_USED = "used_prcnt"
DVC_DRV_MOUNT = "mount_pt"
DVC_DRV_DEVICE = "device"
DVC_DRV_NFS = "device-nfs"
DVC_DVC_IP = "ip"
DVC_DVC_PATH = "dvc"
# new memory dictionary
DVC_MEMORY = "memory"
DVC_MEM_TOTAL = "size_mb"
DVC_MEM_FREE = "free_mb"
# Tuple (Hardware, Model Name, NbrCores, BogoMIPS)
DVC_CPU = "cpu"
DVC_CPU_HARDWARE = "hardware"
DVC_CPU_MODEL = "model_name"
DVC_CPU_CORES = "number_cores"
DVC_CPU_BOGOMIPS = "bogo_mips"


def send_status(timestamp, nothing):
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

    dvcDrives = getDrivesDictionary()
    if len(dvcDrives) > 0:
        dvcData[DVC_DRIVES] = dvcDrives

    dvcRam = getMemoryDictionary()
    if len(dvcRam) > 0:
        dvcData[DVC_MEMORY] = dvcRam

    dvcCpu = getCPUDictionary()
    if len(dvcCpu) > 0:
        dvcData[DVC_CPU] = dvcCpu

    dvcData[DVC_TEMP] = dvc_system_temp
    dvcData[DVC_SCRIPT] = dvc_mqtt_script.replace('.py', '')
    dvcData[SCRIPT_REPORT_INTERVAL] = interval_in_minutes

    dvcTopDict = OrderedDict()
    dvcTopDict[LDS_PAYLOAD_NAME] = dvcData

    _thread.start_new_thread(publishMonitorData, (dvcTopDict, values_topic))

def getDrivesDictionary():
    dvcDrives = OrderedDict()
    # tuple { total blocks, used%, mountPoint, device }
    for driveTuple in dvc_filesystem:
        dvcSingleDrive = OrderedDict()
        dvcSingleDrive[DVC_DRV_BLOCKS] = int(driveTuple[0])
        dvcSingleDrive[DVC_DRV_USED] = int(driveTuple[1])
        device = driveTuple[3]
        # special 'overlayfs' for omega2+ devices
        if ':' in device and 'overlayfs' not in device:
            dvcDevice = OrderedDict()
            lineParts = device.split(':')
            dvcDevice[DVC_DVC_IP] = lineParts[0]
            dvcDevice[DVC_DVC_PATH] = lineParts[1]
            dvcSingleDrive[DVC_DRV_NFS] = dvcDevice
        else:
            dvcSingleDrive[DVC_DRV_DEVICE] = device
            #rpiTest = OrderedDict()
            #rpiTest[DVC_DVC_IP] = '255.255.255.255'
            #rpiTest[DVC_DVC_PATH] = '/srv/c2db7b94'
            #dvcSingleDrive[DVC_DRV_NFS] = rpiTest
        dvcSingleDrive[DVC_DRV_MOUNT] = driveTuple[2]
        driveKey = driveTuple[2].replace('/','-').replace('-','',1)
        if len(driveKey) == 0:
            driveKey = "root"
        dvcDrives[driveKey] = dvcSingleDrive
    return dvcDrives;

def getNetworkDictionary():
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
    #print_line('networkData:{}"'.format(networkData), debug=True)
    return networkData

def getMemoryDictionary():
    # TYPICAL:
    #   Tuple (Total, Free, Avail.)
    memoryData = OrderedDict()
    if dvc_memory_tuple != '':
        memoryData[DVC_MEM_TOTAL] = '{:.3f}'.format(dvc_memory_tuple[0])
        memoryData[DVC_MEM_FREE] = '{:.3f}'.format(dvc_memory_tuple[2])
    #print_line('memoryData:{}"'.format(memoryData), debug=True)
    return memoryData

def getCPUDictionary():
    # TYPICAL:
    #   Tuple (Hardware, Model Name, NbrCores, BogoMIPS)
    cpuDict = OrderedDict()
    #print_line('dvc_cpu_tuple:{}"'.format(dvc_cpu_tuple), debug=True)
    if dvc_cpu_tuple != '':
        cpuDict[DVC_CPU_HARDWARE] = dvc_cpu_tuple[0]
        cpuDict[DVC_CPU_MODEL] = dvc_cpu_tuple[1]
        cpuDict[DVC_CPU_CORES] = dvc_cpu_tuple[2]
        cpuDict[DVC_CPU_BOGOMIPS] = '{:.2f}'.format(dvc_cpu_tuple[3])
    #print_line('cpuDict:{}"'.format(cpuDict), debug=True)
    return cpuDict

def publishMonitorData(latestData, topic):
    print_line('Publishing to MQTT topic "{}, Data:{}"'.format(topic, json.dumps(latestData)))
    mqtt_client.publish('{}'.format(topic), json.dumps(latestData), 1, retain=False)
    sleep(0.5) # some slack for the publish roundtrip and callback function


def update_values():
    # nothing here yet
    getUptime()
    getDeviceMemory()
    getFileSystemDrives()
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


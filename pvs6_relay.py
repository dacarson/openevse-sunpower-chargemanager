#!/usr/bin/env python3
"""
pvs6_relay: reads the SunPower PVS6 WebSocket power stream and relays it to
  * InfluxDB (measurement `sunpower_power`), and
  * OpenEVSE solar divert, as POST /status {"grid_ie": W} (see OpenEVSEGridFeeder).
Formerly pvs6_ws_logger.py.
"""
import argparse
import time
import json
import websocket
import ssl
from pprint import pprint
import requests
import base64
import os
import urllib3
import threading

from openevse_client import OpenEVSE

# Disable SSL warnings since we're connecting to local PVS6 devices
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_PVS6_IP = "172.27.153.1"

# InfluxDB (HTTP Line Protocol) settings
INFLUX_URL = "http://127.0.0.1:8086/write"
INFLUX_DB = "pvs6"          # change if needed
INFLUX_PRECISION = "s"             # seconds
session = requests.Session()

# Global variables for data monitoring
last_data_time = None
telemetry_timer = None

# OpenEVSE solar-divert feed (set up in __main__ when --openevse_host is given)
feeder = None

# ############################################################################


class OpenEVSEGridFeeder:
    """
    Pushes the PVS6 grid reading (net_p, kW; positive = importing, negative = exporting)
    to OpenEVSE as POST /status {"grid_ie": W}, which drives the firmware's solar divert.

    Samples are averaged over each interval and sent from a background thread so a slow
    or offline charger never blocks the websocket. If no fresh samples arrive, nothing is
    sent; OpenEVSE's /status `divert_update` age then grows and openevse_manager.py pauses
    charging rather than trusting a stale reading. The interval must be >= 1 s: the divert
    smoothing filter in firmware 5.1.5 measures time between updates in whole seconds.
    """

    def __init__(self, evse, interval=5.0, verbose=False):
        self.evse = evse
        self.interval = max(1.0, float(interval))
        self.verbose = verbose
        self._lock = threading.Lock()
        self._sum = 0.0
        self._count = 0
        self._failures = 0
        threading.Thread(target=self._run, name="openevse-feeder", daemon=True).start()

    def add_sample(self, net_kw):
        try:
            w = float(net_kw) * 1000.0
        except (TypeError, ValueError):
            return
        with self._lock:
            self._sum += w
            self._count += 1

    def _run(self):
        # Never let an unexpected error kill the feeder thread.
        while True:
            try:
                self._tick()
            except Exception as e:
                print("OpenEVSE feeder error:", e)

    def _tick(self):
        time.sleep(self.interval)
        with self._lock:
            count, total = self._count, self._sum
            self._sum, self._count = 0.0, 0
        if count == 0:
            return
        watts = total / count
        try:
            self.evse.post_grid_ie(watts)
            if self._failures:
                print("OpenEVSE grid_ie feed recovered after {} failures".format(self._failures))
            self._failures = 0
            if self.verbose:
                print("OpenEVSE grid_ie={:.0f}W ({} samples)".format(watts, count))
        except Exception as e:
            self._failures += 1
            # log the first failure and then every ~5 minutes
            if self._failures == 1 or self._failures % max(1, int(300 / self.interval)) == 0:
                print("Failed to POST grid_ie to OpenEVSE ({} in a row): {}".format(self._failures, e))

# ############################################################################

def check_data_timeout():
    """Check if we haven't received data for 5 seconds and re-enable telemetry if needed"""
    global last_data_time, telemetry_timer
    
    if last_data_time is None:
        return
    
    current_time = time.time()
    if current_time - last_data_time > 5:
        print("No data received for 5+ seconds. Re-enabling telemetry...")
        enable_telemetry(args.ip, args.serial_number)
        last_data_time = current_time  # Reset the timer
    
    # Schedule the next check in 1 second
    telemetry_timer = threading.Timer(1.0, check_data_timeout)
    telemetry_timer.start()

def enable_telemetry(ip_address, serial_number):
    """
    Enable web socket telemetry on PVS6 by logging in and setting the telemetry flag.
    Based on the enable_telemetry.sh script logic.
    """
    try:
        # Extract last 5 digits of serial number as password
        password = serial_number[-5:]
        
        if args.verbose:
            print(f"Enabling telemetry for IP: {ip_address}, Serial: {serial_number}")
            print(f"Password (last 5 digits): {password}")
        
        # Create authorization header
        auth_string = f"ssm_owner:{password}"
        auth_header = base64.b64encode(auth_string.encode()).decode()
        
        # Create session for cookie management (like the shell script)
        session = requests.Session()
        session.verify = False  # Disable SSL verification like the shell script
        
        # Login to PVS6 (this will set cookies in the session)
        login_url = f"https://{ip_address}/auth?login"
        login_headers = {"Authorization": f"basic {auth_header}"}
        
        if args.verbose:
            print("Logging in...")
        
        login_response = session.get(login_url, headers=login_headers, timeout=10)
        
        if args.verbose:
            print(f"Login response: {login_response.status_code} - {login_response.text}")
        
        # Check if login was successful
        if login_response.status_code != 200:
            print(f"Login failed with status: {login_response.status_code}")
            return False
        
        # Enable web socket telemetry using the same session (cookies will be automatically included)
        telemetry_url = f"https://{ip_address}/vars?set=/sys/telemetryws/enable=1"
        
        if args.verbose:
            print("Enabling web socket telemetry...")
        
        telemetry_response = session.get(telemetry_url, timeout=10)
        
        if args.verbose:
            print(f"Telemetry response: {telemetry_response.status_code} - {telemetry_response.text}")
        
        # Check if telemetry was successfully enabled
        if telemetry_response.status_code == 200:
            print("Telemetry enabled successfully")
            return True
        else:
            print(f"Failed to enable telemetry: {telemetry_response.status_code}")
            return False
            
    except Exception as e:
        print(f"Error enabling telemetry: {e}")
        return False


def influxdb_publish(measurement, fields, timestamp=None):
    """
    Publish one measurement via InfluxDB HTTP /write endpoint using line protocol.
    This mirrors the other logging script's approach.
    """
    if not fields:
        print("Not publishing empty data for:", measurement)
        return

    # Remove any 'time' field from fields (timestamp is passed separately)
    fields = {k: v for k, v in fields.items() if k != "time"}

    def escape_key(s):
        return str(s).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

    def format_field_value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        # Treat ints as ints with 'i' suffix; floats as floats
        if isinstance(v, int) and not isinstance(v, bool):
            return "{}i".format(v)
        if isinstance(v, float):
            # Use repr to avoid locale; ensure finite
            return repr(v)
        # Strings must be quoted with internal quotes escaped
        return "\"{}\"".format(str(v).replace("\\", "\\\\").replace("\"", "\\\""))

    # Build field set
    field_pairs = []
    for k, v in fields.items():
        try:
            field_pairs.append("{}={}".format(escape_key(k), format_field_value(v)))
        except Exception:
            # If a field can't be formatted, coerce to string
            field_pairs.append("{}=\"{}\"".format(escape_key(k), str(v)))

    if not field_pairs:
        print("No fields after formatting; skipping publish for:", measurement)
        return

    # Timestamp: seconds precision (per INFLUX_PRECISION)
    ts = int(timestamp if timestamp is not None else time.time())

    line = "{} {} {}".format(escape_key(measurement), ','.join(field_pairs), ts)
    payload = "\n".join([line])

    if args.verbose:
        print("Publishing to InfluxDB HTTP: {}".format(payload))

    try:
        params = {"db": INFLUX_DB, "precision": INFLUX_PRECISION}
        r = session.post(INFLUX_URL, params=params, data=payload.encode("utf-8"), timeout=10)
        r.raise_for_status()
        if args.verbose:
            try:
                print("Influx POST OK: status={} len={} resp={!r}".format(r.status_code, len(getattr(r, "text", "") or ""), (getattr(r, "text", "") or "")[:120]))
            except Exception:
                pass
    except Exception as e:
        status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
        body = ""
        try:
            body = e.response.text[:200] if hasattr(e, "response") and hasattr(e.response, "text") else ""
        except Exception:
            body = ""
        print("Failed to POST to InfluxDB: {} status={} url={} db={}".format(e, status, INFLUX_URL, INFLUX_DB))
        if body:
            print("  Response:", body)
        print("  Payload was:", payload)


def on_message(ws, message):
    global last_data_time
    
    try:
        data = json.loads(message)
        
        # Update the last data received time
        last_data_time = time.time()
        
        if args.raw or args.verbose:
            pprint(data)

        # InfluxDB logging comes first and never depends on OpenEVSE.
        if args.influxdb and data.get("notification") == "power":
            ts = data["params"].get("time")
            if ts:
                influxdb_publish("sunpower_power", data["params"], timestamp=int(ts))
            else:
                influxdb_publish("sunpower_power", data["params"])

    except Exception:
        print("Invalid JSON received")
        return

    # OpenEVSE feed: only queues a sample (the HTTP POST happens on the feeder thread),
    # and is isolated so nothing here can affect logging.
    if feeder is not None and data.get("notification") == "power":
        try:
            feeder.add_sample(data["params"].get("net_p"))
        except Exception as e:
            print("OpenEVSE feed: could not queue sample:", e)


def on_error(ws, error):
    print("WebSocket Error:", error)


def on_close(ws, close_status_code, close_msg):
    global telemetry_timer
    
    print("WebSocket closed:", close_status_code, close_msg)
    
    # Stop the data monitoring timer
    if telemetry_timer:
        telemetry_timer.cancel()
        telemetry_timer = None


def on_open_with_telemetry(ws):
    """WebSocket on_open handler that enables telemetry after connection is established"""
    global last_data_time, telemetry_timer
    
    print("WebSocket connection opened (verbose={})".format(bool(args.verbose)))
    print("Enabling telemetry...")
    enable_telemetry(args.ip, args.serial_number)
    
    # Initialize data monitoring
    last_data_time = time.time()
    
    # Start the data timeout checker
    if telemetry_timer:
        telemetry_timer.cancel()
    telemetry_timer = threading.Timer(1.0, check_data_timeout)
    telemetry_timer.start()
    print("Data monitoring timer started")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default=DEFAULT_PVS6_IP, help="IP address of the PVS6")
    parser.add_argument("--serial_number", required=True, help="Serial number of the PVS6 (required for telemetry enablement)")
    parser.add_argument("--raw", action="store_true", help="Print raw data to stdout")
    parser.add_argument("--influxdb", action="store_true", help="Publish to InfluxDB")
    parser.add_argument("--influxdb_user", default=None, help="InfluxDB username (if auth is enabled)")
    parser.add_argument("--influxdb_pass", default=None, help="InfluxDB password")
    parser.add_argument("--openevse_host", default=None,
                        help="OpenEVSE host/IP: feed net_p to its solar divert as grid_ie")
    parser.add_argument("--openevse_user", default=None, help="OpenEVSE web UI username (if auth is enabled)")
    parser.add_argument("--openevse_pass", default=None, help="OpenEVSE web UI password")
    parser.add_argument("--openevse_interval", type=float, default=5.0,
                        help="Seconds between grid_ie updates to OpenEVSE (default 5, minimum 1)")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    if args.influxdb_user:
        session.params = {"u": args.influxdb_user, "p": args.influxdb_pass or ""}

    if args.openevse_host:
        feeder = OpenEVSEGridFeeder(
            OpenEVSE(args.openevse_host, args.openevse_user, args.openevse_pass, timeout=5),
            interval=args.openevse_interval, verbose=args.verbose)
        print("Feeding grid_ie to OpenEVSE at {} every {}s".format(args.openevse_host, feeder.interval))

    # Build WebSocket URL from IP address
    ws_url = f"ws://{args.ip}:9002"
    
    print("pvs6_relay starting; influxdb={}, db='{}', url='{}'".format(bool(args.influxdb), INFLUX_DB, INFLUX_URL))
    print(f"Connecting to PVS6 at {args.ip}")

    while True:
        try:
            ws = websocket.WebSocketApp(ws_url,
                                        on_open=on_open_with_telemetry,
                                        on_message=on_message,
                                        on_error=on_error,
                                        on_close=on_close)

            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            print("Exception in WebSocket loop:", e)

        print("WebSocket connection lost or failed to start. Retrying in 5 minutes...")
        time.sleep(300)

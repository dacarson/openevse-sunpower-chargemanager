#!/usr/bin/env python3
"""
OpenEVSE solar charge manager: TOU policy on top of the firmware's built-in solar divert,
plus InfluxDB logging.

The OpenEVSE firmware's Eco ("solar divert") mode does the second-to-second work:
it receives the grid import/export reading (fed by pvs6_relay.py via POST /status
{"grid_ie": W}), adds back the car's own draw, smooths it, and sets the pilot current.
This script runs once a minute and does what the firmware can't:

  * Time-of-use tuning: rewrites `divert_PV_ratio` when the PG&E TOU period changes.
    Off-Peak tolerates some grid draw to keep a session going (the old
    --offpeak-grid-tolerance). Peak and Part-Peak use 1.1: charge only from excess solar,
    with a 100 W buffer and no rounding up, so the car doesn't import at the dearest rates.
    Exports earn a flat rate regardless of TOU, so there's no reason to hold solar back.
  * Overnight charging: during Off-Peak, when solar production is below a threshold,
    claims the EVSE "active" at full current (unless the month is a no-overnight month).
  * Stale-feed failsafe: firmware 5.1.5 keeps the last grid_ie value forever. If the feed
    stops while in Eco mode, the charger would keep acting on a stale export reading, so
    this script claims the EVSE "disabled" until the feed resumes.
  * Logging: OpenEVSE state to InfluxDB (`openevse`), the policy decision
    (`openevse_policy`), and the legacy `solar_charge_control` measurement so existing Grafana panels keep working.

Claims are made at the firmware's API priority (500). A manual override from the OpenEVSE
UI or HomeKit (priority 1000) always wins, so "charge now" needs no special handling here.
If the charger's divert mode is set to Normal (not Eco), the script releases its claim and
leaves the charger alone.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

import pytz
import requests

from openevse_client import (
    DEFAULT_CLAIM_CLIENT_ID,
    DIVERT_MODE_ECO,
    EVSE_STATE_NAMES,
    OpenEVSE,
    is_fault_state,
)

PACIFIC = pytz.timezone("America/Los_Angeles")
DEFAULT_NO_OVERNIGHT_MONTHS = [6, 7, 8, 9]  # June-September


# --- TOU -----------------------------------------------------------------------------------

def get_tou_period(now_pt: datetime) -> str:
    """PG&E TOU period (same windows as the old ChargePoint controller)."""
    h = now_pt.hour
    if 16 <= h < 21:
        return "peak"
    if h < 15:
        return "off_peak"
    return "part_peak"


# --- divert_PV_ratio mapping --------------------------------------------------------------
#
# Firmware 5.1.5 divert (grid mode), per update:
#   available   = export_amps + car_amps - reserve,  reserve = max(0, ratio-1) * 1000W / V
#   trigger     = min_current * min(1, ratio)
#   start/keep  when smoothed(available) >= trigger + 0.5A; stop when <= trigger
#               (after divert_min_charge_time)
#   charge_rate = floor(available), +1A if the fractional part > min(1, ratio)
#
# So a ratio < 1 lets charging start/continue with (1-ratio)*min_power of grid draw, and a
# ratio > 1 keeps (ratio-1) kW exported at all times.

def ratio_for_grid_tolerance(tolerance_w: float, min_power_w: float) -> float:
    """Off-Peak: tolerate up to `tolerance_w` of grid draw at the minimum charge rate."""
    if min_power_w <= 0:
        return 1.0
    return round(min(1.0, max(0.05, (min_power_w - tolerance_w) / min_power_w)), 3)


# Peak / Part-Peak: the firmware default. Starts only when solar alone covers the minimum
# current (+0.5 A hysteresis), keeps a 100 W buffer, and never rounds the current up.
DEFAULT_PEAK_PV_RATIO = 1.1
DEFAULT_PART_PEAK_PV_RATIO = 1.1


@dataclass
class PolicyConfig:
    offpeak_grid_tolerance: float = 500.0
    peak_pv_ratio: float = DEFAULT_PEAK_PV_RATIO
    part_peak_pv_ratio: float = DEFAULT_PART_PEAK_PV_RATIO
    off_peak_pv_ratio: float | None = None   # None = derive from offpeak_grid_tolerance
    no_overnight_months: set = field(default_factory=lambda: set(DEFAULT_NO_OVERNIGHT_MONTHS))
    overnight_periods: set = field(default_factory=lambda: {"off_peak"})
    night_production_w: float = 500.0
    stale_feed_seconds: float = 120.0
    tune_ratio: bool = True


@dataclass
class PolicyInputs:
    now_pt: datetime
    divertmode: int | None
    feed_age_s: float | None          # /status divert_update
    production_w: float | None        # PVS6 mean pv_p (W) over the averaging window
    min_current: float
    max_current: float
    voltage: float


@dataclass
class Decision:
    period: str
    pv_ratio: float | None            # None = leave divert_PV_ratio alone
    claim: str | None                 # None (release), "active" or "disabled"
    charge_current: int | None
    reason: str


def decide(inp: PolicyInputs, cfg: PolicyConfig) -> Decision:
    period = get_tou_period(inp.now_pt)
    voltage = inp.voltage if inp.voltage and inp.voltage > 60 else 240.0
    min_power = inp.min_current * voltage

    ratio = None
    if cfg.tune_ratio:
        if period == "peak":
            ratio = cfg.peak_pv_ratio
        elif period == "off_peak":
            ratio = cfg.off_peak_pv_ratio if cfg.off_peak_pv_ratio is not None else \
                ratio_for_grid_tolerance(cfg.offpeak_grid_tolerance, min_power)
        else:
            ratio = cfg.part_peak_pv_ratio

    if inp.divertmode != DIVERT_MODE_ECO:
        return Decision(period, None, None, None,
                        f"divert mode is {inp.divertmode} (not Eco); leaving the charger alone")

    dark = inp.production_w is not None and inp.production_w < cfg.night_production_w
    if dark and period in cfg.overnight_periods:
        if inp.now_pt.month in cfg.no_overnight_months:
            reason = f"low production, {period}, but month {inp.now_pt.month} is a no-overnight month"
            # fall through: divert will keep it off; still apply the stale-feed failsafe
        else:
            return Decision(period, ratio, "active", int(inp.max_current),
                            f"low production ({inp.production_w:.0f}W) during {period}; "
                            f"overnight charging at {int(inp.max_current)}A")
    else:
        reason = f"{period}: solar divert in control"

    if inp.feed_age_s is None or inp.feed_age_s > cfg.stale_feed_seconds:
        return Decision(period, ratio, "disabled", None,
                        f"grid_ie feed stale (last update {inp.feed_age_s}s ago); pausing")

    return Decision(period, ratio, None, None, reason)


# --- InfluxDB (1.x HTTP API, same approach as pvs6_relay.py) ----------------------------

class Influx:
    def __init__(self, host, port, user, password, timeout=10.0):
        self.base = f"http://{host}:{port}"
        self.auth = {"u": user, "p": password} if user else {}
        self.timeout = timeout
        self.session = requests.Session()

    def query(self, db: str, q: str) -> list[dict]:
        params = {"db": db, "q": q, "epoch": "s", **self.auth}
        r = self.session.get(f"{self.base}/query", params=params, timeout=self.timeout)
        r.raise_for_status()
        rows = []
        for result in r.json().get("results", []):
            for series in result.get("series", []) or []:
                cols = series["columns"]
                for values in series["values"]:
                    rows.append(dict(zip(cols, values)))
        return rows

    def write(self, db: str, points: list[tuple[str, dict]], ts: int | None = None) -> None:
        ts = int(ts if ts is not None else time.time())
        lines = []
        for measurement, fields in points:
            fs = format_fields(fields)
            if fs:
                lines.append(f"{escape_key(measurement)} {fs} {ts}")
        if not lines:
            return
        params = {"db": db, "precision": "s", **self.auth}
        r = self.session.post(f"{self.base}/write", params=params,
                              data="\n".join(lines).encode(), timeout=self.timeout)
        r.raise_for_status()


def escape_key(s) -> str:
    return str(s).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def format_fields(fields: dict) -> str:
    out = []
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, bool):
            val = "true" if v else "false"
        elif isinstance(v, int):
            val = f"{v}i"
        elif isinstance(v, float):
            if not math.isfinite(v):
                continue
            val = repr(v)
        else:
            val = '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
        out.append(f"{escape_key(k)}={val}")
    return ",".join(out)


def mean_field(influx: Influx, db: str, measurement: str, fld: str, minutes: int) -> float | None:
    rows = influx.query(db, f'SELECT MEAN("{fld}") FROM "{measurement}" WHERE time > now() - {minutes}m')
    if rows and rows[0].get("mean") is not None:
        return float(rows[0]["mean"])
    return None


def solar_slope_w_per_s(influx: Influx, db: str, minutes: int) -> float | None:
    rows = influx.query(db, (
        'SELECT DERIVATIVE(MEAN("pv_p"), 1s) FROM "sunpower_power" '
        f'WHERE time > now() - {minutes}m GROUP BY time(1m) fill(null)'))
    vals = [r["derivative"] for r in rows if r.get("derivative") is not None]
    return (sum(vals) / len(vals)) * 1000.0 if vals else None


# --- OpenEVSE status -> InfluxDB fields -------------------------------------------------------

def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    return None


def openevse_fields(st: dict) -> dict:
    """Normalise /status (v5.1.5 default scaling) into SI units."""
    def temp(key):
        v = _num(st.get(key))
        return None if v is None else float(v) / 10.0

    amp = _num(st.get("amp"))
    state = _num(st.get("state"))
    f = {
        "current_a": None if amp is None else float(amp) / 1000.0,
        "voltage_v": _num(st.get("voltage")) and float(st["voltage"]),
        "power_w": _num(st.get("power")) and float(st["power"]),
        "pilot_a": _num(st.get("pilot")),
        "max_current_a": _num(st.get("max_current")),
        "state": state,
        "state_name": EVSE_STATE_NAMES.get(state, str(state)) if state is not None else None,
        "fault": is_fault_state(state),
        "active": st.get("status") == "active",
        "vehicle": _num(st.get("vehicle")),
        "manual_override": _num(st.get("manual_override")),
        "divertmode": _num(st.get("divertmode")),
        "divert_active": bool(st.get("divert_active")) if "divert_active" in st else None,
        "charge_rate_a": _num(st.get("charge_rate")),
        "divert_update_s": _num(st.get("divert_update")),
        "grid_ie_w": _num(st.get("grid_ie")),
        "temp_c": temp("temp"),
        "temp_max_c": temp("temp_max"),
        "session_energy_wh": _num(st.get("session_energy")),
        "session_elapsed_s": _num(st.get("session_elapsed")),
        "total_energy_kwh": _num(st.get("total_energy")),
        "total_day_kwh": _num(st.get("total_day")),
        "total_week_kwh": _num(st.get("total_week")),
        "total_month_kwh": _num(st.get("total_month")),
        "total_year_kwh": _num(st.get("total_year")),
        "gfci_count": _num(st.get("gfcicount")),
        "no_ground_count": _num(st.get("nogndcount")),
        "stuck_relay_count": _num(st.get("stuckcount")),
        "wifi_rssi": _num(st.get("srssi")),
        "free_heap": _num(st.get("free_heap")),
    }
    # keep int/float types stable across writes (InfluxDB rejects type changes per field)
    for k in ("voltage_v", "power_w", "temp_c", "temp_max_c", "current_a",
              "session_energy_wh", "total_energy_kwh", "total_day_kwh", "total_week_kwh",
              "total_month_kwh", "total_year_kwh", "grid_ie_w"):
        if f.get(k) is not None:
            f[k] = float(f[k])
    for k in ("pilot_a", "max_current_a", "state", "vehicle", "manual_override", "divertmode",
              "charge_rate_a", "divert_update_s", "session_elapsed_s", "gfci_count",
              "no_ground_count", "stuck_relay_count", "wifi_rssi", "free_heap"):
        if f.get(k) is not None:
            f[k] = int(f[k])
    return {k: v for k, v in f.items() if v is not None}


# --- main loop ----------------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="TOU policy + InfluxDB logging for OpenEVSE solar divert")
    p.add_argument("--openevse-host", required=True, help="OpenEVSE hostname or IP")
    p.add_argument("--openevse-user", default=None, help="OpenEVSE web UI username (if auth is enabled)")
    p.add_argument("--openevse-pass", default=None)
    p.add_argument("--claim-client-id", type=int, default=DEFAULT_CLAIM_CLIENT_ID)

    p.add_argument("--influxdb-host", default="localhost")
    p.add_argument("--influxdb-port", type=int, default=8086)
    p.add_argument("--influxdb-user", default=None)
    p.add_argument("--influxdb-pass", default=None)
    p.add_argument("--influxdb-db", default="pvs6", help="DB holding sunpower_power; also written to")


    p.add_argument("--interval", type=int, default=60, help="Seconds between loops (default 60)")
    p.add_argument("--average-minutes", type=int, default=5, help="Window for production/net means")
    p.add_argument("--slope-window", type=int, default=30, help="Minutes for the legacy solar slope field")

    p.add_argument("--offpeak-grid-tolerance", type=float, default=500.0)
    p.add_argument("--peak-pv-ratio", type=float, default=DEFAULT_PEAK_PV_RATIO)
    p.add_argument("--part-peak-pv-ratio", type=float, default=DEFAULT_PART_PEAK_PV_RATIO)
    p.add_argument("--off-peak-pv-ratio", type=float, default=None,
                   help="Default: derived from --offpeak-grid-tolerance")
    p.add_argument("--no-ratio-tuning", action="store_true", help="Never change divert_PV_ratio")
    p.add_argument("--no-overnight-months", type=int, nargs="*", default=DEFAULT_NO_OVERNIGHT_MONTHS,
                   metavar="MONTH", help="Months with no overnight grid charging (empty list = always allow)")
    p.add_argument("--overnight-periods", nargs="+", default=["off_peak"],
                   choices=["off_peak", "part_peak", "peak"])
    p.add_argument("--night-production-watts", type=float, default=500.0)
    p.add_argument("--stale-feed-seconds", type=float, default=120.0)

    p.add_argument("--no-control", action="store_true", help="Log only; never change the charger")
    p.add_argument("--dry-run", action="store_true", help="Log decisions but don't apply them")
    p.add_argument("--log-file", default=None)
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def setup_logging(log_file, quiet):
    handlers = []
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    if not quiet or not handlers:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)


class Manager:
    def __init__(self, args, evse: OpenEVSE, influx: Influx):
        self.args = args
        self.evse = evse
        self.influx = influx
        self.cfg = PolicyConfig(
            offpeak_grid_tolerance=args.offpeak_grid_tolerance,
            peak_pv_ratio=args.peak_pv_ratio,
            part_peak_pv_ratio=args.part_peak_pv_ratio,
            off_peak_pv_ratio=args.off_peak_pv_ratio,
            no_overnight_months=set(args.no_overnight_months or []),
            overnight_periods=set(args.overnight_periods),
            night_production_w=args.night_production_watts,
            stale_feed_seconds=args.stale_feed_seconds,
            tune_ratio=not args.no_ratio_tuning,
        )
        self.evse_config: dict = {}
        self.config_fetched_at = 0.0
        self.last_reason = None

    def refresh_config(self, force=False):
        # Re-read every 5 min so a change made in the web UI (e.g. picking a filter preset,
        # which also rewrites divert_PV_ratio) is noticed and the TOU ratio re-applied.
        if force or time.time() - self.config_fetched_at > 300:
            self.evse_config = self.evse.config()
            self.config_fetched_at = time.time()
            if self.evse_config.get("divert_type") not in (1, None):
                logging.warning("OpenEVSE divert_type is %s; set Self Production mode to Export (1) so it uses the "
                                "grid_ie feed from pvs6_relay.", self.evse_config.get("divert_type"))

    def apply(self, d: Decision, st: dict):
        cid = self.args.claim_client_id
        if d.pv_ratio is not None:
            current = self.evse_config.get("divert_PV_ratio")
            if current is None or abs(float(current) - d.pv_ratio) > 0.005:
                logging.info("Setting divert_PV_ratio %s -> %.3f (%s)", current, d.pv_ratio, d.period)
                self.evse.set_config({"divert_PV_ratio": d.pv_ratio})
                self.evse_config["divert_PV_ratio"] = d.pv_ratio

        existing = self.evse.get_claim(cid)
        if d.claim is None:
            if existing:
                logging.info("Releasing claim (%s)", d.reason)
                self.evse.release_claim(cid)
            return
        want = {"state": d.claim, "auto_release": False}
        if d.charge_current is not None:
            want["charge_current"] = int(d.charge_current)
        same = existing and existing.get("state") == want["state"] and \
            existing.get("charge_current") == want.get("charge_current")
        if not same:
            logging.info("Claiming %s (%s)", want, d.reason)
            self.evse.set_claim(cid, want)

    def step(self):
        now = int(time.time())
        now_pt = datetime.now(PACIFIC)
        st = self.evse.status()
        self.refresh_config()

        db = self.args.influxdb_db
        avg = self.args.average_minutes
        production_w = net_w = slope = None
        try:
            pv = mean_field(self.influx, db, "sunpower_power", "pv_p", avg)
            net = mean_field(self.influx, db, "sunpower_power", "net_p", avg)
            production_w = None if pv is None else pv * 1000.0
            net_w = None if net is None else net * 1000.0
            slope = solar_slope_w_per_s(self.influx, db, self.args.slope_window)
        except Exception as e:
            logging.warning("InfluxDB PVS6 query failed: %s", e)

        min_current = float(self.evse_config.get("min_current_hard") or 6)
        max_current = float(self.evse_config.get("max_current_soft") or st.get("max_current") or 32)
        inp = PolicyInputs(
            now_pt=now_pt,
            divertmode=st.get("divertmode"),
            feed_age_s=st.get("divert_update"),
            production_w=production_w,
            min_current=min_current,
            max_current=max_current,
            voltage=float(st.get("voltage") or 240),
        )
        d = decide(inp, self.cfg)
        if d.reason != self.last_reason:
            logging.info("Policy: %s", d.reason)
            self.last_reason = d.reason

        applied = False
        if not self.args.no_control and not self.args.dry_run:
            try:
                self.apply(d, st)
                applied = True
            except Exception as e:
                logging.error("Failed to apply decision: %s", e)

        # ---- logging ----
        ev = openevse_fields(st)
        car_w = ev.get("power_w", 0.0) if ev.get("state") == 3 else 0.0
        policy = {
            "tou_period": d.period,
            "pv_ratio": None if d.pv_ratio is None else float(d.pv_ratio),
            "claim": d.claim or "none",
            "reason": d.reason,
            "applied": applied,
            "production_w": production_w,
            "net_w": net_w,
            "feed_age_s": None if inp.feed_age_s is None else int(inp.feed_age_s),
        }
        points = [("openevse", ev), ("openevse_policy", policy)]
        # Legacy measurement, kept field-compatible with solar_charge_controller.py
        if net_w is not None:
            legacy = {
                "excess_solar_watts": float(-(net_w - car_w)),
                "charging_power_watts": float(car_w),
                "target_amperage": int(st.get("charge_rate") or 0) if st.get("divert_active")
                else (int(st.get("pilot") or 0) if ev.get("state") == 3 else 0),
                "current_amperage": int(st.get("pilot") or 0),
            }
            if slope is not None:
                legacy["solar_slope_w_per_s"] = float(slope)
            points.append(("solar_charge_control", legacy))
        try:
            self.influx.write(db, points, ts=now)
        except Exception as e:
            logging.warning("InfluxDB write failed: %s", e)
        return d


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.log_file, args.quiet)
    evse = OpenEVSE(args.openevse_host, args.openevse_user, args.openevse_pass)
    influx = Influx(args.influxdb_host, args.influxdb_port, args.influxdb_user, args.influxdb_pass)
    mgr = Manager(args, evse, influx)
    mode = "log-only" if args.no_control else ("dry-run" if args.dry_run else "control")
    logging.info("openevse_manager starting (%s) for %s", mode, args.openevse_host)
    while True:
        try:
            mgr.step()
        except requests.RequestException as e:
            logging.warning("OpenEVSE unreachable: %s", e)
        except Exception:
            logging.exception("Unexpected error in main loop")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())

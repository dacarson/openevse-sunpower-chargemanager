import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openevse_manager as m  # noqa: E402
from openevse_client import OpenEVSE  # noqa: E402

PT = m.PACIFIC


def at(month, hour, minute=0):
    return PT.localize(datetime(2026, month, 15, hour, minute))


def inputs(now, production=3000.0, divertmode=2, feed_age=3, min_a=6, max_a=48, v=240):
    return m.PolicyInputs(now_pt=now, divertmode=divertmode, feed_age_s=feed_age,
                          production_w=production, min_current=min_a, max_current=max_a, voltage=v)


class RatioTests(unittest.TestCase):
    def test_off_peak_tolerance(self):
        # 6A * 240V = 1440W min; tolerate 500W of grid draw -> start with 940W excess
        self.assertAlmostEqual(m.ratio_for_grid_tolerance(500, 1440), 0.653, places=3)
        self.assertEqual(m.ratio_for_grid_tolerance(0, 1440), 1.0)
        self.assertEqual(m.ratio_for_grid_tolerance(5000, 1440), 0.05)

    def test_peak_multiplier(self):
        # 2x -> keep 1440W exported -> ratio 2.44
        self.assertAlmostEqual(m.ratio_for_excess_multiplier(2.0, 1440), 2.44, places=3)
        self.assertEqual(m.ratio_for_excess_multiplier(1.0, 1440), 1.0)

    def test_part_peak(self):
        self.assertAlmostEqual(m.ratio_for_part_peak(6), 0.917, places=3)


class TouTests(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(m.get_tou_period(at(11, 2)), "off_peak")
        self.assertEqual(m.get_tou_period(at(11, 14, 59)), "off_peak")
        self.assertEqual(m.get_tou_period(at(11, 15)), "part_peak")
        self.assertEqual(m.get_tou_period(at(11, 16)), "peak")
        self.assertEqual(m.get_tou_period(at(11, 20, 59)), "peak")
        self.assertEqual(m.get_tou_period(at(11, 21)), "part_peak")


class DecideTests(unittest.TestCase):
    cfg = m.PolicyConfig()

    def test_daytime_divert(self):
        d = m.decide(inputs(at(11, 12)), self.cfg)
        self.assertIsNone(d.claim)
        self.assertEqual(d.period, "off_peak")
        self.assertAlmostEqual(d.pv_ratio, 0.653, places=3)

    def test_peak_ratio(self):
        d = m.decide(inputs(at(11, 17)), self.cfg)
        self.assertIsNone(d.claim)
        self.assertAlmostEqual(d.pv_ratio, 2.44, places=3)

    def test_overnight_charging(self):
        d = m.decide(inputs(at(11, 2), production=0), self.cfg)
        self.assertEqual(d.claim, "active")
        self.assertEqual(d.charge_current, 48)

    def test_no_overnight_month(self):
        d = m.decide(inputs(at(7, 2), production=0), self.cfg)
        self.assertIsNone(d.claim)

    def test_dark_peak_no_claim(self):
        d = m.decide(inputs(at(11, 19), production=0), self.cfg)
        self.assertIsNone(d.claim)

    def test_dark_part_peak_not_overnight_by_default(self):
        d = m.decide(inputs(at(11, 22), production=0), self.cfg)
        self.assertIsNone(d.claim)
        cfg = m.PolicyConfig(overnight_periods={"off_peak", "part_peak"})
        self.assertEqual(m.decide(inputs(at(11, 22), production=0), cfg).claim, "active")

    def test_stale_feed_pauses(self):
        d = m.decide(inputs(at(11, 12), feed_age=600), self.cfg)
        self.assertEqual(d.claim, "disabled")
        d = m.decide(inputs(at(11, 12), feed_age=None), self.cfg)
        self.assertEqual(d.claim, "disabled")

    def test_overnight_beats_stale_feed(self):
        d = m.decide(inputs(at(11, 2), production=0, feed_age=9999), self.cfg)
        self.assertEqual(d.claim, "active")

    def test_normal_mode_hands_off(self):
        d = m.decide(inputs(at(11, 2), production=0, divertmode=1, feed_age=9999), self.cfg)
        self.assertIsNone(d.claim)
        self.assertIsNone(d.pv_ratio)

    def test_unknown_production_no_overnight(self):
        d = m.decide(inputs(at(11, 2), production=None), self.cfg)
        self.assertIsNone(d.claim)


class FieldTests(unittest.TestCase):
    def test_scaling(self):
        st = {"amp": 31500, "voltage": 241, "power": 7591, "pilot": 32, "state": 3,
              "status": "active", "temp": 345, "temp_max": False, "session_energy": 1234.5,
              "total_energy": 456.7, "divert_active": True, "charge_rate": 18, "divertmode": 2}
        f = m.openevse_fields(st)
        self.assertEqual(f["current_a"], 31.5)
        self.assertEqual(f["temp_c"], 34.5)
        self.assertNotIn("temp_max_c", f)
        self.assertEqual(f["state_name"], "charging")
        self.assertIsInstance(f["voltage_v"], float)
        self.assertFalse(f["fault"])
        self.assertTrue(m.openevse_fields({"state": 6})["fault"])

    def test_line_protocol(self):
        s = m.format_fields({"a": 1, "b": 1.5, "c": "x y", "d": True, "e": None, "f": float("nan")})
        self.assertEqual(s, 'a=1i,b=1.5,c="x y",d=true')


# --- mock OpenEVSE -----------------------------------------------------------------------------

class FakeEVSE:
    def __init__(self):
        self.status = {"state": 254, "status": "disabled", "divertmode": 2, "divert_update": 2,
                       "voltage": 240, "pilot": 48, "max_current": 48, "amp": 0, "power": 0}
        self.config = {"divert_type": 1, "divert_PV_ratio": 1.1, "min_current_hard": 6,
                       "max_current_soft": 48}
        self.claims = {}
        self.grid_ie = []
        self.config_posts = []


def make_handler(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n).decode()

        def do_GET(self):
            if self.path == "/status":
                return self._send(200, fake.status)
            if self.path == "/config":
                return self._send(200, fake.config)
            if self.path.startswith("/claims/"):
                cid = self.path.split("/")[2]
                if cid in fake.claims:
                    return self._send(200, fake.claims[cid])
                return self._send(404, {"msg": "Not found"})
            self._send(404, {})

        def do_POST(self):
            body = self._body()
            if self.path == "/status":
                fake.grid_ie.append(json.loads(body)["grid_ie"])
                return self._send(200, {"msg": "done"})
            if self.path == "/config":
                v = json.loads(body)
                fake.config.update(v)
                fake.config_posts.append(v)
                return self._send(200, {"msg": "done"})
            if self.path.startswith("/claims/"):
                fake.claims[self.path.split("/")[2]] = json.loads(body)
                return self._send(200, {"msg": "done"})
            self._send(404, {})

        def do_DELETE(self):
            cid = self.path.split("/")[2]
            if fake.claims.pop(cid, None) is None:
                return self._send(404, {"msg": "Not found"})
            self._send(200, {"msg": "done"})
    return H


class FakeInflux:
    def __init__(self, pv_kw=0.0, net_kw=0.3):
        self.pv_kw, self.net_kw = pv_kw, net_kw
        self.writes = []

    def query(self, db, q):
        if "DERIVATIVE" in q:
            return [{"derivative": 0.0}]
        if '"pv_p"' in q:
            return [{"mean": self.pv_kw}]
        if '"net_p"' in q:
            return [{"mean": self.net_kw}]
        return []

    def write(self, db, points, ts=None):
        self.writes.append((db, points))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEVSE()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.fake))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()

    def manager(self, influx, extra=()):
        args = m.parse_args(["--openevse-host", self.host, *extra])
        return m.Manager(args, OpenEVSE(self.host), influx)

    def test_overnight_claim_then_release(self):
        influx = FakeInflux(pv_kw=0.0)
        mgr = self.manager(influx)
        mgr.cfg.no_overnight_months = set()
        orig = m.datetime

        class FixedDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return at(11, 2)
        m.datetime = FixedDT
        try:
            d = mgr.step()
            self.assertEqual(d.claim, "active")
            cid = str(m.DEFAULT_CLAIM_CLIENT_ID)
            self.assertEqual(self.fake.claims[cid],
                             {"state": "active", "auto_release": False, "charge_current": 48})
            self.assertEqual(self.fake.config["divert_PV_ratio"], 0.653)
            # second step: idempotent, no extra config writes
            mgr.step()
            self.assertEqual(len(self.fake.config_posts), 1)
            # sunrise -> claim released, divert takes over
            influx.pv_kw = 2.0
            d = mgr.step()
            self.assertIsNone(d.claim)
            self.assertNotIn(cid, self.fake.claims)
            # feed goes stale -> paused
            self.fake.status["divert_update"] = 900
            d = mgr.step()
            self.assertEqual(self.fake.claims[cid]["state"], "disabled")
        finally:
            m.datetime = orig
        names = [p[0] for _, pts in influx.writes for p in pts]
        self.assertIn("openevse", names)
        self.assertIn("solar_charge_control", names)
        policy = [p[1] for _, pts in influx.writes for p in pts if p[0] == "openevse_policy"][0]
        self.assertNotIn("meter_diff_w", policy)
        self.assertNotIn("eagle_net_w", policy)
        self.assertEqual(policy["net_w"], 300.0)

    def test_no_control_never_writes(self):
        influx = FakeInflux(pv_kw=0.0)
        mgr = self.manager(influx, ["--no-control"])
        mgr.step()
        self.assertEqual(self.fake.claims, {})
        self.assertEqual(self.fake.config_posts, [])

    def test_feeder_averages(self):
        import importlib
        sys.argv = ["x"]
        logger = importlib.import_module("pvs6_relay")
        f = logger.OpenEVSEGridFeeder(OpenEVSE(self.host), interval=1.0)
        for kw in (-2.0, -3.0, -4.0):
            f.add_sample(kw)
        f.add_sample("bad")
        deadline = time.time() + 3
        while not self.fake.grid_ie and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.fake.grid_ie[0], -3000)


class RelayLoggingIndependenceTests(unittest.TestCase):
    """pvs6_relay must log every PVS6 sample to InfluxDB whatever state OpenEVSE is in."""

    def _run_messages(self, evse_host, n=20):
        import importlib
        sys.argv = ["x"]
        relay = importlib.import_module("pvs6_relay")
        published = []
        relay.args = SimpleNamespace(raw=False, verbose=False, influxdb=True)
        orig_pub = relay.influxdb_publish
        relay.influxdb_publish = lambda m, f, timestamp=None: published.append((m, f.get("net_p")))
        relay.feeder = relay.OpenEVSEGridFeeder(OpenEVSE(evse_host, timeout=3), interval=1.0)
        try:
            start = time.time()
            for i in range(n):
                relay.on_message(None, json.dumps(
                    {"notification": "power", "params": {"time": 1700000000 + i, "net_p": -1.5, "pv_p": 3.0}}))
            elapsed = time.time() - start
            time.sleep(1.5)  # let the feeder attempt (and fail) a POST
            for i in range(n):
                relay.on_message(None, json.dumps(
                    {"notification": "power", "params": {"time": 1700001000 + i, "net_p": -1.5}}))
        finally:
            relay.influxdb_publish = orig_pub
            relay.feeder = None
        return published, elapsed

    def test_logs_when_openevse_refuses_connections(self):
        published, elapsed = self._run_messages("127.0.0.1:1")
        self.assertEqual(len(published), 40)
        self.assertLess(elapsed, 0.5)

    def test_logs_when_openevse_hangs(self):
        import socket
        hang = socket.socket()
        hang.bind(("127.0.0.1", 0))
        hang.listen(1)  # accepts TCP but never answers HTTP
        try:
            published, elapsed = self._run_messages(f"127.0.0.1:{hang.getsockname()[1]}")
        finally:
            hang.close()
        self.assertEqual(len(published), 40)
        self.assertLess(elapsed, 0.5)

    def test_logs_without_openevse_configured(self):
        import importlib
        sys.argv = ["x"]
        relay = importlib.import_module("pvs6_relay")
        published = []
        relay.args = SimpleNamespace(raw=False, verbose=False, influxdb=True)
        orig_pub = relay.influxdb_publish
        relay.influxdb_publish = lambda m, f, timestamp=None: published.append(m)
        relay.feeder = None
        try:
            relay.on_message(None, json.dumps({"notification": "power", "params": {"net_p": 0.2}}))
        finally:
            relay.influxdb_publish = orig_pub
        self.assertEqual(published, ["sunpower_power"])


if __name__ == "__main__":
    unittest.main()

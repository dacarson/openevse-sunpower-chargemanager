"""
Minimal client for the OpenEVSE WiFi firmware local HTTP API (tested against v5.1.5).

Only the endpoints this project needs are wrapped. Notes on v5.1.5 behaviour that
the callers rely on:

* GET  /status          -> live state. `amp` is in mA, `temp*` in 0.1 degC,
                           `power` in W, `session_energy` in Wh, `total_energy` in kWh,
                           `divert_update` = seconds since the last solar/grid_ie update.
* POST /status          -> {"grid_ie": W} feeds solar divert (positive = importing,
                           negative = exporting) and immediately re-runs the divert
                           calculation. No MQTT broker is required.
* GET/POST /config      -> settings. POST only writes flash when a value changes.
* GET/POST/DELETE /claims/<client_id>
                        -> an external "claim" on the EVSE state/current. POSTed
                           claims always get EvseManager_Priority_API (500): higher
                           than divert (50) and schedule timers (100), lower than a
                           manual override (1000), OCPP (1050) and safety limits.
* POST /divertmode      -> form param divertmode=1 (normal) or 2 (eco). Runtime only;
                           the boot-time mode comes from the config flags.
"""

from __future__ import annotations

import requests

# The firmware parses the client id with String::toInt() (a signed 32-bit long), so the
# id must stay below 2**31. Vendor ids 1-3 are registered (OpenEVSE, OpenEnergyMonitor,
# BigJungle); 0x00FF is unused, so EVC(0x00FF, 0x0001) is safe from collisions.
DEFAULT_CLAIM_CLIENT_ID = (0x00FF << 16) | 0x0001  # 16711681

DIVERT_MODE_NORMAL = 1
DIVERT_MODE_ECO = 2

# EVSE controller states (`state` in /status)
EVSE_STATE_NAMES = {
    0: "starting",
    1: "not_connected",
    2: "connected",
    3: "charging",
    4: "vent_required",
    5: "diode_check_failed",
    6: "gfci_fault",
    7: "no_ground",
    8: "stuck_relay",
    9: "gfci_self_test_failed",
    10: "over_temperature",
    11: "over_current",
    254: "sleeping",
    255: "disabled",
}


def is_fault_state(state: int | None) -> bool:
    return state is not None and 4 <= int(state) <= 11


class OpenEVSEError(Exception):
    pass


class OpenEVSE:
    def __init__(self, host: str, username: str | None = None, password: str | None = None,
                 timeout: float = 10.0, session: requests.Session | None = None):
        if host.startswith("http://") or host.startswith("https://"):
            self.base = host.rstrip("/")
        else:
            self.base = f"http://{host}"
        self.timeout = timeout
        self.session = session or requests.Session()
        if username:
            self.session.auth = (username, password or "")

    # -- helpers -----------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _get_json(self, path: str):
        r = self.session.get(self._url(path), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # -- status / config -----------------------------------------------------------
    def status(self) -> dict:
        return self._get_json("/status")

    def config(self) -> dict:
        return self._get_json("/config")

    def set_config(self, values: dict) -> None:
        r = self.session.post(self._url("/config"), json=values, timeout=self.timeout)
        r.raise_for_status()

    def post_grid_ie(self, watts: float) -> None:
        r = self.session.post(self._url("/status"), json={"grid_ie": int(round(watts))},
                              timeout=self.timeout)
        r.raise_for_status()

    def set_divert_mode(self, mode: int) -> None:
        r = self.session.post(self._url("/divertmode"), data={"divertmode": str(int(mode))},
                              timeout=self.timeout)
        r.raise_for_status()

    # -- claims ----------------------------------------------------------------------
    def get_claim(self, client_id: int) -> dict | None:
        r = self.session.get(self._url(f"/claims/{client_id}"), timeout=self.timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def set_claim(self, client_id: int, props: dict) -> None:
        r = self.session.post(self._url(f"/claims/{client_id}"), json=props, timeout=self.timeout)
        if r.status_code >= 400:
            raise OpenEVSEError(f"claim rejected ({r.status_code}): {r.text[:200]}")

    def release_claim(self, client_id: int) -> bool:
        r = self.session.delete(self._url(f"/claims/{client_id}"), timeout=self.timeout)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

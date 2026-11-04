# OpenEVSE + SunPower solar charge manager

This replaces [ChargePoint-SunPower-ChargeManager](https://github.com/dacarson/ChargePoint-SunPower-ChargeManager) for an **OpenEVSE** charger running WiFi firmware **5.1.5**.

The old controller did all of the solar-following itself through ChargePoint's cloud API, and had to stop and restart a session to change the amperage. OpenEVSE has a built-in **Eco (solar divert)** mode that changes the pilot current locally and immediately. So the work is split:

| Piece | Job |
|---|---|
| **OpenEVSE Eco mode** (firmware) | Follows excess solar every few seconds. It adds back the car's own draw, smooths the signal (20 s when solar rises, 600 s when it falls), enforces a minimum charge time and adds hysteresis. |
| **`pvs6_ws_logger.py`** | As before, streams the PVS6 to InfluxDB. **New:** it also posts the grid reading (`net_p`) to the charger as `grid_ie` every 5 s. This is what drives Eco mode, so **no MQTT broker is needed**. |
| **`openevse_manager.py`** | Runs once a minute. It handles TOU tuning, overnight charging, a stale-feed failsafe, and logging to InfluxDB. |

## Why the PVS6 drives divert, not the Eagle

The PVS6 websocket updates about every second. The Eagle relays the PG&E meter's Zigbee pushes and has to be polled gently (10–30 s), so its readings are often 10–60 s old by the time they're used. Eco mode reacts to every update, and a lagging feed makes it over- or under-shoot. The PVS6 consumption CTs aren't revenue-grade, but an error of a percent or two is far smaller than OpenEVSE's 1 A (~240 W) step. `openevse_manager.py` logs the difference between the two meters so you can watch for CT drift.

## What `openevse_manager.py` does

**TOU tuning of `divert_PV_ratio`** (it writes `/config` only when the TOU period changes):

| Period (PG&E) | Old controller | Now |
|---|---|---|
| Off-Peak (before 3 pm) | Tolerate 500 W of grid draw before stopping | `ratio = (min_power − 500 W) / min_power`, which is ≈ 0.65 at 6 A |
| Part-Peak (3–4 pm, 9 pm–midnight) | Need `(min_A − 0.5) × 240 V` of excess | `ratio = (min_A − 0.5) / min_A`, which is ≈ 0.92 |
| Peak (4–9 pm) | Need 2 × the minimum excess | `ratio = 1 + min_power / 1000`, which is ≈ 2.44 and keeps one minimum-charge's worth of solar (~1.44 kW) exported |

For the firmware math behind these, see the comment block in `openevse_manager.py`. One behaviour differs from the old controller: during Peak, the reserve stays exported even while charging. The old controller let the car use all the excess once it had started. You can override any of these with `--peak-pv-ratio`, `--part-peak-pv-ratio` or `--off-peak-pv-ratio`.

**Overnight charging.** During Off-Peak, if the 5-minute mean PV production is under 500 W, the script claims the charger *active* at the configured max current. It doesn't do this in `--no-overnight-months`, which defaults to June–September. At sunrise it releases the claim and Eco mode takes over again. This replaces the old "ChargePoint schedule window" check.

**Stale-feed failsafe.** Firmware 5.1.5 keeps the last `grid_ie` value forever. If `divert_update` (the time since the last feed update) passes 120 s while in Eco mode, the script claims the charger *disabled* until the feed resumes.

**Manual override always wins.** Claims from this script use the firmware's API priority (500). That's above Eco divert (50) and schedule timers (100), and below a **manual override** (1000) from the OpenEVSE UI or HomeKit. A manual override clears itself when you unplug, so the old "detect a manual 40 A setting" logic isn't needed.

**Hands off in Normal mode.** If you switch the charger out of Eco mode, the script releases its claim and leaves the charger alone.

### InfluxDB measurements (in `--influxdb-db`, default `pvs6`)

| Measurement | Fields |
|---|---|
| `openevse` | `current_a`, `voltage_v`, `power_w`, `pilot_a`, `state`, `state_name`, `fault`, `vehicle`, `manual_override`, `divertmode`, `divert_active`, `charge_rate_a`, `divert_update_s`, `grid_ie_w`, `temp_c`, `session_energy_wh`, `total_energy_kwh`, `total_day/week/month/year_kwh`, fault counters, `wifi_rssi`, … |
| `openevse_policy` | `tou_period`, `pv_ratio`, `claim`, `reason`, `production_w`, `net_w`, `feed_age_s`, `eagle_net_w`, `meter_diff_w` (PVS6 − Eagle) |
| `solar_charge_control` | Same fields as the old controller, so existing Grafana panels keep working: `excess_solar_watts`, `charging_power_watts`, `target_amperage`, `current_amperage`, `solar_slope_w_per_s` |

The ML shadow logger (`model_shadow_logger.py`, `model_run5`) isn't carried over. It predicted ChargePoint amp decisions for the old controller's policy, and charge decisions are now made by the firmware.

## OpenEVSE setup (web UI)

1. **Settings → Solar divert.** Set **Divert type** to **Grid (import/export)** explicitly. In 5.1.5, the upgrade code meant to pick this automatically uses `==` instead of `=`, so it never sets it. Leave the solar/grid MQTT topics blank.
2. Enable divert and set the default charge mode to **Eco**, so the charger boots into Eco mode.
3. Leave the default smoothing settings (attack 20 s, decay 600 s, min charge time 600 s) unless you see stop/start cycling.
4. Don't create schedule timers for overnight charging. They outrank Eco mode, and the overnight logic is handled by `openevse_manager.py`.
5. Turn off the car's own charging schedule, so it charges whenever the charger allows it.

## Install

```bash
git clone https://github.com/dacarson/openevse-sunpower-chargemanager.git ~/openevse-sunpower-chargemanager
cd ~/openevse-sunpower-chargemanager
python3 -m venv venv && venv/bin/pip install -r requirements.txt

sudo systemctl disable --now solar_charge_controller model_shadow_logger   # retire the ChargePoint controller
sudo cp etc/systemd/system/*.service /etc/systemd/system/
sudo cp etc/default/pvs6_ws_logger etc/default/openevse_manager /etc/default/
sudoedit /etc/default/pvs6_ws_logger /etc/default/openevse_manager
sudo systemctl daemon-reload
sudo systemctl enable --now pvs6_ws_logger openevse_manager
```

Start `openevse_manager` with `--dry-run` for a day. It logs every decision and writes InfluxDB, but never changes the charger. `--no-control` makes it a pure logger.

To find the Eagle measurement and field names for the cross-check:

```sql
SHOW MEASUREMENTS ON eagle
SHOW FIELD KEYS ON eagle FROM "device_<hw_addr>"    -- look for ..._InstantaneousDemand (kW)
```

### `openevse_manager.py` options

| Option | Default | |
|---|---|---|
| `--openevse-host` | required | Hostname or IP |
| `--openevse-user` / `--openevse-pass` | | Only if web UI auth is enabled |
| `--influxdb-host/port/user/pass/db` | localhost / 8086 / – / – / pvs6 | |
| `--eagle-db`, `--eagle-measurement`, `--eagle-field`, `--eagle-scale` | – / – / `Main_InstantaneousDemand` / 1000 | Enables the meter cross-check |
| `--interval` | 60 | Seconds between loops |
| `--average-minutes` | 5 | Window for production/net means |
| `--peak-excess-multiplier` | 2.0 | |
| `--offpeak-grid-tolerance` | 500 | W |
| `--peak-pv-ratio`, `--part-peak-pv-ratio`, `--off-peak-pv-ratio` | derived | Explicit overrides |
| `--no-ratio-tuning` | | Never touch `divert_PV_ratio` |
| `--no-overnight-months` | 6 7 8 9 | Pass with no values to always allow |
| `--overnight-periods` | off_peak | Add `part_peak` to also charge 9 pm–midnight |
| `--night-production-watts` | 500 | "Dark" threshold for overnight charging |
| `--stale-feed-seconds` | 120 | Failsafe threshold |
| `--dry-run`, `--no-control` | | |
| `--log-file`, `--quiet` | | |

### New `pvs6_ws_logger.py` options

| Option | Default | |
|---|---|---|
| `--openevse_host` | – | Enables the `grid_ie` feed |
| `--openevse_user` / `--openevse_pass` | | |
| `--openevse_interval` | 5 | Seconds; samples are averaged over each interval. Minimum 1 s, because the firmware's smoothing counts time between updates in whole seconds. |
| `--influxdb_user` / `--influxdb_pass` | | Now accepted, matching the documented `/etc/default` example |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

These cover the TOU/ratio mapping, the policy decisions, field scaling, and a mock OpenEVSE server that exercises claims, config writes and the `grid_ie` feeder.

## License

MIT

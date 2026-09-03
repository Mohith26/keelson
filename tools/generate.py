"""Seeded generator for a synthetic wind fleet.

Everything downstream (tests, benchmarks, the demo) uses this so that a run is
reproducible from a seed alone. The sensor signals are not white noise: each
channel is a slow random walk around a plausible physical setpoint with a
diurnal component, which matters because the time series codec's compression
ratio is entirely a function of how smooth the signal is. Feeding it uniform
random doubles would make the codec look far worse than it is on real
telemetry, and feeding it a constant would make it look far better.
"""

import math
import random

REGIONS = ["north", "south", "coastal", "highland"]
STATUSES = ["ACTIVE", "DERATED", "MAINTENANCE", "OFFLINE"]
MODELS = ["V112-3.0", "V150-4.2", "SG-5.0-145", "N149-4.0", "E-138-EP3"]
CHANNELS = [
    # (channel, unit, setpoint, drift per step, diurnal amplitude)
    ("nacelle_temp_c", "degC", 38.0, 0.12, 4.0),
    ("gearbox_oil_c", "degC", 62.0, 0.10, 2.5),
    ("vibration_rms_mm_s", "mm/s", 2.4, 0.02, 0.3),
    ("power_kw", "kW", 2100.0, 18.0, 420.0),
]
CATEGORIES = ["inspection", "gearbox", "blade", "converter", "yaw", "sensor"]

EPOCH = 1_735_689_600  # 2025-01-01T00:00:00Z


class Fleet:
    """A generated fleet, as plain row dicts ready to hand to a session."""

    def __init__(self, sites, turbines, substations, sensors, work_orders, series):
        self.sites = sites
        self.turbines = turbines
        self.substations = substations
        self.sensors = sensors
        self.work_orders = work_orders
        self.series = series

    def counts(self):
        return {
            "sites": len(self.sites),
            "turbines": len(self.turbines),
            "substations": len(self.substations),
            "sensors": len(self.sensors),
            "work_orders": len(self.work_orders),
            "series": len(self.series),
            "points": sum(len(p) for p in self.series.values()),
        }

    def load_into(self, session, oracle=None):
        session.type("Site").upsert(self.sites)
        session.type("Turbine").upsert(self.turbines)
        session.type("Substation").upsert(self.substations)
        session.type("Sensor").upsert(self.sensors)
        session.type("WorkOrder").upsert(self.work_orders)
        for (field, sensor_id), points in self.series.items():
            session.type("Sensor").append_series(field, sensor_id, points)
        session.flush()
        if oracle is not None:
            oracle.load("Site", self.sites)
            oracle.load("Turbine", self.turbines)
            oracle.load("Substation", self.substations)
            oracle.load("Sensor", self.sensors)
            oracle.load("WorkOrder", self.work_orders)
            for (field, sensor_id), points in self.series.items():
                oracle.load_series("Sensor", field, sensor_id, points)
        return self


def generate(
    seed=1,
    n_sites=6,
    turbines_per_site=12,
    substations_per_site=1,
    channels=None,
    points_per_series=0,
    cadence_s=600,
    work_orders_per_site=40,
):
    rnd = random.Random(seed)
    channels = CHANNELS if channels is None else channels

    sites, turbines, substations, sensors, work_orders = [], [], [], [], []
    series = {}

    for si in range(n_sites):
        site_id = "site-%03d" % si
        sites.append(
            {
                "id": site_id,
                "version": 1,
                "name": "Site %d" % si,
                "region": REGIONS[si % len(REGIONS)],
                "timezoneOffsetMin": rnd.choice([-480, -420, -360, -300, 0, 60]),
                "createdAt": EPOCH - rnd.randint(0, 5_000_000),
                "updatedBy": "loader",
            }
        )

        for ti in range(turbines_per_site):
            tid = "wtg-%03d-%03d" % (si, ti)
            model = MODELS[(si + ti) % len(MODELS)]
            rated = 3000.0 + 400.0 * ((si + ti) % 5)
            turbines.append(
                {
                    "id": tid,
                    "version": 1,
                    "siteId": site_id,
                    "serial": "SN-%05d" % (si * 1000 + ti),
                    "status": STATUSES[(si * 7 + ti * 3) % len(STATUSES)],
                    "commissionedOn": EPOCH - rnd.randint(0, 200_000_000),
                    "ratedPowerKw": rated,
                    "hubHeightM": 90.0 + 10.0 * (ti % 4),
                    "rotorDiameterM": 112.0 + 8.0 * (ti % 5),
                    "model": model,
                }
            )

            for ch, unit, setpoint, drift, diurnal in channels:
                sid = "%s-%s" % (tid, ch)
                sensors.append(
                    {
                        "id": sid,
                        "version": 1,
                        "assetId": tid,
                        "channel": ch,
                        "unit": unit,
                        "calibrationOffset": round(rnd.uniform(-0.5, 0.5), 3),
                    }
                )
                if points_per_series:
                    series[("readings", sid)] = _signal(
                        rnd, setpoint, drift, diurnal, points_per_series, cadence_s
                    )

        for bi in range(substations_per_site):
            substations.append(
                {
                    "id": "sub-%03d-%02d" % (si, bi),
                    "version": 1,
                    "siteId": site_id,
                    "serial": "SUB-%04d" % (si * 10 + bi),
                    "status": "ACTIVE",
                    "commissionedOn": EPOCH - rnd.randint(0, 200_000_000),
                    "capacityMva": rnd.choice([50.0, 75.0, 100.0]),
                    "voltageKv": rnd.choice([33.0, 66.0, 132.0]),
                }
            )

        for wi in range(work_orders_per_site):
            opened = EPOCH + rnd.randint(0, 30_000_000)
            duration = rnd.randint(30, 4000)
            asset = turbines[rnd.randrange(len(turbines))] if turbines else None
            work_orders.append(
                {
                    "id": "wo-%03d-%04d" % (si, wi),
                    "version": 1,
                    "siteId": site_id,
                    "assetId": asset["id"] if asset else "",
                    "openedAt": opened,
                    "closedAt": opened + duration * 60,
                    "priority": rnd.randint(1, 5),
                    "category": CATEGORIES[wi % len(CATEGORIES)],
                    "downtimeMinutes": duration,
                    "createdAt": opened,
                    "updatedBy": "scheduler",
                }
            )

    return Fleet(sites, turbines, substations, sensors, work_orders, series)


def _signal(rnd, setpoint, drift, diurnal, n, cadence_s):
    """A slow random walk plus a daily cycle, rounded like a real historian."""
    points = []
    ts = EPOCH
    value = setpoint
    day = 86400.0
    for i in range(n):
        ts += cadence_s
        value += rnd.uniform(-drift, drift)
        # Pull gently back toward the setpoint so the walk does not run away.
        value += (setpoint - value) * 0.01
        cycle = diurnal * math.sin(2.0 * math.pi * (ts % day) / day)
        points.append((ts, round(value + cycle, 3)))
    return points

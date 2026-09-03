"""Shared model sources for the tests."""

import os

TINY = """
mixin type Persistable {
  id: string
  version: int = 1
}

@db(index="region")
entity type Site mixes Persistable {
  name: string
  region: string
  turbines: [Turbine](siteId, id)
}

@db(unique="serial", index="status")
entity type Turbine mixes Persistable schema name "WTG" {
  siteId: string
  serial: string schema name "SERIAL_NO"
  status: string = "UNKNOWN"
  ratedPowerKw: double schema name "RATED_KW"
  site: Site(siteId)
  sensors: [Sensor](turbineId, id)
}

@db(index="turbineId")
entity type Sensor mixes Persistable {
  turbineId: string
  channel: string
  turbine: Turbine(turbineId)
  readings: timeseries<double>(id)
}
"""


def fleet_source():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "models", "fleet.ks")) as fh:
        return fh.read()


SITES = [
    {"id": "s1", "name": "Alpha", "region": "north", "version": 1},
    {"id": "s2", "name": "Bravo", "region": "south", "version": 1},
    {"id": "s3", "name": "Cielo", "region": "north", "version": 2},
]

TURBINES = [
    {"id": "t1", "siteId": "s1", "serial": "A-1", "status": "ACTIVE", "ratedPowerKw": 3000.0, "version": 1},
    {"id": "t2", "siteId": "s1", "serial": "A-2", "status": "DERATED", "ratedPowerKw": 2200.0, "version": 1},
    {"id": "t3", "siteId": "s2", "serial": "B-1", "status": "ACTIVE", "ratedPowerKw": 4100.0, "version": 1},
    {"id": "t4", "siteId": "s2", "serial": "B-2", "status": "OFFLINE", "ratedPowerKw": 4100.0, "version": 1},
    {"id": "t5", "siteId": "s3", "serial": "C-1", "status": "ACTIVE", "ratedPowerKw": 1800.0, "version": 1},
]

SENSORS = [
    {"id": "t1-temp", "turbineId": "t1", "channel": "temp", "version": 1},
    {"id": "t1-vib", "turbineId": "t1", "channel": "vib", "version": 1},
    {"id": "t3-temp", "turbineId": "t3", "channel": "temp", "version": 1},
]

// Wind fleet reliability model.
//
// This is the model the demo and the benchmarks run against. It is written to
// exercise every feature the resolver supports: a mixin, an abstract base with
// two concrete subtypes, many-to-one references, one-to-many collections, a
// time series field, explicit legacy column names, and index annotations.

mixin type Persistable {
  id: string
  version: int = 1
}

mixin type Audited {
  createdAt: datetime
  updatedBy: string
}

@db(index="region")
entity type Site mixes Persistable, Audited {
  name: string
  region: string
  timezoneOffsetMin: int = 0
  assets: [Asset](siteId, id)
  workOrders: [WorkOrder](siteId, id)
}

// Nothing is stored against Asset directly; it exists so that turbines and
// substations share an identity, a site, and a commissioning date.
@db(index="siteId")
abstract entity type Asset mixes Persistable {
  siteId: string
  serial: string
  status: string = "UNKNOWN"
  commissionedOn: datetime
  site: Site(siteId)
}

// Deliberately mapped onto a legacy table and column naming scheme, so the
// generated DDL has to honour `schema name` rather than the field names.
@db(unique="serial", index="status")
entity type Turbine extends Asset schema name "WTG_MASTER" {
  ratedPowerKw: double schema name "RATED_KW"
  hubHeightM: double schema name "HUB_HT_M"
  rotorDiameterM: double
  model: string schema name "WTG_MODEL"
  sensors: [Sensor](assetId, id)
}

@db(index="siteId")
entity type Substation extends Asset {
  capacityMva: double
  voltageKv: double
}

@db(index="assetId", unique="assetId,channel")
entity type Sensor mixes Persistable {
  assetId: string
  channel: string
  unit: string
  calibrationOffset: double = 0.0
  turbine: Turbine(assetId)
  readings: timeseries<double>(id)
  quality: timeseries<int>(id)
}

@db(index="siteId,openedAt")
entity type WorkOrder mixes Persistable, Audited {
  siteId: string
  assetId: string
  openedAt: datetime
  closedAt: datetime
  priority: int = 3
  category: string
  downtimeMinutes: int = 0
  site: Site(siteId)
}

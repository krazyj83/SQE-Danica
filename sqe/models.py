"""Plain data objects shared by every layer. Everything here is JSON-serializable."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum


class Role(str, Enum):
    CAS = "CAS"
    CAP = "CAP"
    SWEEP = "SWEEP"
    ESCORT = "ESCORT"
    SEAD = "SEAD"
    STRIKE = "STRIKE"
    TRANSPORT = "TRANSPORT"     # cargo flights (logistics.py)


class BaseKind(str, Enum):
    AIRFIELD = "AIRFIELD"
    CARRIER = "CARRIER"


class RefuelMethod(str, Enum):
    BASKET = "BASKET"   # probe-and-drogue (Navy jets)
    BOOM = "BOOM"       # F-16 etc.
    NONE = "NONE"       # no air-to-air refuelling (warbirds, most trainers and many mods): never gets a tanker


class AssetKind(str, Enum):
    ARMOR = "ARMOR"
    AIRFIELD = "AIRFIELD"
    SAM = "SAM"
    EWR = "EWR"
    C2 = "C2"
    DEPOT = "DEPOT"
    FUEL = "FUEL"


class ObjectiveType(str, Enum):
    CAS = "CAS"                # close air support on an enemy armor column (JTAC)
    STRIKE = "STRIKE"          # bomb a ground asset
    DEAD = "DEAD"              # destroy an air-defence site
    FLEET_DEFENSE = "FLEET_DEFENSE"  # intercept an enemy bomber raid on the fleet
    COUNTER_AIR = "COUNTER_AIR"  # sweep / hit an enemy airfield's air wing
    BARCAP = "BARCAP"          # defend a friendly base / the fleet
    CARGO = "CARGO"            # fly supplies to a friendly base (target_id = that base; logistics.py)


@dataclass
class Base:
    id: str
    name: str
    kind: BaseKind
    x: float = 0.0              # DCS coords (x = north, y = east, metres). Carriers use these.
    y: float = 0.0
    airport: str | None = None  # pydcs airport name for AIRFIELD bases
    defense: float = 1.0        # air-defence strength (Patriot + AAA); the enemy can degrade it, it repairs daily


@dataclass
class Squadron:
    id: str
    name: str
    aircraft: str               # key into aircraft.AIRCRAFT, e.g. "F-14BU"
    base_id: str
    authorized: int
    available: int
    callsign: str = "Viper"
    side: str = "blue"          # "blue" (state.squadrons, base_id = a Base) | "red" (state.red_squadrons, base_id = a red airfield asset)

    @property
    def readiness(self) -> float:
        return self.available / self.authorized if self.authorized else 0.0


class AirWing:
    """Everything red has at one airfield, seen as one wing: a live view over that field's red squadrons.

    Code that thinks in wings (planning, threat, raids, the debrief) keeps working: reading `available` sums the squadrons, and setting it
    spreads the change over them (losses come off the fullest squadron first, replacements go to the emptiest). The squadrons are what
    is saved."""
    def __init__(self, base_asset_id: str, units: list):
        self.base_asset_id = base_asset_id
        self.units = units                                   # the Squadron objects (side "red") at this field

    @property
    def types(self) -> list:
        out = []
        for q in self.units:
            if q.aircraft not in out:
                out.append(q.aircraft)
        return out

    @property
    def squadrons(self) -> int:
        return len(self.units)

    @property
    def authorized(self) -> int:
        return sum(q.authorized for q in self.units)

    @property
    def available(self) -> int:
        return sum(q.available for q in self.units)

    @available.setter
    def available(self, value: int) -> None:
        value = max(0, int(value))
        delta = value - self.available
        while delta < 0 and self.units:
            q = max(self.units, key=lambda q: q.available)
            if q.available <= 0:
                break
            q.available -= 1
            delta += 1
        while delta > 0 and self.units:
            q = min(self.units, key=lambda q: (q.readiness >= 1.0, q.readiness))
            q.available += 1
            delta -= 1

    def of_type(self, aircraft: str):
        return next((q for q in self.units if q.aircraft == aircraft), None)

    def __repr__(self):
        return f"AirWing({self.base_asset_id}, {self.types}, {self.available}/{self.authorized})"


@dataclass
class EnemyAsset:
    id: str
    name: str
    kind: AssetKind
    x: float
    y: float
    health: float = 1.0                 # 0..1, 0 = destroyed
    value: int = 5                      # planner priority weight
    defended_by: list[str] = field(default_factory=list)  # ids of SAM/EWR assets covering it
    airport: str | None = None          # AIRFIELD assets: pydcs airport name
    variant: str = ""                   # SAM/EWR flavour: "SA-2", "SA-6", "SA-11", "EWR"; ARMOR: "ARMOR" (moving column) or "GARRISON" (dug in)
    tier: int = 1                       # depth tier: 1 front, 2 Abkhazia, 3 coast / north Caucasus, 4 deep; gated by CampaignState.front
    guards: str = ""                    # GARRISON: id of the SAM site it protects
    suppressed: bool = False            # SAM: radars blinded today (SEAD worked, the launchers are still there); clears at end of day

    @property
    def destroyed(self) -> bool:
        return self.health <= 0.05




@dataclass
class Objective:
    id: str
    type: ObjectiveType
    target_id: str              # asset id (or base id for BARCAP)
    priority: float
    description: str = ""

    def to_dict(self):
        d = asdict(self)
        d["type"] = self.type.value
        return d


@dataclass
class PlayerProfile:
    aircraft: str               # "F-14BU", "F-16C"
    squadron_id: str
    callsign: str = "Hammer"
    flight_number: int = 1

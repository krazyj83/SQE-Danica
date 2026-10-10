"""Logistics: every airfield on both sides holds fuel, ammunition and spares; sorties use them up and the supply chain ships them forward.

Plain numbers in CampaignState.logistics (JSON), so it saves with the campaign:

    {"v": 1,
     "bases": {base_id: {"side": "blue"|"red", "name": str, "fuel": t, "ammo": t, "spares": t, "cap": {"fuel": t, "ammo": t, "spares": t}}},
     "sources": [{"side", "id", "name", "x", "y", "kind": "rear"|"depot"|"fuel"|"sea", "asset": asset id or ""}],
     "shipments": [today's deliveries: {"side", "from", "to", "mode", "fuel", "ammo", "spares", "lost"}],
     "used": {base_id: {"fuel", "ammo", "spares"}}}                         # what each base used in the day just ended

Blue bases are keyed by their Base id (state.bases), red bases by the airfield asset id (state.assets, "ab_..."); a red field's squadrons
are state.red_squadrons with that base_id, so both sides work the same way.

The day, in end_day order:
  1. consume: every package that flew today charges fuel and ammunition to the bases its flights took off from; red fields burn fuel for
     their own patrols and raids;
  2. deliver: each side's sources (its rear base or the sea, its depots and fuel farms, scaled by their health) ship to the bases that need it
     most. A shipment whose route crosses live enemy air defences, or runs under enemy air superiority, loses part of its load;
  3. replacements: new airframes and base repairs draw on spares (repair_allowance).

What stock does:
  * sortie_cap: a squadron cannot fly more sorties than its base's fuel and ammunition allow (the daily planner respects it);
  * operational: a red field below LOW_FRAC of its fuel cannot launch fighters, escorts or raids;
  * supply_factor: the ground war's supply also reflects the stock at that side's forward fields.

Flyable cargo missions (next step) add to the same ledger: what a transport lands at a base is put into its stock.
"""
from __future__ import annotations
import math

NM = 1852.0
COMMODITIES = ("fuel", "ammo", "spares")
CAP_AIRFIELD = {"fuel": 900.0, "ammo": 250.0, "spares": 60.0}       # tonnes a main airfield holds
CAP_FORWARD = {"fuel": 300.0, "ammo": 100.0, "spares": 30.0}        # a forward strip (WWII landing grounds, small fields)
CAP_CARRIER = {"fuel": 2500.0, "ammo": 700.0, "spares": 120.0}
START_FRAC = 0.8                       # every base starts the campaign at 80%
LOW_FRAC = 0.15                        # below this much fuel a red field cannot launch
# A side's daily lift is sized to its own air force (so it balances on any theatre, era or level): with everything intact it ships 120%
# of what the force uses; its rear area (or the sea) carries 65% of that need on its own, its depots the other 50%. Lose the depots and
# the side slowly runs down.
REAR_SHARE, DEPOT_SHARE = 0.65, 0.5
BLUE_RATE = 1.0                        # sorties per blue airframe per day the lift is sized for (what the daily tasking order flies)
AMMO_A2A, AMMO_A2G = 0.25, 1.0         # tonnes of ammunition per aircraft per sortie (jets; WWII aircraft use 40%)
SPARES_PER_AIRFRAME = 1.5              # tonnes of spares per replacement aircraft made ready
SPARES_PER_REPAIR = 4.0                # tonnes of spares per 10% of an airfield's air defence repaired
RED_ACTIVITY = 0.6                     # sorties per serviceable red fighter per day (patrols, scrambles, training, ferry)


def _activity(aircraft: str) -> float:
    """How often a red type flies: fighters RED_ACTIVITY, strike aircraft less, strategic bombers rarely (they mostly sit on the ramp)."""
    try:
        from . import factions
        if aircraft == factions.bomber():
            return 0.1
        if aircraft not in (factions.red().get("air") or {}).get("fighters", []):
            return 0.4
    except Exception:
        pass
    return RED_ACTIVITY
MAX_LOSS = 0.6


# ---- set-up ----------------------------------------------------------------------------------------------------------------------
def active(state) -> bool:
    return bool(getattr(state, "logistics", None))


def _caps(kind: str, name: str = "") -> dict:
    if kind == "CARRIER":
        return dict(CAP_CARRIER)
    forward = any(name.startswith(p) for p in ("A-", "B-")) and name[2:3].isdigit()     # Normandy landing grounds: A-2, B-11 ...
    return dict(CAP_FORWARD if forward else CAP_AIRFIELD)


def _era() -> str:
    try:
        from . import factions
        return factions.blue().get("era", "modern")
    except Exception:
        return "modern"


def build(state) -> None:
    """Stock every base on both sides and work out where each side's supplies come from."""
    from .models import AssetKind
    bases = {}
    for b in state.bases.values():
        cap = _caps(b.kind.value, b.name)
        bases[b.id] = {"side": "blue", "name": b.name, "cap": cap, **{c: round(cap[c] * START_FRAC, 1) for c in COMMODITIES}}
    for a in state.assets.values():
        if a.kind == AssetKind.AIRFIELD:
            cap = _caps("AIRFIELD", a.name)
            bases[a.id] = {"side": "red", "name": a.name, "cap": cap, **{c: round(cap[c] * START_FRAC, 1) for c in COMMODITIES}}
    state.logistics = {"v": 1, "bases": bases, "sources": _sources(state, bases), "shipments": [], "used": {}}
    _size_lift(state)


def ensure(state) -> bool:
    """A campaign from before logistics gets a full ledger the first time it is opened. Returns True when it made one."""
    if active(state):
        return False
    build(state)
    return True


def _front_xy(state):
    g = getattr(state, "ground", None) or {}
    if g.get("centers"):
        c = g["centers"][0]
        return c[0], c[1]
    from .models import AssetKind
    red = [a for a in state.assets.values() if a.kind == AssetKind.AIRFIELD]
    if red:
        return sum(a.x for a in red) / len(red), sum(a.y for a in red) / len(red)
    return 0.0, 0.0


def _sources(state, bases) -> list:
    """Each side: its rear (the blue base farthest from the front, or the carrier as the sea route; red's deepest field), plus every depot
    and fuel site it owns (their health scales what they add)."""
    from .models import AssetKind
    fx, fy = _front_xy(state)
    out = []
    carrier = next((b for b in state.bases.values() if b.kind.value == "CARRIER"), None)
    if carrier:
        out.append({"side": "blue", "id": "src_sea", "name": "Sea lift (fleet supply)", "x": carrier.x, "y": carrier.y, "kind": "sea", "asset": ""})
    fields = [b for b in state.bases.values() if b.kind.value == "AIRFIELD"]
    if fields:
        rear = max(fields, key=lambda b: math.hypot(b.x - fx, b.y - fy))
        out.append({"side": "blue", "id": "src_rear_blue", "name": f"Rear area ({rear.name})", "x": rear.x, "y": rear.y, "kind": "rear", "asset": ""})
    for a in getattr(state, "blue_assets", {}).values():
        if a.kind == AssetKind.DEPOT:                  # blue fuel farms are each base's own storage, not a supply source
            out.append({"side": "blue", "id": f"src_{a.id}", "name": a.name, "x": a.x, "y": a.y, "kind": a.kind.value.lower(), "asset": a.id, "pool": "blue"})
    red_fields = [a for a in state.assets.values() if a.kind == AssetKind.AIRFIELD]
    if red_fields:
        rear = max(red_fields, key=lambda a: math.hypot(a.x - fx, a.y - fy))
        out.append({"side": "red", "id": "src_rear_red", "name": f"Rear area ({rear.name})", "x": rear.x, "y": rear.y, "kind": "rear", "asset": ""})
    for a in state.assets.values():
        if a.kind in (AssetKind.DEPOT, AssetKind.FUEL):
            out.append({"side": "red", "id": f"src_{a.id}", "name": a.name, "x": a.x, "y": a.y, "kind": a.kind.value.lower(), "asset": a.id, "pool": "red"})
    return out


def demand(state, side: str) -> float:
    """Tonnes a side's air force uses on a normal day: blue flies about one sortie per airframe, red RED_ACTIVITY."""
    if side == "blue":
        return sum(q.authorized * BLUE_RATE * (_fuel_t(q.aircraft) + (AMMO_A2A + AMMO_A2G) / 2) for q in state.squadrons.values())
    return sum(q.authorized * _activity(q.aircraft) * (_fuel_t(q.aircraft) + AMMO_A2A) for q in state.red_squadrons.values())


def _size_lift(state) -> None:
    """Give every source its share of the side's lift ("t", tonnes a day at full health)."""
    for side in ("blue", "red"):
        srcs = [s for s in state.logistics["sources"] if s["side"] == side]
        need = demand(state, side)
        rear = [s for s in srcs if s["kind"] in ("rear", "sea")]
        deps = [s for s in srcs if s["kind"] not in ("rear", "sea")]
        rear_t = need * (REAR_SHARE if deps else REAR_SHARE + DEPOT_SHARE)
        for s in rear:
            s["t"] = round(rear_t * (0.6 if s["kind"] == "sea" else 0.4) / max(1e-9, sum(0.6 if r["kind"] == "sea" else 0.4 for r in rear)), 1)
        for s in deps:
            s["t"] = round(need * DEPOT_SHARE / len(deps), 1)


# ---- reading ------------------------------------------------------------------------------------------------------------------------
def base(state, base_id: str) -> dict | None:
    return (getattr(state, "logistics", None) or {}).get("bases", {}).get(base_id)


def frac(state, base_id: str, c: str = "fuel") -> float:
    b = base(state, base_id)
    return 1.0 if b is None else (b[c] / b["cap"][c] if b["cap"][c] else 0.0)


def status(state, base_id: str) -> str:
    """GOOD / LOW / CRITICAL, from the scarcest commodity."""
    b = base(state, base_id)
    if b is None:
        return "GOOD"
    worst = min(frac(state, base_id, c) for c in COMMODITIES)
    return "GOOD" if worst >= 0.4 else "LOW" if worst >= LOW_FRAC else "CRITICAL"


def operational(state, base_id: str) -> bool:
    """A field with fuel can launch. No ledger yet (an old campaign) = always operational."""
    return base(state, base_id) is None or frac(state, base_id, "fuel") >= LOW_FRAC


def _fuel_t(aircraft: str) -> float:
    """Tonnes of fuel one sortie burns: a full internal load plus 30% for taxi, reserve and drop tanks."""
    from .aircraft import AIRCRAFT
    try:
        spec = AIRCRAFT.for_type(aircraft) if aircraft not in AIRCRAFT else AIRCRAFT[aircraft]
        kg = float(getattr(spec.dcs_type, "fuel_max", 3000) or 3000)
    except Exception:
        kg = 3000.0
    return kg * 1.3 / 1000.0


def _ammo_t(aircraft: str, role: str) -> float:
    from .aircraft import AIRCRAFT
    a = AMMO_A2A if role in ("CAP", "ESCORT", "SWEEP") else AMMO_A2G
    try:
        spec = AIRCRAFT.for_type(aircraft) if aircraft not in AIRCRAFT else AIRCRAFT[aircraft]
        if spec.era == "ww2":
            a *= 0.4
    except Exception:
        pass
    return a


def sortie_cap(state, sq) -> int:
    """How many sorties this squadron's base can support today. The base's stock is shared between its squadrons by what they need:
    an F-15 squadron burns twice the fuel of the F-16 squadron next to it, so it gets twice the share."""
    b = base(state, sq.base_id)
    if b is None:
        return 10 ** 6
    mates = [q for q in state.squadrons.values() if q.base_id == sq.base_id] if sq.side == "blue" else \
            [q for q in state.red_squadrons.values() if q.base_id == sq.base_id]
    tr = _is_transport_sq(sq)                          # transports fly occasional cargo runs: they share among themselves, not with the combat squadrons
    mates = [q for q in mates if _is_transport_sq(q) == tr]
    per_ammo = (AMMO_A2A + AMMO_A2G) / 2
    need_fuel = sum(q.available * _fuel_t(q.aircraft) for q in mates) or 1.0
    need_ammo = sum(q.available for q in mates) * per_ammo or 1.0
    by_fuel = b["fuel"] * sq.available / need_fuel                       # sorties per airframe the fuel allows, times this squadron's airframes
    by_ammo = b["ammo"] * sq.available / need_ammo
    return max(0, int(min(by_fuel, by_ammo)))


def _is_transport_sq(q) -> bool:
    from .aircraft import AIRCRAFT
    from .models import Role
    try:
        spec = AIRCRAFT[q.aircraft] if q.aircraft in AIRCRAFT else AIRCRAFT.for_type(q.aircraft)
        return Role.TRANSPORT in spec.roles
    except Exception:
        return False


def supply_factor(state, side: str) -> float:
    """0.5 .. 1.0: how well stocked the side's forward bases are (the three nearest the front), for the ground war."""
    led = getattr(state, "logistics", None) or {}
    if not led:
        return 1.0
    fx, fy = _front_xy(state)

    def xy(bid):
        b = state.bases.get(bid) if side == "blue" else state.assets.get(bid)
        return (b.x, b.y) if b is not None else (fx, fy)
    ids = sorted((bid for bid, b in led["bases"].items() if b["side"] == side), key=lambda bid: math.hypot(xy(bid)[0] - fx, xy(bid)[1] - fy))[:3]
    if not ids:
        return 1.0
    f = sum(min(frac(state, i, "fuel"), frac(state, i, "ammo")) for i in ids) / len(ids)
    return 0.5 + 0.5 * min(1.0, f / 0.6)


# ---- the day: consume ----------------------------------------------------------------------------------------------------------------
def _take(b: dict, c: str, t: float) -> float:
    got = min(b[c], max(0.0, t))
    b[c] = round(b[c] - got, 2)
    return got


def consume_day(state, plan: list) -> None:
    """Charge today's packages (dicts, as in state.plan) to the bases they flew from, and red's own flying to its fields."""
    led = state.logistics
    used = {}

    def charge(bid, c, t):
        b = led["bases"].get(bid)
        if b is None:
            return
        got = _take(b, c, t)
        u = used.setdefault(bid, {k: 0.0 for k in COMMODITIES})
        u[c] = round(u[c] + got, 2)
    for pd in plan:
        if (pd.get("extra") or {}).get("scrub"):
            continue                                   # scrubbed for weather: never took off
        for f in pd.get("flights", []):
            n = int(f.get("count", 0))
            charge(f["base_id"], "fuel", n * _fuel_t(f["aircraft"]))
            charge(f["base_id"], "ammo", n * _ammo_t(f["aircraft"], f.get("role", "CAP")))
    for w in state.enemy_air:
        if w.available <= 0:
            continue
        burn = sum(q.available * _activity(q.aircraft) * _fuel_t(q.aircraft) for q in w.units)
        charge(w.base_asset_id, "fuel", burn)
        charge(w.base_asset_id, "ammo", sum(q.available * _activity(q.aircraft) for q in w.units) * AMMO_A2A)
    led["used"] = used


# ---- the day: deliver --------------------------------------------------------------------------------------------------------------
def _source_t(state, s: dict, era: str) -> float:
    """Tonnes this source ships today: its share of the lift, times the health of its depot (the rear area and the sea never fail)."""
    t = float(s.get("t", 0.0))
    if s["kind"] in ("rear", "sea"):
        return t
    pool = state.assets if s.get("pool") == "red" else getattr(state, "blue_assets", {})
    a = pool.get(s["asset"])
    return t * (a.health if a is not None else 0.0)


def _route_loss(state, side: str, sxy, dxy) -> float:
    """Share of a shipment lost on the way: each live enemy air-defence ring the route crosses, plus the other side's air superiority."""
    from .models import AssetKind
    from . import threatmap as tm
    loss = 0.0
    if side == "blue":
        for a in state.assets.values():
            if a.kind == AssetKind.SAM and not a.destroyed:
                r = tm.RANGE_NM.get(a.variant, 0) * NM
                if r and tm.seg_dist(a.x, a.y, sxy[0], sxy[1], dxy[0], dxy[1]) < r:
                    loss += 0.08 * a.health
        from .war import totals
        loss += 0.15 * totals(state).get("enemy_air", 0.0) * 0.3
    else:
        from .war import totals
        t = totals(state)
        loss += 0.05 + 0.25 * max(0.0, t.get("friendly_air", 0.0) - 0.5 * t.get("enemy_air", 0.0))
    return min(MAX_LOSS, loss)


def _mode(state, side: str, s: dict, dxy) -> str:
    if s["kind"] == "sea":
        return "sea"
    d_nm = math.hypot(dxy[0] - s["x"], dxy[1] - s["y"]) / NM
    try:
        from . import seacheck
        wet = any(not seacheck.is_land(s["x"] + (dxy[0] - s["x"]) * k / 6, s["y"] + (dxy[1] - s["y"]) * k / 6) for k in range(1, 6))
    except Exception:
        wet = False
    if wet:
        return "airlift" if side == "blue" or _era() != "ww2" else "sea"
    return "convoy" if d_nm <= 250 else "airlift"


def deliver_day(state, rng=None) -> list:
    """Ship from each side's sources to the bases that need it most. Returns log lines."""
    led = state.logistics
    era = _era()
    ships, lines = [], []
    for side in ("blue", "red"):
        srcs = [s for s in led["sources"] if s["side"] == side]
        dest = {bid: b for bid, b in led["bases"].items() if b["side"] == side and not _cut_off(state, side, bid)}
        if not srcs or not dest:
            continue
        lift = sum(_source_t(state, s, era) for s in srcs)
        need = {bid: {c: max(0.0, b["cap"][c] - b[c]) for c in COMMODITIES} for bid, b in dest.items()}
        total_need = sum(sum(n.values()) for n in need.values())
        if total_need <= 0.5 or lift <= 0.5:
            continue
        scale = min(1.0, lift / total_need)
        main = max(srcs, key=lambda s: _source_t(state, s, era))
        for bid, n in need.items():
            want = {c: n[c] * scale for c in COMMODITIES}
            if sum(want.values()) < 0.5:
                continue
            dxy = _xy(state, side, bid)
            src = min(srcs, key=lambda s: (math.hypot(s["x"] - dxy[0], s["y"] - dxy[1]) if _source_t(state, s, era) > 1 else 1e12))
            if _source_t(state, src, era) <= 1:
                src = main
            loss = _route_loss(state, side, (src["x"], src["y"]), dxy)
            b = dest[bid]
            got = {}
            for c in COMMODITIES:
                arrive = want[c] * (1.0 - loss)
                b[c] = round(min(b["cap"][c], b[c] + arrive), 1)
                got[c] = round(arrive, 1)
            lost = round(sum(want.values()) * loss, 1)
            mode = _mode(state, side, src, dxy)
            ships.append({"side": side, "from": src["name"], "to": b["name"], "to_id": bid, "mode": mode, **got, "lost": lost})
        if side == "blue":
            low = [b["name"] for bid, b in dest.items() if status(state, bid) == "CRITICAL"]
            if low:
                lines.append(f"supplies critical at {', '.join(low)}")
    road = sorted((x for x in ships if x["side"] == "red" and x["mode"] == "convoy"), key=lambda x: -(x["fuel"] + x["ammo"] + x["spares"]))
    for x in road[MAX_CONVOYS:]:
        x["mode"] = "road"                                 # small loads by back roads: delivered tonight, not a target
    for x in road[:MAX_CONVOYS]:                           # the big convoys arrive tomorrow night: take them back off the stock for now
        b = led["bases"][x["to_id"]]
        for c in COMMODITIES:
            b[c] = round(b[c] - x[c], 1)
    led["shipments"] = ships
    lines += arrive_convoys(state)                         # last night's convoys arrive with what is left of them
    launch_convoys(state, ships)                           # tonight's go on the road, as targets for tomorrow
    lost_blue = sum(s["lost"] for s in ships if s["side"] == "blue")
    if lost_blue >= 5:
        lines.append(f"{lost_blue:.0f} t of our supplies were lost on the way to the front")
    return lines


def _xy(state, side, bid):
    b = state.bases.get(bid) if side == "blue" else state.assets.get(bid)
    return (b.x, b.y) if b is not None else (0.0, 0.0)


def _cut_off(state, side, bid) -> bool:
    """A fallen blue field or a destroyed red field gets nothing until it is back in hand / repaired."""
    if side == "blue":
        return bid in (getattr(state, "ground", None) or {}).get("fallen", [])
    a = state.assets.get(bid)
    return a is not None and a.destroyed


# ---- replacements draw on spares --------------------------------------------------------------------------------------------------
def repair_allowance(state, base_id: str, want: int) -> int:
    """How many of `want` replacement aircraft the base's spares can make ready (spares are taken)."""
    b = base(state, base_id)
    if b is None or want <= 0:
        return want
    n = min(want, int(b["spares"] // SPARES_PER_AIRFRAME))
    _take(b, "spares", n * SPARES_PER_AIRFRAME)
    return n


def add_cargo(state, base_id: str, cargo: dict) -> dict:
    """Put delivered cargo into a base's stock (flyable cargo missions). Returns what was taken in (capped at the base's capacity)."""
    b = base(state, base_id)
    if b is None:
        return {}
    took = {}
    for c in COMMODITIES:
        v = float(cargo.get(c, 0.0))
        room = max(0.0, b["cap"][c] - b[c])
        took[c] = round(min(room, v), 1)
        b[c] = round(b[c] + took[c], 1)
    return took


def summary(state) -> list:
    """Lines for the briefing / overview: the state of each side's bases."""
    led = getattr(state, "logistics", None) or {}
    out = []
    for side, label in (("blue", "Our bases"), ("red", "Enemy fields")):
        bs = [bid for bid, b in led.get("bases", {}).items() if b["side"] == side]
        if not bs:
            continue
        crit = [led["bases"][b]["name"] for b in bs if status(state, b) == "CRITICAL"]
        low = [led["bases"][b]["name"] for b in bs if status(state, b) == "LOW"]
        txt = f"{label}: {len(bs) - len(crit) - len(low)} well supplied"
        if low:
            txt += f", {len(low)} low ({', '.join(low[:3])})"
        if crit:
            txt += f", {len(crit)} critical ({', '.join(crit[:3])})"
        out.append(txt + ".")
    return out


# ---- cargo runs (flown by you or by the AI) ------------------------------------------------------------------------------------------
CARGO_MIX = {"fuel": 0.5, "ammo": 0.35, "spares": 0.15}             # what a cargo load is made of
CARGO_THRESHOLD = 0.97                 # a base below this share of its stock (scarcest commodity) can be sent a cargo run
MAX_CARGO_TASKS = 2                    # cargo runs in the daily tasking order


def cargo_t(aircraft: str) -> float:
    from .catalog import CARGO_T
    from .aircraft import AIRCRAFT
    try:
        t = AIRCRAFT.for_type(aircraft).dcs_type.id if aircraft not in CARGO_T else aircraft
    except Exception:
        t = aircraft
    return CARGO_T.get(t, CARGO_T.get(aircraft, 2.0))


def cargo_load(flights: list) -> dict:
    """[(aircraft, count)] -> {"per_aircraft": {aircraft: t}, "total": t, "mix": CARGO_MIX}."""
    per = {a: cargo_t(a) for a, _n in flights}
    return {"per_aircraft": per, "total": round(sum(per[a] * n for a, n in flights), 1), "mix": dict(CARGO_MIX)}


def split(tonnes: float) -> dict:
    return {c: round(tonnes * f, 1) for c, f in CARGO_MIX.items()}


def cargo_objectives(state) -> list:
    """Cargo runs for today's tasking order: the neediest of our airfields, if anyone has transports to send."""
    from .models import Objective, ObjectiveType, Role
    from .aircraft import AIRCRAFT
    if not active(state):
        return []
    have = [q for q in state.squadrons.values() if Role.TRANSPORT in AIRCRAFT[q.aircraft].roles and q.available >= 2]
    if not have:
        return []
    fallen = (getattr(state, "ground", None) or {}).get("fallen", [])
    cands = []
    for b in state.bases.values():
        if b.kind.value != "AIRFIELD" or b.id in fallen:
            continue
        worst = min(frac(state, b.id, c) for c in COMMODITIES)
        if worst < CARGO_THRESHOLD and any(q.base_id != b.id for q in have):
            cands.append((worst, b))
    cands.sort(key=lambda t: (t[0], t[1].id))
    return [Objective("", ObjectiveType.CARGO, b.id, round(5.0 + 5.0 * (1.0 - w), 2), f"Deliver supplies to {b.name}")
            for w, b in cands[:MAX_CARGO_TASKS]]


def deliver_cargo(state, base_id: str, tonnes: float) -> dict:
    """Land `tonnes` of cargo at a base (split fuel / ammunition / spares). Returns what went into stock."""
    return add_cargo(state, base_id, split(tonnes))


def resolve_cargo(state, pkg, rng, d=None) -> dict:
    """An AI cargo run: each transport gets through unless the route bites (the same losses a shipment suffers, escorts halve them)."""
    from .models import Role
    obj = pkg.objective
    dest = state.bases.get(obj.target_id)
    tr = [f for f in pkg.flights if f.role == Role.TRANSPORT]
    if dest is None or not tr:
        return {"success": False, "odds": 0.0, "lines": [f"{obj.description}: no transports"], "objective": obj.description, "roll": {"success": False}}
    src = state.bases[tr[0].base_id]
    loss = _route_loss(state, "blue", (src.x, src.y), (dest.x, dest.y))
    if any(f.role == Role.ESCORT for f in pkg.flights):
        loss *= 0.5
    landed, lost_ac = 0.0, 0
    for f in tr:
        for _ in range(f.count):
            if rng.random() < loss * 0.5:              # shot down with its load
                lost_ac += 1
                continue
            landed += cargo_t(f.aircraft)
        sq = state.squadrons.get(f.squadron_id)
        if sq is not None and lost_ac:
            sq.available = max(0, sq.available - lost_ac)
    took = deliver_cargo(state, dest.id, landed)
    got = sum(took.values())
    lines = [f"{obj.description}: {got:.0f} t landed" + (f", {lost_ac} transport(s) lost" if lost_ac else "")]
    for ln in lines:
        state.note(ln)
    return {"success": got > 0, "odds": 1.0 - loss, "lines": lines, "objective": obj.description, "roll": {"success": got > 0}, "cargo_t": got}


# ---- red convoys: road shipments you can catch ---------------------------------------------------------------------------------
CONVOY_PREFIX = "convoy_"
MAX_CONVOYS = 3                        # the biggest red road shipments each night travel as convoys you can catch; the rest arrive by back roads


def _place_convoy(state, src: dict, dest_xy) -> tuple:
    """A spot on the road: 60% of the way from the source to the field, on land."""
    x = src["x"] + (dest_xy[0] - src["x"]) * 0.6
    y = src["y"] + (dest_xy[1] - src["y"]) * 0.6
    try:
        from . import seacheck
        x, y = seacheck.snap_to_land(x, y)
    except Exception:
        pass
    return x, y


def launch_convoys(state, shipments: list) -> None:
    """Red's road shipments travel overnight and arrive the NEXT night: until then each is a target on the road (a DEPOT-kind asset named
    'Supply convoy to <field>'). What it delivers is scaled by what is left of it."""
    from .models import EnemyAsset, AssetKind
    led = state.logistics
    pend = led.setdefault("convoys", {})
    sams = [a for a in state.assets.values() if a.kind == AssetKind.SAM and not a.destroyed]
    for k, sh in enumerate(shipments):
        if sh["side"] != "red" or sh["mode"] != "convoy":
            continue
        src = next((s for s in led["sources"] if s["name"] == sh["from"]), None)
        dxy = _xy(state, "red", sh["to_id"])
        if src is None:
            continue
        x, y = _place_convoy(state, src, dxy)
        aid = f"{CONVOY_PREFIX}{state.day}_{k}"
        cargo = {c: sh[c] for c in COMMODITIES}
        state.assets[aid] = EnemyAsset(aid, f"Supply convoy to {sh['to']}", AssetKind.DEPOT, x, y, 1.0, 5,
                                       [s.id for s in sams if math.hypot(s.x - x, s.y - y) < 40_000], None, "CONVOY", 1)
        pend[aid] = {"to": sh["to_id"], "to_name": sh["to"], "cargo": cargo, "day": state.day}


def arrive_convoys(state) -> list:
    """Convoys launched last night reach their fields now, carrying what is left of them; then they leave the map."""
    led = state.logistics
    pend = led.get("convoys", {})
    lines = []
    for aid, c in list(pend.items()):
        a = state.assets.pop(aid, None)
        h = a.health if a is not None else 0.0
        b = led["bases"].get(c["to"])
        if b is not None and h > 0.05:
            for k in COMMODITIES:
                b[k] = round(min(b["cap"][k], b[k] + c["cargo"][k] * h), 1)
        if h < 0.95:
            lines.append(f"the supply convoy to {c['to_name']} arrived at {h:.0%}" if h > 0.05 else f"the supply convoy to {c['to_name']} was destroyed on the road")
        del pend[aid]
    return lines

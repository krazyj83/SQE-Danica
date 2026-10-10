"""Cargo runs as flyable missions (logistics.py): transports from their own field to a base that needs supplies, with a fighter escort when
the route is threatened. You fly either the transport or the escort.

Lighter than a strike mission on purpose: no ground target, no AWACS or tanker, no SAM belt to build. What it does have:
  * the two airfields with their own air defence, set to their owners;
  * the transport flight: takes off (you hot on the runway, AI wingmen with you), climbs out, cruises and lands at the destination
    (helicopters fly low, 500 ft above the ground);
  * an escort flight that guards the transports and goes home;
  * if the enemy can reach the route, a pair of enemy fighters from the nearest field that can launch, hunting along it;
  * the results hook, which records where every aircraft landed: a transport that lands at the destination puts its load into that
    base's stock when you accept the results (debrief.apply_debrief, kind "cargo").
"""
from __future__ import annotations
import copy
import datetime
import hashlib
import math
import random
import tempfile
from pathlib import Path

from dcs import mapping, planes, task
from dcs.mission import Mission, StartType
from dcs.unit import Skill

from . import callsigns, factions, logistics, theatres, weather
from .aircraft import AIRCRAFT
from .debrief import Manifest, install_hook
from .loadouts import enemy_cap_loadout, ENEMY_FIGHTERS
from .models import AssetKind, BaseKind, Role
from .radio import build_radio_plan, RadioCfg
from .routes import NM, FT, Wpt, bearing, dist, offset

KPH = 1.852


def build_cargo(mb, package, out_path):
    """mb: a MissionBuilder (its options, loadouts, helpers and BuildResult). Returns a BuildResult."""
    from .mission_builder import BuildResult, _side_country
    o, st = mb.o, mb.state
    pf = package.player_flight
    if pf is None:
        raise ValueError("choose your flight first")
    warns: list = []
    mb.warns, mb.wx_notes = warns, []
    out_path = Path(out_path); out_path.parent.mkdir(parents=True, exist_ok=True)
    theatres.use(st.theatre)
    m = Mission(theatres.terrain())
    mb.m, mb.t = m, m.terrain
    blue, red = _side_country(m, "blue"), _side_country(m, "red")
    mb.usa, mb.red = blue, red
    date = st.campaign_date()
    hh, mm = int(package.start[:2]), int(package.start[3:5])
    start = datetime.datetime(date.year, date.month, date.day, hh, mm)
    m.start_time = start
    clock = lambda s: (start + datetime.timedelta(seconds=s)).strftime("%H:%M:%S")
    mb.wx = weather.for_package(st, o.weather_mode, theatres.active(), package.start)
    mb._weather(m, date)
    seed = f"{st.campaign_id}:{st.day}:{st.sortie_counter + 1}:{package.number}"
    mb.vrng = random.Random(seed)
    mb.seed_code = hashlib.md5(seed.encode()).hexdigest()[:5].upper()
    era = factions.blue().get("era", "modern")

    dest = st.bases[package.objective.target_id]
    tr = [f for f in package.flights if f.role == Role.TRANSPORT]
    origin = st.bases[tr[0].base_id]
    manifest = Manifest(st.campaign_id, st.sortie_counter + 1, package.id, st.day)
    manifest.cur_pkg = package.id

    # ---- the two fields (and the escorts' own), set to their owners, with their air defence -------------------------------------
    mb.ship, mb.apt = {}, {}
    for bs in st.bases.values():
        if bs.kind == BaseKind.AIRFIELD:
            mb.t.airports[bs.airport].set_blue()
    for a in st.assets.values():
        if a.kind == AssetKind.AIRFIELD and a.airport:
            mb.t.airports[a.airport].set_red()
    for bid in dict.fromkeys([dest.id] + [f.base_id for f in package.flights]):
        mb._place_base(st.bases[bid], dest.x, dest.y)

    # ---- radios: tower at the field you leave, the destination as the "divert" channel -------------------------------------------
    ww2 = era == "ww2"
    _atc = lambda ap_: (ap_.atc_radio.vhf_high_hz if ww2 else ap_.atc_radio.uhf_hz) / 1e6
    atc = _atc(mb.t.airports[origin.airport]) if origin.airport else o.carrier_atc_mhz
    dest_mhz = _atc(mb.t.airports[dest.airport]) if dest.airport else None
    from .radio import FrequencyAllocator as _FA
    plan = build_radio_plan(package, st, atc, mb.rng, o.layout, RadioCfg(o.carrier_tacan, o.carrier_link4_mhz), dest_mhz, dest.name,
                            band=_FA.VHF_WW2 if ww2 else None)

    # ---- the route: climb out, cruise, approach, land -------------------------------------------------------------------------------
    pt = lambda x, y: mapping.Point(x, y, mb.t)
    hdg = bearing(origin.x, origin.y, dest.x, dest.y)
    d_m = dist(origin.x, origin.y, dest.x, dest.y)
    spec_t = AIRCRAFT[tr[0].aircraft]
    heli = spec_t.helicopter
    cruise_ft = 500 if heli else spec_t.profile.cap_alt_ft
    kts = spec_t.profile.cap_kts
    climb = offset(origin.x, origin.y, hdg, min(d_m * 0.25, (4 if heli else 12) * NM))
    mid = offset(origin.x, origin.y, hdg, d_m * 0.5)
    appr = offset(dest.x, dest.y, hdg + 180, min(d_m * 0.25, (3 if heli else 10) * NM))
    leg_s = lambda a, b: dist(a[0], a[1], b[0], b[1]) / (kts * NM / 3600.0)
    t_climb = 120 + leg_s((origin.x, origin.y), climb)
    t_mid = t_climb + leg_s(climb, mid)
    t_appr = t_mid + leg_s(mid, appr)
    t_land = t_appr + leg_s(appr, (dest.x, dest.y)) + 120
    route = [("CLIMB", climb, t_climb), ("CRUISE", mid, t_mid), ("APPR", appr, t_appr)]
    mb.flight_groups = []

    def add_route(g, alt_ft, speed_kts, radio_alt):
        for name, (x, y), _t in route:
            wp = g.add_waypoint(pt(x, y), alt_ft * FT, speed_kts * KPH, name)
            if radio_alt:
                wp.alt_type = "RADIO"
        return g

    transport_groups = []
    for f in tr:
        spec = AIRCRAFT[f.aircraft]
        g = m.flight_group_from_airport(blue, f.callsign, spec.dcs_type, mb.t.airports[origin.airport], maintask=task.Transport,
                                        start_type=StartType.Runway, group_size=f.count)
        add_route(g, cruise_ft, kts, heli)
        g.land_at(mb.t.airports[dest.airport])
        names = _name_units(g, f)
        g.set_skill(Skill.High)
        if f.is_player:
            _make_player(mb, g, f, spec, plan, manifest)
        else:
            g.set_frequency(next((e.mhz for e in plan.package_flights if e.callsign == f.callsign), 130.0))
        manifest.add("friendly", f.id, names, squadron=f.squadron_id, callsign=f.callsign, player=f.is_player)
        per = logistics.cargo_t(f.aircraft)
        manifest.add("cargo", dest.id, names, airport=dest.airport or "", dest_name=dest.name, x=dest.x, y=dest.y, per_unit={n: per for n in names})
        transport_groups.append(g)
        mb.flight_groups.append((f, g))

    for f in (f for f in package.flights if f.role == Role.ESCORT):
        spec = AIRCRAFT[f.aircraft]
        base = st.bases[f.base_id]
        if base.kind == BaseKind.CARRIER:
            g = m.flight_group_from_unit(blue, f.callsign, spec.dcs_type, mb.ship[base.id], maintask=task.Escort,
                                         start_type=StartType.Runway, group_size=f.count)
        else:
            g = m.flight_group_from_airport(blue, f.callsign, spec.dcs_type, mb.t.airports[base.airport], maintask=task.Escort,
                                            start_type=StartType.Runway, group_size=f.count)
        esc_alt = max(cruise_ft + 2000, 6000 if ww2 else 15000)
        wp0 = g.add_waypoint(pt(*climb), esc_alt * FT, max(kts, spec.profile.cap_kts) * KPH, "JOIN")
        if transport_groups:
            wp0.tasks.append(task.EscortTaskAction(group_id=transport_groups[0].id, engagement_max_dist=int(20 * NM), lastwpt=len(route) + 1))
        for name, (x, y), _t in route[1:]:
            g.add_waypoint(pt(x, y), esc_alt * FT, max(kts, spec.profile.cap_kts) * KPH, name)
        if base.kind == BaseKind.AIRFIELD:
            g.land_at(mb.t.airports[base.airport])
        names = _name_units(g, f)
        load = mb.lo.for_role(f.aircraft, Role.ESCORT)
        for u in g.units:
            u.pylons = copy.deepcopy(load)
        g.set_skill(Skill.High)
        if f.is_player:
            _make_player(mb, g, f, spec, plan, manifest)
        else:
            g.set_frequency(next((e.mhz for e in plan.package_flights if e.callsign == f.callsign), 130.0))
        manifest.add("friendly", f.id, names, squadron=f.squadron_id, callsign=f.callsign, player=f.is_player)
        mb.flight_groups.append((f, g))

    # ---- the threat: a pair of enemy fighters along the route, if the enemy can reach it ------------------------------------------
    if package.n_def:
        _hunters(mb, m, red, package, mid, appr, cruise_ft, ww2, manifest, warns)

    # ---- text, hook, save ----------------------------------------------------------------------------------------------------------------
    stock = logistics.base(st, dest.id) or {}
    need = ", ".join(f"{c} {stock[c]:.0f}/{stock['cap'][c]:.0f} t" for c in logistics.COMMODITIES) if stock else "unknown"
    load_t = sum(logistics.cargo_t(f.aircraft) * f.count for f in tr)
    tl = {"launch": clock(0), "marshal": clock(t_climb), "push": clock(t_climb), "tot": clock(t_land), "egress": clock(t_land),
          "rtb": clock(t_land), "push_s": t_climb, "tot_s": t_land, "rtb_s": t_land}
    who = "\n".join(f"  {f.callsign:<14} {f.count}x {AIRCRAFT[f.aircraft].display:<18} {f.role.value}" + ("   <- YOU" if f.is_player else "")
                    for f in package.flights)
    comms = (("RADIO (VHF): " if ww2 else "COMM1 (UHF): ") + " | ".join(f"CH{c} {e.label} {e.mhz:.3f}" for c, e in plan.comm1.items()) + "\n" +
             ("FLIGHT (VHF): " if ww2 else "COMM2 (VHF): ") + " | ".join(f"CH{c} {e.label} {e.mhz:.3f}" for c, e in plan.comm2.items()))
    sit = (f"{start.strftime('%d %b %Y').upper()}. {dest.name} is running short: {need}.\n\n" + " ".join(logistics.summary(st)) +
           ("\n\nEnemy fighters can reach the route. The escort stays with the transports." if package.n_def else
            "\n\nNo organised fighter threat is expected on the route."))
    mis = (f"{package.objective.description}.\nYou are {pf.callsign}-1, {AIRCRAFT[pf.aircraft].display}, tasked as {pf.role.value}.\n"
           f"Cargo: {load_t:.0f} t (fuel, ammunition, spares) from {origin.name} to {dest.name}, {d_m / NM:.0f} nm.\n"
           "It counts when a transport lands at the destination: every transport that lands there delivers its load.")
    exe = f"Flights:\n{who}\n\nTimeline (local):\n  Launch {clock(0)}\n  Cruise {clock(t_climb)}\n  Land   {clock(t_land)} at {dest.name}"
    full = (f"1. SITUATION\n{sit}\n\n2. MISSION\n{mis}\n\n3. EXECUTION\n{exe}\n\n4. ADMINISTRATION AND LOGISTICS\n"
            f"Recovery: the transports stay at {dest.name}; escorts return to their own fields.\n\n5. COMMAND AND SIGNAL\n{comms}\n\n"
            f"{__import__('sqe').credit_line()}")
    text = {"situation": sit, "mission": mis, "execution": exe, "admin": "", "comms": comms, "threats": [], "full": full,
            "blue_task": f"{mis}\n\nLAND {clock(t_land)}"}
    m.set_description_text(full)
    m.set_description_bluetask_text(text["blue_task"])
    m.set_sortie_text(f"{start.strftime('%d %b %Y').upper()}  day {st.day}: {package.objective.description}")
    labels = {u.name: f.callsign for f, g in mb.flight_groups for u in g.units}
    calls = [(int(t_appr), f"{dest.name.upper()} TOWER to {pf.callsign.upper()}: cleared to land, cargo handlers standing by.")]
    with tempfile.TemporaryDirectory(prefix="sqe_cg_") as td:
        sound = mb._squelch(td)
        install_hook(m, st.campaign_id, manifest.sortie, package.id, [], manifest.player_unit, group=pf.callsign, calls=calls, wps=[],
                     sound=sound, flights=labels, enemy_air=[u for g_ in manifest.groups if g_["kind"] == "enemy_air" for u in g_["units"]])
        counts = mb._counts()
        mb._add_ship_warehouses(m)
        mb._require_mods(m)
        mb._save_without_carrier_slots(m, out_path)
    wps = [Wpt("TAKEOFF", origin.x, origin.y, 0, 0, origin.name)] + [Wpt(n, x, y, cruise_ft, kts, "") for n, (x, y), _t in route] + \
          [Wpt("LAND", dest.x, dest.y, 0, 0, dest.name)]
    whois = [{"cs": f.callsign, "ac": AIRCRAFT[f.aircraft].display, "role": f.role.value, "m3": "-", "stn": "-", "you": f.is_player} for f in package.flights]
    if not any(f.is_player for f in tr) and not tr:
        warns.append("no transports in this package")
    return BuildResult(out_path, manifest, plan, wps, package, tl, text, counts, whois, warns, mb.seed_code, [])


def _name_units(g, f) -> list:
    name, fno = f.callsign.rsplit(" ", 1)
    cs = callsigns.apply(g, name, int(fno), callsigns.table(f.aircraft))
    for u, n in zip(g.units, cs):
        u.name = n
    return [u.name for u in g.units]


def _make_player(mb, g, f, spec, plan, manifest) -> None:
    u0 = g.units[0]
    if not mb.o.player_is_ai:
        u0.set_client() if mb.o.player_is_client else u0.set_player()
    try:
        g.set_frequency(plan.freq("FLIGHT"), radio_id=mb.o.layout.comm2_radio_id)
    except Exception:
        g.set_frequency(plan.freq("FLIGHT"))
    manifest.player_unit = u0.name
    g.points[0].ETA = 0
    g.points[0].ETA_locked = True


def _hunters(mb, m, red, package, mid, appr, cruise_ft, ww2, manifest, warns) -> None:
    """A pair from the nearest red field that can launch fighters, sweeping the route from its middle toward the destination."""
    st = mb.state
    best = None
    for w in st.enemy_air:
        a = st.assets.get(w.base_asset_id)
        if a is None or a.destroyed or w.available < 2 or not logistics.operational(st, w.base_asset_id):
            continue
        types = [t for t in w.types if t in ENEMY_FIGHTERS]
        if not types:
            continue
        d = dist(a.x, a.y, mid[0], mid[1])
        if best is None or d < best[0]:
            best = (d, w, a, types[0])
    if best is None:
        return
    _d, w, a, tname = best
    cls = getattr(planes, tname)
    sx, sy = offset(mid[0], mid[1], bearing(mid[0], mid[1], a.x, a.y), 25 * NM)
    alt = max(cruise_ft + 3000, 8000 if ww2 else 20000) * FT
    g = m.flight_group(red, "Hunter 1", cls, None, mapping.Point(sx, sy, mb.t), altitude=alt, speed=(400 if ww2 else 800), maintask=task.FighterSweep, group_size=2)
    g.add_waypoint(mapping.Point(mid[0], mid[1], mb.t), alt, (400 if ww2 else 800), "SWEEP").tasks.append(
        task.EngageTargets(max_distance=int(30 * NM), targets=[task.Targets.All.Air]))
    g.add_waypoint(mapping.Point(appr[0], appr[1], mb.t), alt, (400 if ww2 else 800), "SWEEP2")
    load = enemy_cap_loadout(tname)
    names = []
    for j, u in enumerate(g.units, 1):
        u.name = f"ENM-{a.id}-H-{j}"; u.pylons = copy.deepcopy(load); names.append(u.name)
    g.set_skill(getattr(Skill, mb.d.enemy_skill, Skill.High))
    manifest.add("enemy_air", w.base_asset_id, names, type=tname)

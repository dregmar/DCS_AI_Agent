import re
from typing import Dict, List, Tuple, Any

# Threat priority weights (Higher score = higher tactical priority)
THREAT_WEIGHTS = {
    # Air Defense / High Threat
    "ZSU-23 Shilka": 100,
    "SA-8 Osa": 100,
    "SA-13 Gopher": 100,
    "2S6 Tunguska": 100,
    "2A38": 90,

    # Armor / Medium Threat
    "T-72B": 60,
    "T-80UD": 60,
    "BMP-2": 50,
    "BTR-80": 40,

    # Soft Targets / Low Threat
    "Ural-375": 10,
    "GAZ-66": 10,
    "UNKNOWN": 5
}

# George's Target List shows a CLASSIFICATION until he has identified a contact
# (manual p.655: GROUND / ARMORED / AAA / HELICOPTER / AIR / NAVAL ...). Matched exactly.
CLASSIFICATION_WEIGHTS = {
    "AAA": 90, "AIR DEFENSE": 95, "SAM": 100,
    "HELICOPTER": 70, "AIR": 60,
    "ARMORED": 50, "TRACKED": 40, "NAVAL": 30, "GROUND": 15,
}

# DCS display names vary ("ZSU-23-4 Shilka", "Osa 9A33 ln", "Strela-10M3", "M1A2 Abrams").
# First matching pattern wins, so the most dangerous classes come first.
_TYPE_PATTERNS = [
    # Radar SAM / gun-missile systems and short-range SAM
    (r"shilka|zsu-23|tunguska|2s6|pantsir|\btor\b|9a331|sa-15|\bosa\b|9a33|sa-8|strela|9a35|sa-13|"
     r"gopher|\bbuk\b|sa-11|9a310|\bkub\b|sa-6|gepard|roland|avenger|linebacker|chaparral|rapier|"
     r"hq-7|hawk|patriot|nasams|s-300|sa-10|sa-2\b|sa-3\b|sa-5|\bsam\b", 100),
    # MANPADS
    (r"igla|stinger|sa-18|sa-24|manpad|mpads", 95),
    # Towed / light AAA and heavy machine guns
    (r"zu-23|2a38|zsu-57|s-60|ks-19|vulcan|m163|bofors|flak|\baaa\b|dshk|kpvt|\.50 cal", 90),
    # Attack helicopters / aircraft
    (r"mi-24|mi-28|ka-50|ka-52|ah-64|ah-1|apache|cobra helicopter|hind|havoc|alligator", 70),
    # Main battle tanks
    (r"t-55|t-62|t-64|t-72|t-80|t-90|leopard|abrams|m1a\d|challenger|leclerc|merkava|chieftain|"
     r"\bm60|type 59|type 96|ztz|\btank\b", 60),
    # IFVs with ATGMs / autocannons
    (r"bmp|bmd|bradley|m2a2|marder|warrior|zbd|lav-25|\bifv\b", 50),
    # APCs and armoured cars
    (r"btr|brdm|m113|mt-lb|stryker|\blav\b|aav|tpz|fuchs|\bapc\b|cobra\b|vab\b", 45),
    # Infantry (RPGs are a real threat to a hovering helicopter)
    (r"rpg|infantry|soldier|insurgent|paratrooper|mortar team", 35),
    # Artillery and MLRS
    (r"msta|gvozdika|akatsiya|2s1\b|2s3|2s19|grad|bm-21|smerch|bm-30|uragan|bm-27|mlrs|m270|m109|"
     r"paladin|howitzer|mortar|2b11|d-30", 30),
    # Self-propelled guns seen 22 Oct (PLZ-05)
    (r"plz-?\d\d|pcl-?\d\d|sph\b", 30),
    # Transport / support aircraft (22 Oct: Il-76)
    (r"il-76|il-78|an-2[46]|an-30|c-130|c-17|kc-135|y-8|y-20|transport aircraft", 20),
    # Other helicopters (scout / transport), seen 21 Oct: OH-58
    (r"oh-58|kiowa|uh-60|black ?hawk|ch-47|chinook|mi-8|mi-17|mi-26|uh-1|sa342|gazelle|helicopter", 40),
    # Ships
    (r"frigate|corvette|patrol boat|ship|boat|speedboat|type[_ -]?0\d\d|lpd|landing", 30),
    # Fixed positions and buildings
    (r"outpost|bunker|pillbox|command post|checkpoint|fortification|sandbag|watch ?tower|tent|"
     r"barracks|building|warehouse|depot|hangar|structure|house|hut|antenna|satcom|mast", 15),
    # Trucks, utility, logistics
    (r"ural|gaz|kamaz|zil|kraz|truck|hmmwv|humvee|uaz|tigr|land ?rover|larc|m818|m978|fuel|"
     r"tanker|transport|bus|car\b|tractor", 10),
]
_reported_unknown = set()


def threat_weight(target_type: str) -> int:
    """Threat weight of a DCS type name (also used for the Huey crew chief's contact calls)."""
    return _threat_weight(target_type)


def _threat_weight(target_type: str) -> int:
    if target_type in THREAT_WEIGHTS:
        return THREAT_WEIGHTS[target_type]
    if target_type.strip().upper() in CLASSIFICATION_WEIGHTS:
        return CLASSIFICATION_WEIGHTS[target_type.strip().upper()]
    from common.unit_types import TYPES          # what DCS itself says the type is (no hard-coding)
    w = TYPES.weight(target_type)
    if w is not None:
        return w
    low = target_type.lower()
    for pattern, score in _TYPE_PATTERNS:
        if re.search(pattern, low):
            return score
    if target_type not in _reported_unknown:
        _reported_unknown.add(target_type)
        print(f"[THREAT] Unrecognized target type '{target_type}' - scoring as UNKNOWN until DCS "
              f"describes it (george_comms_hook.lua sends every unit type's attributes).")
    return THREAT_WEIGHTS["UNKNOWN"]


# Maximum effective ranges in meters
WEAPON_MAX_RANGE_M = {
    "MSL": 8000,   # AGM-114 Hellfire (~8 km)
    "RKT": 4500,   # Hydra 70mm (~4.5 km)
    "GUN": 1500    # M230 30mm (~1.5 km)
}


def evaluate_tactical_environment(state_snapshot: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """
    Evaluates raw targets, sorts them by threat priority, and checks weapon engagement constraints.
    Expects state_snapshot["targets"] to be the RAW target list.
    """
    raw_targets = state_snapshot.get("targets", [])
    pitch = state_snapshot.get("pitch", 0.0)
    roll = state_snapshot.get("roll", 0.0)
    was_mode = state_snapshot.get("was_state", "NONE")

    evaluated_targets = []

    for i, t in enumerate(raw_targets):
        target_type = str(t.get("type", "UNKNOWN"))
        try:
            dist_m = float(t["dist_m"]) if t.get("dist_m") is not None else None
        except (TypeError, ValueError):
            dist_m = None          # range unknown (George's list gives names only)

        base_score = _threat_weight(target_type)

        # Proximity modifier: Closer threats score slightly higher
        proximity_bonus = max(0, 10 - int(dist_m / 1000)) if dist_m is not None else 0
        total_score = base_score + proximity_bonus

        evaluated_targets.append({
            "id": t.get("id", i + 1),
            "wheel_index": i + 1,          # 1-based position in George's Target List
            "type": target_type,
            "dist_m": dist_m,
            "dist_km": round(dist_m / 1000.0, 1) if dist_m is not None else None,
            "threat_score": total_score,
            "clock": t.get("clock"),          # 1..12 relative to the nose, None if unknown
        })

    # Sort targets descending by threat score
    evaluated_targets.sort(key=lambda x: x["threat_score"], reverse=True)

    highest_threat = evaluated_targets[0] if evaluated_targets else {}

    # Engagement constraints.
    # NOTE: this is an attitude heuristic, not true line-of-sight masking, and it assumes
    # pitch/roll arrive in DEGREES. Export.lua APIs often return radians - verify.
    tads_masked = abs(pitch) > 15.0 or abs(roll) > 20.0

    out_of_range = False
    if highest_threat and was_mode in WEAPON_MAX_RANGE_M and highest_threat["dist_m"] is not None:
        if highest_threat["dist_m"] > WEAPON_MAX_RANGE_M[was_mode]:
            out_of_range = True

    constraints = {
        "tads_masked": tads_masked,
        "out_of_range": out_of_range,
        "master_arm_disarmed": state_snapshot.get("master_arm") == "DISARMED"
    }

    return evaluated_targets, highest_threat, constraints
"""
common/wingmen.py
Your AI wingmen, for any aircraft: orders by voice, who's in your flight, and the menu map.

    "two, return to base" · "flight, engage my target" · "three, cover me" · "wingman, rejoin"
    "two, go pincer left" · "flight, go trail" · "three, recon 5 km" · "two, hold position"

    "flight check" · "check wingmen" · "who's in my flight"   -> "Two, AI, up. Three, AI, down."
    "map wingman menu"                                        -> walks the menu, saves it to logs\\

Numbering is the military one: you are one (when you lead), "two" is your first wingman, the
second unit of your group in the mission.

With DCS Olympus running (common/olympus_orders.py), also: attack a chosen target, follow me,
go to / land at a waypoint, land next to me, weapons hold / free, lase, smoke.

Where the data comes from:
  * the flight line-up: the mission data, sent by george_comms_hook.lua ("FLIGHT ..."),
  * who's still alive: Export.lua's world objects of your group ("flight": [...]),
  * the menu: george_comms_hook.lua (open / select / back), only submenus are entered.
"""
import json
import os
import re
import time
from typing import Callable, Dict, List, Optional

NUM_SAY = {1: "one", 2: "two", 3: "three", 4: "four"}
ALIVE_FRESH_SEC = 5.0

_CHECK = re.compile(r"\b(?:flight (?:check|status)|check (?:my |the )?(?:wingm[ae]n|flight)|wingm[ae]n (?:check|status)|"
                    r"who(?:'s| s| is) in (?:my|the|our) flight|do (?:i|we) have (?:any )?wingm[ae]n)\b")
# 28 Oct: small.en hears "map" as "mop" / "mup"
_MAP = re.compile(r"\b(?:map|mop|mup|learn|read|scan) (?:the )?wingm[ae]n(?:'s)? menu\b")
# "map carrier menu": the ATC / Supercarrier branches (for voice -> carrier menu items, feature 10)
_MAP_CARRIER = re.compile(r"\b(?:map|mop|mup|learn|read|scan) (?:the )?(?:carrier|super ?carrier|atc|ship|marshal|tower)(?:'s)? menu\b")
CARRIER_BRANCHES = (r"atc|carrier|cvn|stennis|lincoln|roosevelt|washington|truman|forrestal|tarawa|kuznetsov|"
                    r"ship|marshal|tower|approach|other")


# ------------------------------------------------------------------ orders
# (menu path below the recipient, phrases). Names as DCS shows them (AH-64D, 4-ship, 19 Oct 2026);
# matched by name at each step, so it doesn't matter which F-key they're on today.
ORDERS = [
    (["Engage", "Mission and RTB"], r"\bmission and (?:rtb|return to base|go home|head home)\b"),
    (["Engage", "Mission and Rejoin"], r"\bmission and (?:rejoin|join up|come back)\b"),
    (["Engage", "My Target"], r"\b(?:engage|attack|hit|kill|take out|destroy)? ?my target\b"),
    (["Engage", "My Enemy"], r"\bmy enemy\b|\b(?:engage|get|attack) (?:whoever(?:'s| s| is)? shooting at me|"
                             r"my attacker|the guy shooting at me)\b|\bget him off me\b"),
    (["Engage", "Air Defences"], r"\b(?:engage|attack|hit|kill|take out|suppress)? ?(?:the )?(?:air defen[cs]es?|"
                                 r"sams?|triple a|aaa|flak|air defen[cs]e)\b"),
    (["Engage", "Ground Targets"], r"\b(?:engage|attack|hit|kill|take out)? ?(?:the )?ground targets?\b|"
                                   r"\b(?:engage|attack) (?:the )?(?:ground|armou?r|vehicles|tanks|convoy)\b"),
    (["Engage", "Bandits"], r"\b(?:engage|attack)? ?(?:the )?bandits?\b|\bengage air\b"),
    (["Go To", "Return to Base"], r"\b(?:return to base|rtb|go home|head home|go back to base|bug out)\b"),
    (["Go To", "Route"], r"\b(?:resume|return to|back to|go to|continue) (?:the |your )?route\b"),
    (["Go Pincer", "Right"], r"\bpincer right\b|\bgo pincer right\b"),
    (["Go Pincer", "Left"], r"\bpincer left\b|\bgo pincer left\b"),
    (["Cover Me"], r"\bcover me\b|\bcover my (?:six|back)\b"),
    (["Hold Position"], r"\b(?:hold|whole|hole) (?:position|here|your position)\b|\b(?:hold|stay|hover) (?:there|here)\b"),
    (["Rejoin Formation"], r"\b(?:rejoin|join up|form up|come back to me|back in formation)\b"),
    (["Jettison Weapons"], r"\bjettison (?:weapons|stores|ordnance|everything)\b"),
]
_FORMATIONS = ("echelon", "spread", "trail", "overwatch", "wedge", "line abreast", "column")
_RECON_KM = (1, 2, 3, 5, 8, 10)
_ORDER_RX = [(path, re.compile(rx)) for path, rx in ORDERS]

# Who: at the start. "two" = your first wingman (unit 2 of the group); Whisper hears "two" as
# "to"/"too" and "four" as "for".
# "tool" too (Olympus flight, 20 Oct: three orders lost to it)
_NUM_WORD = {"two": 2, "to": 2, "too": 2, "tool": 2, "2": 2, "three": 3, "tree": 3, "3": 3, "four": 4,
             "for": 4, "4": 4}
_LEAD = r"^(?:(?:hey|george|okay|ok|roger|arm|uh|um|and|so)\s+)*"
_WHO = re.compile(_LEAD + r"(?:(?:wingman|wingmen|number|dash)\s+)?"
                  r"(two|to|too|tool|2|three|tree|3|four|for|4)\b[\s,]*(.*)$|"
                  + _LEAD + r"(flight|everyone|everybody|wingmen|wingman)\b[\s,]*(.*)$")
_ONE = re.compile(_LEAD + r"(?:wingman|number|dash)\s+(?:one|1)\b")


EXECUTOR = None            # olympus_orders.Executor when DCS Olympus is configured (main.py)
_SOUNDALIKE_WHO = {"to", "too", "tool", "for", "tree"}     # may just be words: pass the utterance on


def _split(text: str):
    """(who, the word used, the rest) or None. who: 2..4, 'flight' or 'wingman'."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    m = _WHO.match(t)
    if not m:
        return None
    if m.group(1):
        return _NUM_WORD[m.group(1)], m.group(1), m.group(2)
    word = m.group(3)
    return ("wingman" if word == "wingman" else "flight"), word, m.group(4)


def parse_order(text: str):
    """'Two, return to base' -> (2, ['Go To', 'Return to Base']); 'flight cover me' -> ('flight', [...]);
    'wingman, rejoin' -> ('wingman', [...]). None if it isn't a wingman order."""
    split = _split(text)
    if split is None:
        return None
    who, _, rest = split
    if not rest:
        return None
    for path, rx in _ORDER_RX:
        if rx.search(rest):
            return who, list(path)
    f = next((f for f in _FORMATIONS if re.search(r"\b" + f + r"\b", rest)), None)
    if f and re.search(r"\b(?:go|formation|form|fly)\b", rest):
        return who, ["Go Formation", f.title()]
    r = re.search(r"\brecon(?:naissance|noiter)?\b.*?\b(\d{1,2})\b", rest)
    if r:
        km = min(_RECON_KM, key=lambda k: abs(k - int(r.group(1))))
        return who, ["Reconnaissance", f"{km} km"]
    return who, None                            # someone was called, but no order we know


def _who_say(who) -> str:
    return "Flight" if who == "flight" else NUM_SAY.get(who, str(who)).capitalize()


def _unit_number(name: str) -> Optional[int]:
    m = re.search(r"-(\d+)$", name)
    return int(m.group(1)) if m else None


def resolve(snap: dict, who):
    """(the wingmen addressed [{'n', 'name', 'ai', 'alive'}], None) or (None, what to say)."""
    r = roster(snap)
    if r is not None:
        ai = [w for w in r["wingmen"] if w["ai"]]
        up = [w for w in ai if w["alive"] is not False]
    else:                                        # no line-up (old hook): what Export.lua sees
        alive = snap.get("flight_alive")
        if alive is None:
            return None, "No flight data."
        ai = up = [{"n": _unit_number(n), "name": n, "ai": True, "alive": True} for n in alive]
    if not ai:
        return None, "Negative, no wingmen."
    if who == "wingman":
        if len(up) == 1:
            return [up[0]], None
        return None, ("No wingmen left." if not up else "Which wingman?")
    if who == "flight":
        return (up, None) if up else (None, "No wingmen left.")
    w = next((w for w in ai if w["n"] == who), None)
    if w is None:
        size = (len(r["wingmen"]) + 1) if r else len(ai) + 1
        return None, f"Negative, we're a {NUM_SAY.get(size, size)} ship, there's no {NUM_SAY.get(who, who)}."
    if w["alive"] is False:
        return None, f"{_who_say(who)} is down."
    return [w], None


def give_order(comms, snap: dict, who, path: Optional[List[str]]) -> str:
    """Checks the flight, then walks the menu by name. '' = done (George stays quiet);
    otherwise what to say."""
    if path is None:
        return "Say again the order."
    wing, err = resolve(snap, who)
    if err:
        return err
    if who == "wingman":
        who = wing[0]["n"]
    target = "Flight" if who == "flight" else f"Wingman {who}"
    ok, why = comms.select_path([target] + path)
    from common.orders import ORDERS
    ORDERS.record(_who_say(who), " / ".join(path), "submitted" if ok else "failed",
                  "radio menu" if ok else f"radio menu: {why}")
    if ok:
        print(f"[WINGMEN] {target}: {' / '.join(path)}")
        return ""
    print(f"[WINGMEN] {target}: {' / '.join(path)} failed: {why}")
    if who != "flight" and why == f"no {target} in the menu":
        # DCS doesn't list every wingman (a 3-ship Huey had only "Wingman 2")
        listed = [m for m in getattr(comms, "last_items", []) if m.lower().startswith("wingman")]
        names = ["flight"] + [NUM_SAY.get(_unit_number(m.replace(" ", "-")) or 0, m) for m in listed]
        choice = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        return f"{_who_say(who)} isn't in the radio menu. Tell {choice}."
    return f"Unable, {why}."


def roster(snap: dict) -> Optional[dict]:
    """{'group', 'me': n or None, 'wingmen': [{'n', 'name', 'ai', 'alive' (True/False/None)}]}
    or None when the mission data hasn't arrived."""
    units = snap.get("flight_units") or []
    if not units:
        return None
    if snap.get("my_group") and snap.get("flight_group") and snap["my_group"] != snap["flight_group"]:
        return None                  # the line-up is another aircraft's group: don't trust it
    alive = snap.get("flight_alive")
    fresh = alive is not None and snap.get("flight_alive_age", float("inf")) <= ALIVE_FRESH_SEC
    my_unit = snap.get("my_unit") or ""
    # Export.lua knows which unit is you, but sometimes gives the PILOT's name ("RedArcher", 28 Oct:
    # "One, player, down"); then the hook's own "you" mark stands
    if my_unit and any(name == my_unit for _, name, _, _ in units):
        units = [(n, name, skill, name == my_unit) for n, name, skill, _ in units]
    me = next((n for n, _, _, is_me in units if is_me), None)
    wingmen = []
    for n, name, skill, is_me in units:
        if is_me:
            continue
        ai = skill not in ("Client", "Player")
        wingmen.append({"n": n, "name": name, "ai": ai, "alive": (name in alive) if fresh else None})
    return {"group": snap.get("flight_group") or "", "me": me, "wingmen": wingmen}


def check_report(snap: dict) -> str:
    r = roster(snap)
    if r is None:
        return "No flight data. Is the comms hook running?"
    if not r["wingmen"]:
        return "No wingmen, we're a single ship."
    parts = []
    for w in r["wingmen"]:
        who = NUM_SAY.get(w["n"], str(w["n"])).capitalize()
        state = "" if w["alive"] is None else (", up" if w["alive"] else ", down")
        parts.append(f"{who}, {'AI' if w['ai'] else 'player'}{state}")
    lead = "" if r["me"] in (1, None) else f"You're number {NUM_SAY.get(r['me'], r['me'])}. "
    return lead + ". ".join(parts) + "."


def menu_lines(node: dict, indent: int = 0) -> List[str]:
    out = []
    for n, item in sorted(node["items"].items(), key=lambda kv: int(kv[0])):
        out.append("  " * indent + f"F{n} {item['name']}")
        if "sub" in item:
            out.extend(menu_lines(item["sub"], indent + 1))
    return out


def map_menu(comms, snap: dict, folder: str = "logs", kind: str = "wingman") -> str:
    """Walks and saves the wingman menu (kind "wingman") or the ATC / carrier menu ("carrier").
    Only opens submenus, never sends an order. Blocking (several seconds). Returns what to say."""
    try:
        tree = (comms.map_tree(root_rx=CARRIER_BRANCHES, max_depth=4, max_pages=150) if kind == "carrier"
                else comms.map_tree())
    except Exception as e:
        print(f"[WINGMEN] Menu mapping failed: {e}")
        return f"Couldn't map the menu: {e}."
    r = roster(snap)
    n_wing = len(r["wingmen"]) if r else 0
    ac = re.sub(r"[^A-Za-z0-9_-]+", "_", snap.get("aircraft") or "aircraft")
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, f"{kind}_menu_{ac}_{n_wing + 1}ship_{time.strftime('%Y%m%d_%H%M')}")
    lines = menu_lines(tree)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"aircraft": snap.get("aircraft"), "flight": r, "menu": tree}, f, indent=1)
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(f"{snap.get('aircraft')}, flight {r['group'] if r else '?'}: {check_report(snap)}\n\n")
        f.write("\n".join(lines) + "\n")
    print("[WINGMEN] Menu map:\n  " + "\n  ".join(lines))
    print(f"[WINGMEN] Saved to {base}.txt")
    entries = sum(1 for l in lines)
    what = "Carrier and ATC" if kind == "carrier" else "Wingman"
    return f"{what} menu mapped, {entries} items. Saved." if entries else f"No {what.lower()} items in the menu."


# common/independents.Independents, set by main.py when Olympus is enabled: wingmen gone
# independent ("two, go independent") are "Outcast 2" and get their orders through Olympus.
INDEPENDENTS = None


def _go_independent(snap: dict, who) -> str:
    wing, err = resolve(snap, who)
    if err:
        return err
    seen = {str(w[0]): w for w in snap.get("wing") or []}
    out = []
    for w in wing:
        row = seen.get(w["name"])
        if row is not None and int(row[1]) >= 0:
            out.append({"n": w["n"], "name": w["name"], "id": int(row[1]),
                        "lat": float(row[2]), "lon": float(row[3])})
    if not out:
        return f"Unable, I can't see {_who_say(who if who != 'wingman' else wing[0]['n']).lower()}."
    return INDEPENDENTS.detach(out, snap)


def handle(text: str, comms, snap: dict, speak: Callable[[str], None]) -> Optional[str]:
    """What to say ('' = done, say nothing), or None if this isn't a wingman command."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    if _MAP.search(t):
        speak("Mapping the wingman menu, stand by.")
        return map_menu(comms, snap)
    if _MAP_CARRIER.search(t):
        speak("Mapping the carrier and ATC menu, stand by. Hands off the radio menu.")
        return map_menu(comms, snap, kind="carrier")
    if _CHECK.search(t):
        return check_report(snap)
    if _ONE.match(t):
        return "You're one. Your first wingman is two."
    if INDEPENDENTS is not None:
        from common import independents
        oc = independents.parse_outcast(text)
        if oc is not None and not INDEPENDENTS.known(oc[0]) and oc[0] in independents.NUM_SAY:
            # 24 Oct: "Outcast 2, go independent" / "Outcast 2, engage ..." before he went
            # independent: he's still wingman 2, so it's the same as "two, ..."
            text = f"{independents.NUM_SAY[oc[0]]}, {oc[1]}"
            t = re.sub(r"[^a-z0-9' ]+", " ", text.lower())
        elif oc is not None:                            # "Outcast 2, attack my current target"
            return INDEPENDENTS.order(*oc)
    split = _split(text)
    if split is None or not split[2]:
        return None
    who, word, rest = split
    if INDEPENDENTS is not None:
        from common import independents
        if independents.INDEPENDENT.search(rest):       # "two, go independent"
            return _go_independent(snap, who)
        if isinstance(who, int) and INDEPENDENTS.known(who):
            return INDEPENDENTS.order(who, rest)        # "two, ..." after he went independent
        if who == "flight" and INDEPENDENTS.numbers():
            said = [x for x in INDEPENDENTS.order_all(rest) if x]
            _, err = resolve(snap, "flight")
            if err:                                      # nobody left in your group: Outcasts only
                return said[0] if said else ""
    if EXECUTOR is not None:
        from common import olympus_orders
        direct = olympus_orders.parse(rest)
        if direct is not None:
            said = _olympus_order(snap, who, *direct)
            if said is not None:
                return said
    order = parse_order(text)
    who, path = order
    if path is None and (word in _SOUNDALIKE_WHO or word in ("everyone", "everybody")):
        return None             # "to the left...", "everybody stop shooting": not for the wingmen
    return give_order(comms, snap, who, path)


def _olympus_order(snap: dict, who, kind: str, params: dict) -> Optional[str]:
    """Runs a direct order through Olympus. None = let the radio menu try it instead."""
    if kind == "lase_stop" and who in ("wingman", "flight"):
        lasing = EXECUTOR.lasing()          # "wingman, stop lasing": whoever is lasing
        if not lasing:
            return "Nobody's lasing."
        return EXECUTOR.lase_stop([{"id": i} for i in lasing])
    wing, err = resolve(snap, who)
    if err:
        return err
    ids = EXECUTOR.wing_ids(snap)
    for w in wing:
        w["id"] = ids.get(w["name"])
    seen = [w for w in wing if w["id"] is not None]
    if not seen:
        return f"Unable, I can't see {_who_say(who if who != 'wingman' else wing[0]['n']).lower()}."
    from common.orders import ORDERS, capture
    if not EXECUTOR.oly.connect():
        if kind == "attack" and params.get("which") == "mine":
            return None                                  # the menu's Engage > My Target
        ORDERS.record(_who_say(who), kind, "failed", "Olympus isn't running")
        return "Unable, Olympus isn't running."
    with capture() as sent:
        said = EXECUTOR.run(kind, params, seen, _who_say(who))
    ORDERS.record_outcome(_who_say(who), kind, sent, said)
    return said

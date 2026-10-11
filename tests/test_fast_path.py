"""What the fast path does with spoken phrases (None = handed to the LLM)."""
import unittest

import tests.fakes  # noqa: F401
from intelligence.fast_path import match_fast

SNAP = {"targets": [{"type": "T-72B", "id": 1, "dist_m": 3000}, {"type": "ZSU-23 Shilka", "id": 2, "dist_m": 4000}]}

# phrase -> (action, detail) ; detail is setting=value, the targets list, or the index
CASES = {
    # fire control
    "fire": ("CONSENT_TO_FIRE", None), "send it.": ("CONSENT_TO_FIRE", None),
    "and send it": ("CONSENT_TO_FIRE", None), "cleared hot": ("CONSENT_TO_FIRE", None),
    "fire at the ZSU": None, "don't fire": None,
    "cease fire": ("CEASE", None), "hold fire": ("CEASE", None), "C's fire.": ("CEASE", None),
    "seize fire": ("CEASE", None),
    # ROE and George's weapon
    "weapons free": ("GEORGE_SET", "roe=FREE"), "weapons hold": ("GEORGE_SET", "roe=HOLD"),
    "select missiles": ("GEORGE_SET", "weapon=MSL"), "gun": ("GEORGE_SET", "weapon=GUN"),
    "no weapon": ("GEORGE_SET", "weapon=NO_WPN"),
    "burst twenty": ("GEORGE_SET", "gun_burst=20"), "change burst": ("GEORGE_SET", "gun_burst=NEXT"),
    "rocket quantity four": ("GEORGE_SET", "rocket_qty=4"), "trajectory high": ("GEORGE_SET", "missile_traj=HI"),
    "LOAL": ("GEORGE_SET", "missile_method=LOAL"), "switch to RF": ("GEORGE_SET", "missile_type=RF"),
    # your own switches
    "action missiles": ("SELECT_HELLFIRE", None), "action my gun": ("SELECT_GUN", None),
    "my rockets": ("SELECT_ROCKETS", None), "de-action": ("SELECT_WAS_OFF", None),
    "master arm on": ("SET_ARMED", None), "master arm off": ("SET_DISARMED", None),
    "is master arm on": None, "master arm": None,
    # searches and targets
    "search": ("PHS_SEARCH", None), "search again": ("TADS_SEARCH", None),
    "search forward area": ("AREA_SEARCH", "FWD AREA"), "forward area.": ("AREA_SEARCH", "FWD AREA"),
    "radar targets search": ("AREA_SEARCH", "FCR TGTS"),
    "point search tango zero seven": ("POINT_SEARCH_AT", "T07"), "point search T07": ("POINT_SEARCH_AT", "T07"),
    "point search": ("POINT_SEARCH", None),
    "lock target 3": ("SELECT_TARGET_INDEX", 3), "select the second one": ("SELECT_TARGET_INDEX", 2),
    "engage targets 1, 3 and 4": ("ENGAGE_TARGETS", [1, 3, 4]),
    "engage targets one through four": ("ENGAGE_TARGETS", [1, 2, 3, 4]),
    "engage that one": None, "do not engage targets 1 and 2": None,
    "where is target 2": ("TARGET_INFO", 2),
    "next target": ("LIST_DOWN", None), "track it": ("TRACK_TARGET", None),
    "status": ("STATUS", None), "toggle wheel": ("SHOW_HIDE", None),
}


def detail(r):
    if r["action"] == "GEORGE_SET":
        return f"{r['setting']}={r['value']}"
    if r["action"] in ("AREA_SEARCH", "POINT_SEARCH_AT"):
        return r["value"]
    if r["action"] == "ENGAGE_TARGETS":
        return r["targets"]
    if r["action"] in ("SELECT_TARGET_INDEX", "TARGET_INFO"):
        return r["target_index"]
    return None


class FastPath(unittest.TestCase):
    def test_phrases(self):
        for phrase, want in CASES.items():
            with self.subTest(phrase=phrase):
                r = match_fast(phrase, SNAP)
                got = None if r is None else (r["action"], detail(r))
                self.assertEqual(got, want)


class CheatSheet(unittest.TestCase):
    def test_every_phrase_in_commands_md_is_instant(self):
        """COMMANDS.md promises its bold phrases are handled instantly; keep it honest."""
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "COMMANDS.md")
        with open(path, encoding="utf-8") as f:
            md = f.read()
        phrases = [p for row in re.findall(r"^\| (.+?) \|", md, flags=re.M)
                   for p in re.findall(r"\*\*(.+?)\*\*", row)]
        self.assertGreater(len(phrases), 50)
        for p in phrases:
            with self.subTest(phrase=p):
                self.assertIsNotNone(match_fast(p, SNAP))


if __name__ == "__main__":
    unittest.main()

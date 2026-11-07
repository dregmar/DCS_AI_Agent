"""
detection_probe.py - detection model step D0 (3 Nov 2026): what DCS's own sensors report.

    python detection_probe.py on        create the flag: DCS starts logging (comms hook every 10 s,
                                        Export.lua every 5 s) - fly the test mission
    python detection_probe.py off       remove the flag: logging stops
    python detection_probe.py report    summarise the two logs (paste or attach the output for Claude)

    --dcs "C:\\Users\\you\\Saved Games\\DCS"   the DCS Saved Games folder (default: ~\\Saved Games\\DCS;
                                              DCS.openbeta users pass theirs)

Needs the new george_comms_hook.lua (Scripts\\Hooks) and Export.lua (Scripts). Read-only in DCS:
nothing is spawned, changed or sent. Logs (in <DCS>\\Logs):
    george_detection_probe.txt          your side's AWACS / drones / units named JTAC, observer, FAC,
                                        recon, SOF: Controller:getDetectedTargets() + isTargetDetected(),
                                        terrain line of sight, the real time each query took
    george_detection_probe_export.txt   your aircraft: RWR, radar / locked targets, sighting system

Test mission (Caucasus): an E-3 orbiting; a friendly ground unit named "JTAC 1" on a ridge (and an
MQ-9 if available); enemy armour in the open AND behind terrain; a low helicopter, a high fighter, an
emitting SA-6; you in the Apache, then the Hornet. Fly 10-15 minutes, then "off" and "report".
Does not need main.py running.
"""
import argparse
import os
import re
import statistics
import sys
from collections import defaultdict
from typing import Dict, List, Optional

FLAG = os.path.join("Scripts", "george_detection_probe.flag")
HOOK_LOG = os.path.join("Logs", "george_detection_probe.txt")
EXPORT_LOG = os.path.join("Logs", "george_detection_probe_export.txt")


def default_dcs_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Saved Games", "DCS")


def set_flag(dcs_dir: str, on: bool) -> str:
    scripts = os.path.join(dcs_dir, "Scripts")
    if not os.path.isdir(scripts):
        return f"No Scripts folder in {dcs_dir}. Pass --dcs with your DCS Saved Games folder."
    path = os.path.join(dcs_dir, FLAG)
    if on:
        with open(path, "w", encoding="utf-8") as f:
            f.write("detection probe D0\n")
        return f"Probe ON ({path}). Fly the test mission; logs in {os.path.join(dcs_dir, 'Logs')}."
    if os.path.exists(path):
        os.remove(path)
        return "Probe OFF. Run 'python detection_probe.py report'."
    return "Probe was already off."


def _kv(fields: List[str]) -> Dict[str, str]:
    return dict(f.split("=", 1) for f in fields if "=" in f)


def _num(v: Optional[str]) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_hook_log(text: str) -> dict:
    """The hook log -> per-observer statistics, line-of-sight agreement and query cost."""
    costs: List[float] = []
    problems: List[str] = []
    obs: Dict[str, dict] = {}
    for line in text.splitlines():
        m = re.match(r"^== \S+\s+query ([\d.]+) ms", line)
        if m:
            costs.append(float(m.group(1)))
            continue
        if line.startswith("DPROBE ") and line.strip() not in ("DPROBE",):
            problems.append(line.strip())
            continue
        f = line.split("\t")
        if f[0] == "OBS" and len(f) >= 6:
            o = obs.setdefault(f[1], {"type": f[2], "why": f[3], "snapshots": 0, "detected": [], "det": [],
                                      "los": [], "errors": []})
            o["snapshots"] += 1
            n = re.search(r"detected (\d+)", f[5])
            o["detected"].append(int(n.group(1)) if n else 0)
            if len(f) > 6 and f[6].startswith("error"):
                o["errors"].append(f[6])
        elif f[0] == "DET" and len(f) >= 7:
            kv = _kv(f[6:])
            obs.setdefault(f[1], {"type": "?", "why": "?", "snapshots": 0, "detected": [], "det": [], "los": [],
                                  "errors": []})["det"].append(
                {"id": f[2], "type": f[3], "coalition": f[4], "category": f[5], "visible": kv.get("visible") == "true",
                 "typeKnown": kv.get("typeKnown") == "true", "distKnown": kv.get("distKnown") == "true",
                 "range": _num(kv.get("range")), "posErr": _num(kv.get("posErr")), "los": kv.get("los", "")})
        elif f[0] == "LOS" and len(f) >= 7:
            kv = _kv(f[4:])
            obs.setdefault(f[1], {"type": "?", "why": "?", "snapshots": 0, "detected": [], "det": [], "los": [],
                                  "errors": []})["los"].append(
                {"id": f[2], "type": f[3], "range": _num(kv.get("range")), "los": kv.get("los") == "true",
                 "detected": kv.get("detected") == "true"})
    return {"costs": costs, "problems": problems, "observers": obs}


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.0f}%" if whole else "-"


def report_hook(parsed: dict) -> List[str]:
    out = ["== DCS detection (comms hook) =="]
    costs = parsed["costs"]
    if costs:
        out.append(f"queries: {len(costs)}, real time per query: median {statistics.median(costs):.1f} ms, "
                   f"worst {max(costs):.1f} ms")
    else:
        out.append("no queries logged (flag never seen, no mission, or the hook isn't the new one)")
    for p in sorted(set(parsed["problems"]))[:5]:
        out.append(f"problem: {p}")
    if not parsed["observers"]:
        out.append("no observers found: name a unit JTAC / observer / FAC / recon / SOF, or add an AWACS / drone")
    for name, o in parsed["observers"].items():
        det, los = o["det"], o["los"]
        out.append(f"observer {name} ({o['type']}, {o['why']}): {o['snapshots']} snapshots, detected per snapshot "
                   f"median {statistics.median(o['detected']) if o['detected'] else 0:g}, "
                   f"max {max(o['detected']) if o['detected'] else 0}")
        for e in sorted(set(o["errors"]))[:3]:
            out.append(f"  {e}")
        if det:
            kinds = defaultdict(int)
            for d in det:
                kinds[d["category"]] += 1
            errs = [d["posErr"] for d in det if d["posErr"] is not None]
            rngs = [d["range"] for d in det if d["range"] is not None]
            out.append(f"  detections logged {len(det)}: visible {_pct(sum(d['visible'] for d in det), len(det))}, "
                       f"type known {_pct(sum(d['typeKnown'] for d in det), len(det))}, "
                       f"distance known {_pct(sum(d['distKnown'] for d in det), len(det))}; by category "
                       + ", ".join(f"{k or '?'}: {n}" for k, n in sorted(kinds.items())))
            if rngs:
                out.append(f"  range of detections: median {statistics.median(rngs) / 1000:.1f} km, "
                           f"max {max(rngs) / 1000:.1f} km")
            if errs:
                out.append(f"  last-position error: median {statistics.median(errs):.0f} m, max {max(errs):.0f} m")
            blocked = [d for d in det if d["los"] == "false"]
            if blocked:
                out.append(f"  detected WITHOUT terrain line of sight: {len(blocked)} "
                           f"(e.g. {blocked[0]['type']} at {(blocked[0]['range'] or 0) / 1000:.1f} km)")
            coal = defaultdict(int)
            for d in det:
                coal[d["coalition"]] += 1
            out.append("  by coalition: " + ", ".join(f"{k or '?'}: {n}" for k, n in sorted(coal.items())))
        if los:
            vis_det = sum(1 for x in los if x["los"] and x["detected"])
            vis_not = sum(1 for x in los if x["los"] and not x["detected"])
            hid_det = sum(1 for x in los if not x["los"] and x["detected"])
            hid_not = sum(1 for x in los if not x["los"] and not x["detected"])
            out.append(f"  enemy ground within 20 km: in sight & detected {vis_det}, in sight NOT detected {vis_not}, "
                       f"hidden & detected {hid_det}, hidden & not detected {hid_not}")
    return out


def report_export(text: str) -> List[str]:
    out = ["== your aircraft's sensors (Export.lua) =="]
    if not text.strip():
        return out + ["no lines (flag never seen, or the Export.lua isn't the new one)"]
    aircraft = sorted(set(re.findall(r"aircraft (\S+)", text)))
    out.append("aircraft: " + (", ".join(aircraft) or "?"))
    for name in ("LoGetTWSInfo", "LoGetTargetInformation", "LoGetLockedTargetInformation", "LoGetSightingSystemInfo"):
        na = len(re.findall(name + r": not available", text))
        err = re.findall(name + r": error (.*)", text)
        nil = len(re.findall(name + r": nil", text))
        if na or err or nil:
            out.append(f"{name}: not available {na}, nil {nil}, errors {len(err)}" + (f" ({err[0][:80]})" if err else ""))
    emitters = [int(x) for x in re.findall(r"RWR mode \S+, (\d+) emitters", text)]
    if emitters:
        out.append(f"RWR: {len(emitters)} readings, emitters max {max(emitters)}")
    rwr = re.findall(r"  RWR (\{.*\})", text)
    if rwr:
        out.append("RWR example: " + rwr[0][:200])
    radar = [int(x) for x in re.findall(r"radar targets (\d+)", text)]
    if radar:
        out.append(f"radar targets: {len(radar)} readings, max {max(radar)}")
    tgt = re.findall(r"  TGT (\{.*\})", text)
    if tgt:
        out.append("radar target example: " + tgt[0][:200])
    lock = re.findall(r"  LOCK (\{.*\})", text)
    if lock:
        out.append("locked target example: " + lock[0][:200])
    sight = re.findall(r"sight (\{.*\})", text)
    if sight:
        out.append("sighting system example: " + sight[-1][:200])
    return out


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def report(dcs_dir: str) -> str:
    lines = report_hook(parse_hook_log(_read(os.path.join(dcs_dir, HOOK_LOG))))
    lines += [""] + report_export(_read(os.path.join(dcs_dir, EXPORT_LOG)))
    flag = os.path.exists(os.path.join(dcs_dir, FLAG))
    lines += ["", "probe is still ON (run 'off' when done)" if flag else "probe is off"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Detection probe D0: what DCS's own sensors report.")
    ap.add_argument("action", choices=["on", "off", "report"])
    ap.add_argument("--dcs", default=default_dcs_dir(), help="DCS Saved Games folder")
    a = ap.parse_args(argv)
    if a.action == "report":
        print(report(a.dcs))
    else:
        print(set_flag(a.dcs, a.action == "on"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

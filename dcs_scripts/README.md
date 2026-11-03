# DCS scripts

These are COPIES, kept here so they are versioned with the Python side. DCS reads the live ones
from `%USERPROFILE%\Saved Games\DCS\Scripts\` (and `...\Scripts\Hooks\` for the hook). `snapshot.bat`
refreshes these copies from there every time it runs, so edit the live files (or copy an edited copy
there straight away).

| File | What it does | Sends to |
|---|---|---|
| `Export.lua` | Your aircraft (name, position, heading, height above ground, vertical speed, ground velocity, g, fuel, stores), the mission route (LoGetRoute), your group, wingmen and own unit id, and from LoGetWorldObjects: enemy ground units, air defence and aircraft (with the time of the last complete scan, so stale lists are noticed), Olympus units, your own side's and neutral units, and the nearest sling-load cargo. While `george_fast_telemetry.flag` exists (an Instructor exercise) it adds a compact flight packet 10 times a second. Loads the other export scripts at the end | UDP 5005 |
| `george_ai_export.lua` | George's wheel and Target / Search / Point lists (cockpit indicator 26), read-only | UDP 5007 |
| `george_export.lua` | Your WPN page text: actioned weapon and selected sight, read-only | UDP 5006 |
| `george_comms_hook.lua` | Goes in `Scripts\Hooks\`, not `Scripts\`. Reads and drives the DCS radio (comms) menu (MENU / CAPTION / ITEM out; OPEN / SELECT n / BACK / CLOSE / GROUP in) and, from the loaded mission and the mission scripting state, sends: ROUTE, FLIGHT (your flight line-up), BRIEF (situation, your coalition's task, sortie), TYPES (every unit type's DCS attributes), AIRBASES, FUEL (DCS fuel in pounds), CALLSIGN, PLAYERS, EVENTS (unit kills and losses) and LOADED (mission start / restart) | UDP 5011 out, 5012 in |
| `george_huey_export.lua` | UH-1H only: gauges, caution lamps, flares, gunners' Crew Status panel; accepts the game's crew commands (gunner ROE / burst, crew panel, autopilot) and nothing else | UDP 5008 out, 5009 in |

To restore on a new PC or after a DCS update overwrote them: copy the export scripts into
`Saved Games\DCS\Scripts\` and the hook into `Saved Games\DCS\Scripts\Hooks\`. Export.lua is read at
each mission start; the hook only when DCS starts (restart DCS after changing it).
Status files for each are written to `Saved Games\DCS\Logs\george_*_status.txt`.

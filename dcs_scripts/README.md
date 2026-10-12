# DCS scripts

These are COPIES, kept here so they are versioned with the Python side. DCS reads the live ones
from `%USERPROFILE%\Saved Games\DCS\Scripts\`. `snapshot.bat` refreshes these copies from there
every time it runs, so edit the live files, not these.

| File | What it does | Sends to |
|---|---|---|
| `Export.lua` | Flight data, own heading, remaining stores (LoGetPayloadInfo), nearby enemy units (LoGetWorldObjects); loads the other scripts at the end | UDP 5005 |
| `george_ai_export.lua` | George's wheel and Target / Search / Point lists (cockpit indicator 26), read-only | UDP 5007 |
| `george_export.lua` | Your WPN page text: actioned weapon and selected sight, read-only | UDP 5006 |
| `george_huey_export.lua` | UH-1H only: gauges, caution lamps, flares, gunners' Crew Status panel; accepts the game's crew commands (gunner ROE / burst, crew panel, autopilot) and nothing else | UDP 5008 out, 5009 in |

To restore on a new PC or after a DCS update overwrote them: copy all four into
`Saved Games\DCS\Scripts\` and start a new mission (Export.lua is only read at mission start).
Status files for each are written to `Saved Games\DCS\Logs\george_*_status.txt`.

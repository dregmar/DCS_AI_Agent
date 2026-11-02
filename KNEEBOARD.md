# VR kneeboard (OpenKneeboard)

The program serves a kneeboard page on your PC only: **http://127.0.0.1:5000/**

## Set up once
1. Start the program (main.py). The console says `[KNEEBOARD] http://127.0.0.1:5000/`.
2. In OpenKneeboard: Settings > Tabs > Add tab > **Web Dashboard**, URL `http://127.0.0.1:5000/`.
3. It's also fine in a normal browser on the same PC (useful for checking).

Port taken by something else? Set `KNEEBOARD_PORT` in config.py (and the tab's URL). Off: `KNEEBOARD_ENABLED = False`.

## Look (redesign 1 Nov 2026)
Amber command-and-control style: a top bar with the seven tabs, DCS LIVE / NO DCS / PROGRAM OFFLINE
and the PC's local clock (the feeds carry no mission clock). The Map tab is a dashboard: on the left
the mission overview, tactical summary, threat level and DCS links; the map in the middle; on the right
the selected contact and the threat assessment (air defence with rings, nearest edge first, marked
INSIDE / NEAR, and enemy aircraft); and, on a tall screen (900 px or more), a row of summaries of the
other six pages (tap one to open it). In OpenKneeboard's tab (about 1024 x 768) there is one side
column (threat level first) and no summary row, so the map stays big. **⛶** shows the map alone
(the contact panel then floats over it when something is selected). All of it is remembered.
The threat level is worked out on the page from the map: CRITICAL while a critical alert is up, HIGH
inside a threat ring, ELEVATED within 5 km of a ring or an enemy aircraft within 37 km (20 nm), LOW
when hostiles are known, else CLEAR.

## Pages (tap the tabs at the top)
- **Map**: you (white, pointing your way), your track (amber), route with waypoint numbers (next one
  amber), named waypoints (lavender), Outcasts (cyan), Overlord's flights (blue), ground units
  (squares) and aircraft (triangles, altitude in thousands of feet) coloured by side: red hostile,
  green neutral, yellow unknown (the side comes from the tactical picture; Export.lua's raw lists are
  enemies, so red). The JTAC target is an amber crosshair; threat rings are dashed red. Range rings in
  nm (jets) or km (helicopters). Buttons: **+ / −** zoom (2.5 to 160), **N / TU** north up or track
  up, **LYR** layers (threat rings, your track, route, named waypoints, ground, aircraft, ships,
  friendly flights, labels), **⛶** map only / dashboard. Your choices are remembered.
  **Tap a marker** to see what the map knows about it in the Selected contact panel (side, kind,
  bearing and distance, status, threat ring and whether you're inside it, confidence, George's list
  number); tap empty map to clear. This is display only: nothing is ordered from a tap (that is
  feature 3, still to do), and "kneeboard, contact 42" still gives the full track details.
  **From the tactical picture** (31 Oct, step T5) while it's running: enemy units are drawn from the
  picture, one marker per DCS unit. A filled marker is in DCS's latest scan; a **hollow** one is where
  the unit was last seen (out of the lists' range, or lost), with how long ago ("1 min ago"). Green =
  neutral, yellow = unknown (once Export.lua reports sides). "G2" = on George's Target List as #2; "T42" (zoomed in) =
  the unit's track number. George's entries the picture can't place are listed under the top line
  ("George: #2 ARMORED: class only ..."), never drawn. The JTAC crosshair follows its unit as it
  moves. If the picture stops updating, the map goes back to Export.lua's lists by itself.
- **Brief**: the DCS mission's briefing (arrives when the mission loads), your campaign briefing
  (data/campaign_briefing.json), and this aircraft's procedures (closed; tap to open). When George or
  Overlord answers a briefing question, the entry it came from is marked yellow and scrolled to, with
  the question at the top. Frequencies and grids are highlighted (amber).
  After an answer from an imported aircraft manual, that manual section is shown in full under
  "Manual: <section>" (only that section: a manual is thousands of entries).
- **Checklist**: the running Apache / Huey checklist: done items ticked (green tick = George saw it,
  white = you said check, gear = George set it, dash = skipped), the current item in yellow, and what
  George sees for the items still to come ("reads off" in red, "reads battery" in green). When a
  checklist starts, the kneeboard turns to this page (`KNEEBOARD_AUTO_CHECKLIST = False` stops that).
  After it ends, the last one stays up.
  **A-10C II and F/A-18C** (no voice, the page only): cold start, taxi, takeoff, landing and more,
  from the DCS manuals. The page follows along by itself: an item with a cockpit check ticks when
  DCS-BIOS shows it done, an item you do or watch (grey dot) is passed when the next checked item is
  done, and the next list comes up when one is finished. "Show the taxi / carrier launch / shutdown
  checklist" picks one. Switch positions are read from DCS-BIOS's own data
  (`Saved Games\DCS\Scripts\DCS-BIOS\doc\json`); run `python jet_probe.py` once per jet and flip the
  switches to check they read right.
- **Comms**: everything said, newest at the bottom: you, the crew, Overlord. "Picture" lines (dimmed)
  are changes in the tactical picture: enemy units destroyed or gone, and new / lost air defence,
  aircraft and other serious threats ("New: Osa 9A33 ln (T51), 9 km north of you."), plus the
  picture going stale or current again. Nothing is spoken. Grids, lat/long,
  frequencies, steers, elevations and "angels" are highlighted, so a JTAC grid can be read off
  instead of remembered.
- **Training** (1 Nov: one training mode): training mode on / off and, if training targets can't be
  spawned, why (no Game Master login, Olympus off, other players); the training target groups (units
  alive / spawned, minutes since the spawn); the instructor's exercise with its live values (green in
  tolerance, red out); then **Scores**: the last result with every deduction, your levels, recurring
  weaknesses, the suggested next exercise, and the training targets you destroyed with the time after
  the spawn. "Show the training / scores / instructor" opens it.
- **Status**: green / red for DCS telemetry, DCS-BIOS, the comms hook, DCS fuel, Olympus, Ollama and the
  voices, with how long ago each was heard; the last speech recognition time.

## Voice (any aircraft; whole sentences only, answered by the page, not a voice)
- "Show the map / briefing / manual / checklist / comms / status / training" (also "kneeboard, briefing", "open the comms log")
- "Show the shutdown checklist" / "show the before take-off checklist": shows that list without starting it
- "Page down / page up", "next page / previous page", "scroll to the top / bottom"
- "Scroll down / up" too, except in the Apache, where it moves George's target list: there say "kneeboard, scroll down" or "page down"
- A checklist still starts the usual way ("before start checklist").
- "Kneeboard, contact 42" (also "contact tango four two", "track 42"): the details of unit T42 on the
  map: status and why, where, side, how we know (DCS's world scan is simulator truth, not a sensor
  detection), when seen, how sure, George's list entry, and why it's a threat. "Kneeboard, close
  contact" closes it. A new mission closes it too (track numbers start again).

## Notes
- Threat rings are APPROXIMATE (typical maximum range of each system); air defence DCS doesn't
  name gets a 5 km ring.
- No map tiles yet (plain grid): everything stays on your PC.
- The page updates about 5 times a second, only when something changed; it's light for the GPU.

# Both aircraft: radio menu and navigation

These work the same in the Apache and the Huey.

## Radio menu (the DCS comms menu, the \ key)

Needs `Saved Games\DCS\Scripts\Hooks\george_comms_hook.lua` for George to read the menu. Without it
he can only press the keys (\, F1–F12) and can't read the choices back.

| Say | George |
|---|---|
| **radio menu** · **comms menu** · **radio** | Opens the menu and reads the choices: "Main: 1 Flight, 2 ATC, 3 Ground Crew, 4 Other." |
| **ATC** · **Kobuleti** · **ground crew** · **rearm** | While it's open: picks the item you name, then reads the next page |
| **five** · **F5** · **option 5** | While it's open: picks item 5 |
| **back** · **previous menu** | Previous page |
| **close menu** · **exit menu** | Closes it |
| **radio, ATC, Kobuleti, request startup** | Several pages in one sentence |
| **request taxi** · **request startup** · **ask ground crew to rearm** | Finds it under ATC / Ground Crew; stops and reads the choices where it has to ask (e.g. which airfield) |

## Wingmen

Say who, then the order. Numbering is the radio one: **two** is your first wingman (DCS's
"Wingman 2"), **three**, **four**; **flight** (or **everyone**) is all of them; **wingman** alone
works when you have just one. George checks first that you have that wingman and that he's still
up, then picks the order in the radio menu by name (needs george_comms_hook.lua). He says nothing
when it went through, and tells you when it couldn't ("Three is down.", "Which wingman?",
"Negative, we're a two ship, there's no three.").

DCS doesn't list every wingman on his own: a 3-ship Huey had only "Wingman 2", a 4-ship Apache
"Wingman 2" and "3". George then says "Three isn't in the radio menu. Tell flight or two."
In tests (19 Oct) AI Apache and Huey wingmen didn't act on **engage my target** (with the target
lased, stored or tracked) and DCS forum reports say the same of their attack orders; the
order is sent, the AI doesn't follow it. **engage ground targets** / **air defences** are the ones
to try.

| Say (after "two," / "flight," ...) | Radio menu |
|---|---|
| **two, engage my target** · **flight, attack my target** | Engage > My Target |
| **two, engage my enemy** · **flight, get whoever's shooting at me** | Engage > My Enemy |
| **two, take out the air defences** · **flight, engage SAMs** | Engage > Air Defences |
| **two, engage ground targets** · **flight, attack the armor** | Engage > Ground Targets |
| **flight, engage bandits** | Engage > Bandits |
| **two, mission and rejoin** · **two, mission and RTB** | Engage > Mission and Rejoin / RTB |
| **two, return to base** · **flight, RTB** · **three, go home** | Go To > Return to Base |
| **two, resume route** | Go To > Route |
| **two, pincer left** · **flight, go pincer right** | Go Pincer > Left / Right |
| **flight, cover me** · **two, cover my six** | Cover Me |
| **two, hold position** · **flight, hold here** | Hold Position |
| **flight, rejoin** · **two, join up** · **everyone, form up** | Rejoin Formation |
| **flight, go trail** · **flight, go echelon** · **flight, go spread** · **flight, go overwatch** | Go Formation (flight only) |
| **two, recon 5 km** | Reconnaissance (1, 2, 3, 5, 8 or 10 km; single wingmen only) |
| **two, jettison weapons** | Jettison Weapons (only with "weapons" / "stores" said) |

### With DCS Olympus running

These go through DCS Olympus (the Olympus mod enabled in DCS; the web map doesn't need to be
open). They reach every wingman, Wingman 4 included, and attack the unit you mean. Same checks
first, same silence when it worked. Without Olympus, **two, attack my target** falls back to the
radio menu; the rest say "Unable, Olympus isn't running."

| Say | Olympus |
|---|---|
| **two, attack my target** | Attacks your target: George's priority target in the Apache, else the enemy on your nose |
| **three, take out the ZSU at two o'clock** · **two, hit the tank** | The nearest unit of that kind (ZSU / triple A, SAM, tank, APC, truck, infantry, artillery), at that clock if you say one |
| **flight, hit the trucks** · **flight, engage everything** | One target each, most dangerous first |
| **two, follow me** · **three, stay on my left at 200 metres** · **flight, on me** | Formation on you (two right, three left by default) |
| **two, go to waypoint 3** | Flies to that mission waypoint |
| **two, land next to me** · **three, land at waypoint 2** | Lands there |
| **flight, weapons hold** · **two, weapons free** · **three, return fire** | Their rules of engagement |
| **two, lase my target** · **two, lase the tank, code 1688** · **two, stop lasing** | Laser spot (default code 1688) that follows the target until it's gone |
| **two, mark my target with smoke** · **flight, pop smoke** | Red smoke on the target · green smoke on you (colour can be said) |

`olympus_probe.py` checks the connection (`--smoke` pops one green smoke in front of you).

| Say | George |
|---|---|
| **flight check** · **check wingmen** · **who's in my flight** | "Two, AI, up. Three, AI, down." (from the mission and what DCS still shows) |
| **map wingman menu** | Walks the Wingman / Flight / Element menus (only opens submenus, never sends an order) and saves them to `logs\wingman_menu_*.txt` |

### Independent wingmen ("Outcast")

DCS ignores Olympus orders for wingmen in a group you lead. Make a wingman independent and Olympus
can task him:

| Say | Does |
|---|---|
| "two, go independent" · "three, go independent" · "flight, go independent" | Olympus clones him (same aircraft and loadout) into an AI group of his own, removes the original, and he joins your wing as **Outcast 2** (3, 4...) |
| "Outcast 2, attack my current target" · "Outcast 3, take out the ZSU at two o'clock" · "Outcast 2, attack target 3" (George's list) | attacks it |
| "Outcast 2, engage at will" | weapons free; takes the most dangerous enemy within 6 km of him, one after another. Says at once what he's going after ("engaging at will, 3 in range. First, Shilka.") or "no targets within 6 kilometres of me"; "no more targets, rejoining" when done |
| "Outcast 2, go independent" (before he is) | same as "two, go independent"; any "Outcast N, ..." order goes to wingman N until he's independent |
| "Outcast 2, engage air defences" · "… take out the SAMs" · "… destroy the defences" | like engage at will, but only air defence (SAMs, radars, AAA, MANPADS) |
| "Outcast 2, go destroy the tanks" · "… engage armour to the north-west" | the nearest of that kind within 15 km, from every unit DCS lists (not only what your sensors see), in that direction if you say one: "Outcast 2, engaging T-72B, 6 kilometres north west of you." |
| "Outcast 3, RTB" | lands at the mission's start point |
| "Outcast 2, rejoin" · "follow me" · "hold position" · "go to waypoint 3" · "land next to me" | as said |
| "Outcast 2, weapons hold / free" · "lase my target" · "stop lasing" · "status" | as said |

- "Two, ..." still works after he's gone independent (it goes to Outcast 2), and "flight, ..." goes to
  every Outcast as well as the wingmen still in your group.
- It can't be undone, and the radio menu no longer controls him. If the clone doesn't show up within
  20 s, the original stays in your flight ("couldn't go independent"). Your own aircraft is never cloned.
- Each clone costs 10 Olympus spawn points if spawns are restricted. George stays quiet when an order
  went through and speaks when it didn't.

## Overlord (C2, command radio)

Start with **"Overlord"**; only then does Overlord answer, in his own voice (Joe,
en_US-joe-medium.onnx). Everything else, wingman orders included, is George or the Huey crew on the
intercom (Ryan). Overlord sends flights through DCS Olympus only (no radio-menu fallback).
`python voice_probe.py` plays one line in each voice.

"Requesting" is optional. Where: "15 kilometres north-east of my position", "5 miles west of me",
"on waypoint 3", "on my position". For SEAD and CAS, a direction alone ("CAS to my north") means
the nearest targets that way, and no place at all ("Overlord, requesting SEAD") the nearest targets
anywhere (SEAD within 75 km, CAS within 25 km). "CAS on my target" with no target known takes the
nearest enemy within 10 km. For CAP, a direction alone is 10 km that way, and no place is over you.

| Say | Overlord sends |
|---|---|
| "Overlord, requesting SEAD strike north of my current position" | **Weasel**: 2 F-16C with HARMs, on the air-defence radars within 15 km of the point |
| "Overlord, requesting CAS on my target" · "... close air support 5 km east of me" | **Hog**: 2 A-10C, on the enemy ground units within 3 km of the point |
| "Overlord, requesting CAP over waypoint 4" · "... CAP north of me" · "... CAP" (over you) | **Eagle**: 2 F-15C, orbit there, engage enemy aircraft within 40 km |
| "Overlord, requesting escort" · "... fighter cover" | **Shield**: 2 F-16C, stay over you, engage enemy aircraft within 30 km of you |
| "Overlord, picture" · "… bogey dope" · "… any bandits?" | enemy aircraft around you, grouped: "2 ship Mi-24V, 25 kilometres north of you, 500 feet" (or "picture clean") |
| "Overlord, status" | what each flight is doing |
| "Overlord, cancel CAS" · "cancel all" · "cancel" (when only one is out) | that flight goes home |

- Flights spawn airborne 30–45 km back on your side, weapons free. One flight of each kind at a time;
  each costs 20 Olympus spawn points if spawns are restricted.
- **SEAD and CAS** take their targets one at a time. Overlord calls "<type> destroyed", then "area
  clear" (they go home). Nothing there: no flight is sent.
  - SEAD skips IR-only systems (Strela, Igla, Avenger), since a HARM can't home on them.
  - CAS takes the most dangerous first (AAA, then armour, then trucks). "My target" is George's
    priority target (else the contact on your nose), attacked first. The big radar SAMs (SA-6,
    SA-11, SA-10...) are left to SEAD; Overlord warns you if one is within 15 km.
- **CAP and escort** call "committing on <type>, 25 kilometres north of you", then "splash one
  <type>", and go back on station. The escort moves with you (every 5 km). Home after 30 minutes.
- "<callsign> flight is down" when all its aircraft are gone.

Questions to Overlord ("Overlord, what's our package priority?", "Overlord, what's the ROE in sector
Bravo?") are answered from the campaign and mission briefings, below. This works even with Olympus off.

## Briefing questions (George, Huey co-pilot, Overlord)

| Say | Answer from |
|---|---|
| "What's our package priority today?" · "What's the ROE in sector Bravo?" · "What's the frequency for the JTAC?" · "Where's the FARP?" | the campaign and mission briefings |
| "How do I start the engines?" · "Can you fire rockets?" · "What's LOBL?" | also the procedures and checklists for the aircraft you're in (not Overlord) |

- **Where it comes from** (folder `data\`, picked up within 2 s of a change, no restart):
  - `campaign_briefing.json`: yours. Fill it by hand (format: `data\examples\campaign_briefing.sample.json`)
    or from a text file: `python briefing_import.py my_briefing.txt` (headings become entries).
  - `mission_briefing.json`: written by the program from the loaded DCS mission (situation, your
    coalition's task, sortie), through george_comms_hook.lua.
  - `apache_procedures.json`: how George works and AH-64D procedures. Add your own; a
    `huey_procedures.json` in the same format is used in the Huey.
  - The crews' checklists.
- An entry's `"say"` is spoken as written. Without it, a short text is read out, and a long one is
  summarised by the local model from the best matches. Any number the model says must be in the
  briefing, otherwise the briefing text itself is read (no invented frequencies or codes).
- Not found: "That's not in the briefing." Live questions ("where's the priority threat", "what are
  the threats", target numbers) stay with George.
- After a model summary the crew's prompt is reloaded in the background (Ollama keeps one prompt
  cached), so a free-form command in the next few seconds can be slower.

## Aircraft manuals (A-10C, F/A-18C, Supercarrier; any module with a PDF manual)

Import a DCS manual once (needs `pip install pypdf`):

    python manual_import.py "<DCS>\Mods\aircraft\FA-18C\Doc\<manual>.pdf" --aircraft FA-18C_hornet
    python manual_import.py "<A-10C II manual>.pdf" --aircraft A-10C_2 A-10C
    python manual_import.py "<Supercarrier manual>.pdf" --aircraft FA-18C_hornet --name supercarrier

It splits the PDF by its bookmarks into `data\manuals\<name>.json` (contents, index, legal pages and
tables left out; `--dry-run` shows the counts first). `--aircraft` = the DCS type names it's for (the
console's aircraft name). Then, in that aircraft, any question goes to it:

| Say | Answer |
|---|---|
| "How do I employ the Maverick?" · "How do I align the INS?" · "What does the master mode switch do?" | a short spoken summary from the manual (every number must be in the text) |

The section it came from is shown in full on the kneeboard's Brief page ("show the briefing" or
"show the manual"), marked yellow. Overlord never answers from manuals. These files are Eagle
Dynamics' text: they stay on your PC.

## Position (any aircraft; co-pilot / George or Overlord)

| Say | Answer |
|---|---|
| "What's our current grid?" · "where are we?" · "say our grid" · "grid" | "Grid 38 Tango, Lima Mike, 7 2 1 8, 4 0 1 8." (MGRS, 8 digits = 10 m) |
| "give me a six / ten digit grid" | 6 or 10 digits |
| "what's our position in lat long?" | "North 41 degrees 54.2, East 43 degrees 27.5." |
| "say bullseye" | "Bullseye 106 for 21." (true bearing and nautical miles from your coalition's bullseye; needs Olympus) |
| "Overlord, request our grid" · "Overlord, say bullseye" | the same, from Overlord on the radio |

The grid is worked out from your aircraft's position (the same MGRS grid as the F10 map).

**Other aircraft** (not the Apache or Huey, e.g. the Hornet): there's no crew, so George and the
crew stay out of it (no wheel keys, no Apache callouts). Navigation, grid, briefing questions, the
radio menu, wingmen and Overlord all work.

## Named waypoints and the objective (any aircraft)

| Say | Does |
|---|---|
| "Mark our current position as waypoint Alpha" · "mark here as Bravo" | saves where you are: "Waypoint Alpha marked. Grid 38 Tango, ..." |
| "Set grid 38T LM 7218 4018 as waypoint Bravo" · "set grid Lima Mike 721 401 as Charlie" · "set grid 721 401 as Delta" | saves that grid (zone and square default to where you are); reads the grid back, with steer and distance |
| "The objective is waypoint 3" · "set grid 7218 4018 as the objective" | the objective |
| "Where is Alpha?" · "how far to Bravo?" · "bearing to the objective" · "steer to Charlie" | "Alpha, 11 o'clock, steer 353, 8.4 miles, 5 minutes." |
| "List waypoints" · "delete waypoint Alpha" · "clear all waypoints" | |

Names are the NATO alphabet and "objective". They're kept (data/user_waypoints.json) until you
clear them. Overlord and the Outcasts use them: "Overlord, CAS on waypoint Alpha", "Overlord, SEAD on
the objective", "Outcast 2, go to waypoint Bravo", "Outcast 3, land at Charlie".

## Nearest field, debrief (any aircraft)

| Say | Answer |
|---|---|
| "Nearest friendly airfield" · "divert" · "where's the nearest base" | "Nearest friendly field, Kutaisi, 2 o'clock, steer 045, 23 miles, 9 minutes." |
| "Nearest FARP" · "nearest helipad" · "nearest friendly ship" | FARPs/helipads only, or ships only |
| "Overlord, request vector to nearest friendly field" | the same from Overlord |
| "Debrief" · "mission summary" · "how did we do" | kills (George, Outcasts, Overlord's flights), losses, missiles fired, fuel; also saved to logs/debrief_*.txt |

The airfield list comes from the mission (george_comms_hook.lua), with coalitions: no Olympus
needed. A debrief file is also written when a new mission loads and when the program closes.

**Overlord on his own:** enemy aircraft coming within 40 km get "Overlord, pop-up group, 2 ship
Mi-24V, 30 kilometres north of you, 500 feet", and one inside 15 km "Overlord, bandit, Mi-24V,
12 kilometres north of you, closing". Each once (again if it leaves and comes back after 5
minutes). Off: OVERLORD_AIR_WARNINGS = False in config.py.

**Target types:** the comms hook asks DCS what every unit type in the mission is (its own
attributes: tank, IFV, SAM radar, AAA, MANPADS, infantry, truck...). Threat ranking, SEAD radars and
"the tank" / "the truck" use that, so types never seen before work without being added to the code.
They're remembered in data/unit_types.json.

## Jets: A-10C and F/A-18C (no crew; per-aircraft settings)

What you fly sets the numbers (common/aircraft.py; change single values with
`AIRCRAFT_OVERRIDES = {"FA-18C_hornet": {"reserve_lbs": 3500}}` in config.py):

| | Helicopters | A-10C | F/A-18C (and other jets) |
|---|---|---|---|
| Outcast after "go independent" | 60 m above ground, 45 m/s | 1,500 m, 150 m/s | 6,000 m, 230 m/s (other jets 220) |
| Formation (two / three) | 60 m back, 60 m out | 120 m back, 250 m out | 150 m back, 300 m out |
| Overlord's escort | orbits over you | flies formation on you, spawns 10 km back | formation, 12 km back |
| Distances spoken | kilometres | miles (nautical) | miles |
| Enemy aircraft altitude | "500 feet" | "angels 18" | "angels 18" |
| Pop-up / bandit calls | 40 / 15 km | 50 / 20 miles | 50 / 20 miles |
| Joker / bingo calls | no | reserve 1,500 lb, joker +1,000, burn 2,600 lb/h at 300 kt | reserve 3,000 lb, joker +1,500, burn 4,500 lb/h at 420 kt |

**Joker and bingo** (Overlord, on his own, once each per sortie; again after you take on fuel):
"Overlord, joker fuel, 5,500 pounds. Home plate CVN-73, 100 miles, steer 180." then "Overlord,
bingo fuel, 4,000 pounds. ... Recommend RTB." Bingo = fuel to fly home at cruise + the reserve;
joker = bingo + the margin. Home plate = the friendly field or ship you started at (a carrier's
current position), else your first route point, else the nearest friendly field. Ask any time:
"say bingo" / "what's my joker" / "Overlord, say bingo" -> "Fuel 8,000. Joker 5,600, bingo 4,100.
Home plate CVN-73, 100 miles, steer 180." "Fuel to home" / "can we make it home" use the same
numbers. Off: `FUEL_CALLS = False` in config.py.

**Overlord as JTAC** (any aircraft; made for the A-10C and Hornet). Say "Overlord, ..." or "JTAC, ...":

| Say | Overlord |
|---|---|
| "request target" · "request tasking" · "request target north of me" · "request target, armour" | the most dangerous enemy ground unit near you (37 km in jets, 12 km in helicopters), in that direction / of that kind |
| "next target" | another one |
| "say again target" | repeats it |
| "mark target" · "mark target with white smoke" | smoke on it (Olympus; red unless you say a colour) |
| "BDA" | destroyed / still active / can't assess |

In a jet (or while a JTAC target is up) "next target", "BDA", "mark target", "smoke on my target" work
without "Overlord" (not in the Apache: there "next target" is George's).

"Overlord, target Shilka. Grid 40 Romeo, Charlie November, 9 9 1 6 8, 7 6 3 7 4. Elevation 1,040
feet. 6 miles north of you. Threats: SA-8, 1 mile north of the target; SA-6, 1.1 miles east of the target."
- A-10C: 10-digit MGRS for the CDU. Hornet (and other jets): lat/long in degrees and decimal minutes,
  "North 25 degrees 36.450, East 56 degrees 53.736", for the UFC. Helicopters: 8-digit grid.
- Elevation = the target's own altitude (Export.lua). Big radar SAMs are never given as targets, only
  as threats. The console prints both coordinate forms.
- When the target is destroyed Overlord calls it on his own: "Overlord, good effect, Shilka destroyed."
- The JTAC's target is "my target" for "Outcast 2, attack / lase my target" and "Overlord, requesting
  CAS on my target" until it's destroyed.

**Supercarrier by voice** (jets, with a ship in the mission). Say the call; it picks the matching item in
Supercarrier's radio menu and DCS's own Marshal / Approach / Paddles voices answer (ours stay silent):

| Say | Picks |
|---|---|
| "Marshal, 401, inbound" · "mother, inbound" | ATC > CVN-71 ... > Inbound (then the menu's main page becomes the carrier page) |
| "established" · "commencing" · "check in" · "platform" · "request azimuth" | that item on the carrier page |
| "abort inbound" | Abort Inbound |
| "ball" · "Clara" · "see you at ten" · "needles" | that item, when DCS offers it |

- Items are found by name on whatever page is up (they change at each step). If the call isn't there,
  nothing is selected and the crew voice reads what is: "No ball call now. Carrier menu has: abort
  inbound, established, commencing, request azimuth." If the carrier isn't in the ATC list: "No carrier
  in the radio menu. Check your radio. Carrier is on 127.5 AM."
- **Quiet window:** from Inbound until you're stopped on deck (or "abort inbound", a new mission, 45 min),
  everything nobody asked for (callouts, Overlord's calls and warnings, Outcast reports, joker / bingo)
  is held and played afterwards; anything held over a minute is dropped. Answers to your questions still play.
- Known so far (Case II/III): Inbound, Request Azimuth -> Abort Inbound, ESTABLISHED, COMMENCING,
  Request Azimuth -> Abort Inbound, CHECK IN, Platform, Request Azimuth. A Case I capture is still to do.
- Speech recognition in jets uses its own word list with these calls (config.JET_STT_PROMPT).

**Follow-ups:** right after "Outcast 2, ..." (30 s), a bare "hold position" / "rejoin" is taken as for
Outcast 2 too (through the model), and the same for Overlord.

**Carrier menu map** (to capture new carrier pages): in the Hornet near the carrier, with the
radio on its frequency, say **"map carrier menu"**. The ATC / carrier branches of the radio menu are
walked (submenus only, nothing is sent) and saved to `logs\carrier_menu_*.txt`. Hands off the menu
while it runs; it can take a minute.

## When no phrase catches it (the model works it out)

If nothing above matches what you said, or Overlord or an Outcast only managed "say again", the
crew's model (George, the Huey crew, or a small router prompt in other aircraft) may rewrite it
into one of the standard orders on this page, which then runs as if you'd said it:

| You say | Becomes |
|---|---|
| "Ocast 2, destroy your defence" | "Outcast 2, engage air defences" |
| "Forecast two, come back to me" | "Outcast 2, rejoin" |
| "Overlord, I need fast movers on those tanks up north" | "Overlord, requesting CAS north of me" |
| "Put a marker where we are, call it Bravo" | "Mark our current position as waypoint Bravo" |

- **It has to be addressed:** the rewrite must name someone you named (the Outcast's number,
  something like "Overlord", the waypoint name you said). Otherwise: "Say again."
- **Only places you said:** a rewrite naming a waypoint or direction you didn't say is refused, and
  a direction you gave to Overlord is kept ("...armour up north" -> "requesting CAS north of me").
- **Confirm first** for orders that are hard to undo (go independent, Overlord requests, attack,
  engage, weapons free, lase, RTB, cancel, delete): George asks "Confirm, Outcast 2, engage air
  defences?"; for Overlord's own requests Overlord asks, in his voice: "Overlord, confirm CAS north
  of you?" Say **"affirm"** within 12 s to run it, **"negative"** to drop it. A mis-heard one- or
  two-word answer ("Afrom") gets "Say again, affirm or negative?"; a longer sentence drops it and is
  taken as a new order. "Affirm" with nothing waiting: "Nothing to confirm." Orders an Outcast
  wouldn't understand are not read back at all. Rejoin, hold, status, picture, waypoints, grid:
  straight away.
- **It learns:** a rewrite that worked goes into `data\learned_phrases.json`: the whole sentence,
  and mis-heard words that became a keyword ("ocast" -> "outcast"). Next time it's caught at once,
  without the model (and without asking to confirm). Open the file to delete a wrong line, or
  delete the file to start again.
- **Time:** about 1-1.5 s, like other model answers; fixed phrases stay instant.
- **Whisper hints:** the callsigns and waypoint names in use ("Outcast 2, waypoint Alpha") are
  added to the speech recognizer's prompt while they exist, so they're heard right more often.
- Off: `ROUTER_ENABLED = False` in config.py.
- **Speech model:** `WHISPER_MODEL` in config.py: `"base.en"` (~0.35 s) or `"small.en"` (~1.1 s;
  on 24 Oct it heard "Outcast 2" right every time, base.en heard "All Class", "Ocast", "ORCAS").
  Not `"distil-small.en"`: it can't use the word prompt and wrote garbage, 2-9 s per call.

## Tactical picture (any aircraft; the crew voice, or Overlord's after "Overlord,")
Answers come only from the tactical picture (one track per unit in DCS's lists; DCS's lists are simulator
truth, not what anyone detected, so the answers say "picture", not "I see"). Fixed wording, no model.

| Say | Answer |
|---|---|
| "Tactical picture" · "ground picture" · "what's out there?" | counts (ground, air, last known only), the top threat, George's entries the picture can't place |
| "What threats are known?" · "threat summary" · "any threats?" | up to 3 threats, most important first: type, kind, distance and direction, "inside its approximate ring", "closing" only when measured |
| "Where are the unknown contacts?" · "what can't George place?" | George's list entries with no unit in the picture (a class only, or nothing fits) |
| "What has JTAC reported?" · "JTAC report" · "what's the JTAC target?" | the JTAC's target and what happened to it (still active, destroyed by ..., last seen ...) |
| "Which contacts are stale?" · "stale contacts" | units not in the latest scan, with how long ago and where they were |
| "What's changed?" · "anything new?" | the last 2 minutes: kills, units gone, new air defence / aircraft / serious threats |
| "Overlord, threats" · "Overlord, tactical picture" | the same, in Overlord's voice ("Overlord, picture" is still his air picture) |

Not these: "what do you see" and George's target list (his own sensor), "where's the priority threat"
(George), "status" / "sitrep". The kneeboard map's "Kneeboard, contact 42" shows one unit's details.

## Warlord and Darkside (any aircraft; Phase 2 command roles)
Warlord (mission commander) and Darkside (JTAC) answer only when their name comes first, each in his
own voice (en_US-norman-medium / en_US-bryce-medium; Overlord's voice until the files are there). They
call you by the mission's callsign ("Enfield 1-1"), or OPERATOR_CALLSIGN ("NOMAD") when DCS gives none.
COMMAND_ROLES_ENABLED = False in config.py turns all of this off (Overlord as before Phase 2).

| Say | Answer |
|---|---|
| "Warlord, radio check" · "Darkside, how do you read?" | "Enfield 1-1, Warlord, loud and clear." |
| "Warlord" · "Darkside" (nothing after) | "..., go ahead." |
| "Darkside, request target" · "next target" · "say again target" · "mark target" · "BDA" | the JTAC talk-on, mark and BDA, in Darkside's voice ("JTAC, ..." too) |
| "Overlord, request target" (and his other JTAC phrases) | "Overlord, contact Darkside." then Darkside answers |
| "Darkside, abort" | every CAS flight weapons hold, back to the IP |
| "Darkside, continue" | they're cleared again from the IP, if his checks pass |
| "Warlord, override" | within 60 s of a denial: the flight launches anyway; the risk is restated and logged in the debrief |
| "Warlord, say objectives" · "say ROE" · "say priorities" · "say no-strike zones" | from the "warlord" block of data/campaign_briefing.json (sample: data/examples/warlord_block.sample.json) |

Every flight Overlord would send is reviewed by Warlord first. He defers with no current tactical
picture or with WARLORD_MAX_FLIGHTS (4) flights up, and denies a target in a no-strike zone, CAS under a
big SAM with no SEAD up, or CAS / SEAD with your side's units within DARKSIDE_DANGER_CLOSE_M (1000 m)
of a target. An approval outside the priority areas comes with a note.

Overlord's CAS (Hog) arrives weapons hold, flies to an IP 9 km from the target (away from known air
defence) and checks in with Darkside (comms log, "AI to AI"). Darkside clears it hot himself when the
target is alive in the picture, no friendly is within 1000 m and it isn't in a no-strike zone; if
not, he says why it's holding. SEAD, CAP and escort are Overlord's, as before.

How it sounds (config.py, both optional): COMMAND_SHORT_FORM = False (default) is the full exchange:
"Enfield 1-1, Overlord, copy CAS on your target. Checking with Warlord." / "Overlord, Warlord, CAS
approved." / "Enfield 1-1, Overlord, Hog flight, two A-10s, tasked CAS ... Contact Darkside." / later
"Enfield 1-1, Darkside, Hog is cleared hot, T-72B, your target." True is one line per step: "Overlord,
Hog flight tasked CAS your target, 6 minutes." / "Darkside, Hog cleared hot." Denials, danger close,
aborts and the override's risk keep their full words in both. AI_TO_AI_SPOKEN = False (default): calls
between AI flights ("Darkside: Hog 1, cleared hot, T-72B.") are only in the comms log; True speaks them too.

## Training mode (any aircraft; the instructor's voice, Jenny)
One mode for training targets and the instructor's exercises (1 Nov 2026). Everything about training
is answered by the instructor, starting "Instructor, ...".
- "Training mode on" / "training mode off" (also "start / end the training", "Overlord, training mode on",
  "Instructor mode on / off"). On works even when training targets can't be spawned (no Game Master
  login, Olympus off, other players in the mission): the exercises work and she says why there are no
  targets. Off stops a running exercise (kept as aborted, not scored) and removes the training targets.
- "Overlord, spawn two T-72s 5 km north of me" or "Instructor, spawn ..." (only in training mode; options:
  live / weapons hold, moving / static, easy / average / hard). "Training group is up" follows.
- "Overlord / Instructor, clear training targets", "... training status" (the targets and the exercise).
- Exercises (helicopters): "Instructor, hover exercise / hover turn / approach to Alpha / fuel exercise",
  "begin", "stop", "how did I do", "easier / harder / level 3", "what should I practise". Selecting or
  beginning one needs training mode on; results and status always answer.
- Each training target destroyed goes to your score log (Training tab, Scores).

## VR kneeboard pages (any aircraft; see KNEEBOARD.md)
- "Show the map / briefing / checklist / comms / status / training", "kneeboard, briefing"
- "Show the shutdown checklist" (shows it, doesn't start it)
- "Page down / up", "next / previous page", "scroll to the top / bottom"; "scroll down / up" except in the Apache (George's list there: say "kneeboard, scroll down")
Nothing is said back: the page changes.
In the A-10C II and Hornet the Checklist page follows your switches by itself (no voice): "show the cold
start / taxi / takeoff / carrier launch / landing / carrier landing / shutdown checklist" (A-10C also
"pre-flight", "run up") picks a list.

## Navigation (the mission route)

DCS's export doesn't give the route for the AH-64D or the UH-1H, so `george_comms_hook.lua` reads
your aircraft's waypoints from the loaded mission (the console prints `[NAV] Mission route: ...`).
Waypoint numbers are the mission editor's (0 = where you start). "Next waypoint" is then George's
own count: the route in order, moving on as you pass within 800 m of each one. If you've set the
TSD to another waypoint, ask for it by number.

| Say | George |
|---|---|
| **next waypoint** · **where's the next waypoint** | "Waypoint 3, 2 o'clock, steer 045, 4.1 miles, 3 minutes." |
| **waypoint 2** · **how far to waypoint 4** | The same for that waypoint |
| **where's home** · **bearing home** · **how far home** | Home = the first point of your route, or where you started the flight if the mission gives you no route |
| **fuel to home** · **fuel to waypoint 4** · **bingo** · **can we make it home** | Huey: pounds needed, what you'd land with, below reserve or not (not the Apache: DCS gives its fuel only as a percentage) |

Steering is true heading; times use your ground speed (none below 15 knots).

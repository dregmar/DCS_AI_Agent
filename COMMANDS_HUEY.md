# What the Huey crew understands (UH-1H)

Fly the UH-1H and the program switches to the Huey crew by itself (the console prints
`[PROFILE] UH-1H: Huey crew ... active`). Same push-to-talk: **SPACE** or your stick button.
The phrases below are handled instantly; anything else goes to the local model (about 1–1.5 s), whose
answer is checked before the crew acts on it. A short "cease fire" / "hold fire" / "check fire" /
"weapons hold" runs at once, ahead of anything still waiting, and cancels the orders given before it.

Needs `george_huey_export.lua` (gunners, autopilot, gauges, cautions) and DCS-BIOS (switches,
radios, flares). See README.

## Door gunners and co-pilot

Name *left*, *right*, *co-pilot*, *gunners* (both doors) or *all*. Nobody named means
both door gunners, except **cease fire**, which stops everyone. George presses until the Crew
Status panel shows what you asked for, then answers; he opens that panel while he works and
closes it again after.

| Say | Crew |
|---|---|
| **weapons free** · **free fire** · **fire at will** · **open fire** · **cleared hot** | FREE FIRE |
| **return fire** · **weapons tight** | RET. FIRE (only shoot back) |
| **hold fire** · **cease fire** · **weapons hold** · **check fire** | HOLD |
| **left gunner long bursts** · **gunners short bursts** | Burst length |
| **crew status** · **ammo check** | Each gunner's ROE, ammo %, burst |
| **show crew panel** · **hide crew panel** | The Crew Status panel |

Examples: "right gunner, weapons free", "left gunner return fire", "co-pilot hold fire".

## Autopilot (the game's assist)

| Say | Does |
|---|---|
| **autopilot attitude hold** · **hold this attitude** | ATTITUDE HOLD |
| **autopilot level flight** | LEVEL FLIGHT |
| **autopilot orbit** | ORBIT |
| **autopilot on** · **autopilot off** | Presses the autopilot on/off command |

The game doesn't show the autopilot state anywhere George can read, so these are sent, not
confirmed.

## Cockpit jobs (DCS-BIOS, confirmed from the switch)

| Say | Does |
|---|---|
| **master arm on** · **arm up** · **master arm safe** · **master arm off** | Armament OFF / SAFE / ARMED |
| **select rockets** · **select guns** · **grenades** | 2.75 / 7.62 / 40 mm selector |
| **gun selector left / right / both** · **left guns only** · **both guns** | Gun selector |
| **rocket pairs three** | Rocket pair selector (0–7) |
| **flares** · **pop flares** | One flare (arms the dispenser first if it's safe) |
| **flares armed** · **flares safe** | Dispenser ARM switch |
| **landing light on/off** · **search light on/off/stow** | Also "turn off the landing light". Just "lights off": George asks which |
| **anti-collision on/off** · **nav lights steady/flash/off** · **position lights dim/bright** | |
| **pitot heat on/off** · **battery on/off** · **main fuel on/off** · **force trim on/off** · **hydraulics on/off** · **inverter main/off** | |
| **main generator on** · **starter generator standby** · **starter generator start** · **low RPM audio on/off** | |
| **UHF 251.0** · **VHF 124 decimal 75** · **FM 30.5** | Turns that radio's dials click by click and reads them back (UHF 225–399.95 in 50 kHz steps, VHF 116–149.975, FM 30–75.95). UHF only follows its dials with the mode dial on manual; George tells you if it isn't |
| **nav 112.5** · **navigation 108 decimal 05** | Tunes the VHF NAV receiver the same way (108–117.95) |
| **radio 124.75** · **set radio to frequency 241** | No radio named: the frequency picks it (225 and up UHF, 116–149.975 VHF, 108–115.95 NAV, 30–75.95 FM) |
| **radio check** | Reads the radio frequencies |

While you're flying George refuses: main fuel off, hydraulics off, battery off, governor
emergency, starter-generator to start, main generator off.

## Checklists (DCS UH-1H manual, section 9)

**before start checklist** · **start checklist** · **run up checklist** ·
**before takeoff checklist** · **shutdown checklist** ("let's start engines" or "walk me through
the start" also starts the engine start checklist)

George reads each item. Switches he can see on DCS-BIOS and gauges he can read ("RPM 6600",
"AC 112 to 118 volts", "no cautions") he checks himself and moves on. For the rest he waits:

| Say | |
|---|---|
| **check** · **next** · **done** | Item done |
| **set it** | George sets that switch for you |
| **skip** | Move on |
| **repeat** | Read it again |
| **stop checklist** | Abandon it |

## Sling loads

| Say | Crew chief |
|---|---|
| **where's the load** · **where did the cargo go** · **load status** | "Load, 1 o'clock, 40 meters." (the nearest load) |
| **talk me onto the load** · **pick up the cargo** · **talk on** | Talks you on until it's hooked: "Forward ten, right five, down five." ... "Steady, hold." |
| **stop talk on** · **cancel talk on** | Stops the talk-on |
| **hook the load** · **attach cargo** · **connect the load** | After landing within 5 m of it: DCS's hook command (RCtrl+RShift+L) |
| **release the load** · **drop the load** | Cargo unhook |
| **load is under me** · **calibrate the load** | Hover with the load right under the hook and say this once: George learns where the hook sits and corrects every distance after (saved in `huey_calibration.json`) |
| **reset load calibration** | Forget that correction |

He only talks you on when you ask: a mission can have several loads around you. DCS only hooks a
load you've selected first in the radio menu (F6, cargo list): "radio menu", then the cargo item.

## Questions

| Say | George |
|---|---|
| **status** | Short: height, speed, torque, EGT, fuel, cautions, master arm, gunners in one line |
| **engine check** · **gauges** | Rotor, engine RPM, N1, torque, EGT |
| **fuel state** · **how much fuel** | Pounds and roughly how long |
| **caution lights** | Which caution lights are on |
| **any contacts** | Nearest enemy units, clock position and range |

## What the crew chief calls out on his own

- Engine start (at 25 % N1): "Rotor's turning." (or "No rotor turning. Abort the start!"),
  "Forty percent. Release the starter."
- Limits: rotor RPM low (under 300 in flight), rotor overspeed (over 339), engine RPM low
  (under 6100 in flight), EGT over 625 (engine running) and over 760, torque over 50 (at most once a minute), N1 over 101.5.
- "Sink rate! You're settling." when slow (under 30 kt) and sinking faster than 800 ft/min.
- New caution lights once the engine runs: "Caution, hydraulic pressure", "Twenty minutes of
  fuel", "Fire light! Engine fire!" (a lamp test is ignored).
- Gunners, while their panel is up: "Left gunner is hit!", "Right gunner, ammo low",
  "Winchester".
- Enemy units closer than 1.5 km: "Contact, ZU-23, 3 o'clock, 900 meters." At most 3 a minute, nearest
  first; in a busy area one "Multiple contacts, 6 more, 9 to 11 o'clock." and then quiet for a minute.
  Air defence (ZU-23, Shilka, MANPADS, SAMs) is always called. Trucks and buildings aren't called.
- Landing and hover (heights are skid heights): "Twenty feet." "Ten." "Five." "Three." "Skids down.",
  "Drifting back and left." ... "Steady.", "Easy, slow your descent!", and "Skids up." on lift-off.
  The first time you sit still on the ground he learns what "on the ground" reads.
  While an Instructor exercise is armed or running (training mode), the height countdown and drift
  calls stop so the Instructor can judge you; "Easy, slow your descent!", "Skids down/up" and the
  sling-load talk-on still play.
- Sling loads (after "talk me onto the load"): "Load, 12 o'clock, 40 meters." then "Forward ten,
  left five, down five." ... "Steady, hold.", "You're in range. Say hook the load." when you've
  landed next to it, and "Load's off the ground." when it lifts.
- "Bingo fuel. Home is 12 miles, steer 225." when flying home would leave you under the reserve
  (`HUEY_FUEL_RESERVE_LBS` in config.py; burn `HUEY_FUEL_BURN_LBS_HR`, 555 lb/h). Once per flight.

Radio menu, wingmen, Overlord, Darkside, Warlord, briefing, position, training mode (the Instructor's
hover, hover turn, approach and fuel exercises) and the kneeboard: see `COMMANDS_COMMON.md` and
`KNEEBOARD.md`.

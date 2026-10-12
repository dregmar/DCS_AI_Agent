# What the Huey crew understands (UH-1H)

Fly the UH-1H and the program switches to the Huey crew by itself (the console prints
`[PROFILE] UH-1H: Huey crew ... active`). Same push-to-talk: **SPACE** or your stick button.
The phrases below are handled instantly; anything else goes to the LLM (2–5 s).

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
| **autopilot attitude hold** | ATTITUDE HOLD |
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
| **rocket pairs three** | Rocket pair selector (0–7) |
| **flares** · **pop flares** | One flare (arms the dispenser first if it's safe) |
| **flares armed** · **flares safe** | Dispenser ARM switch |
| **landing light on/off** · **search light on/off/stow** | |
| **anti-collision on/off** · **nav lights steady/flash/off** · **position lights dim/bright** | |
| **pitot heat on/off** · **battery on** · **main fuel on** · **force trim on** · **hydraulics on** · **inverter main** | |
| **UHF 251.0** · **VHF 124 decimal 75** · **FM 30.5** | Turns that radio's dials click by click and reads them back (UHF 225–399.95 in 50 kHz steps, VHF 116–149.975, FM 30–75.95). UHF only follows its dials with the mode dial on manual; George tells you if it isn't |
| **radio check** | Reads the three frequencies |

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
- Enemy units closer than 1.5 km: "Contact, ZU-23, 3 o'clock, 900 meters."

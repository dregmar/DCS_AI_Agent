# What George understands

Hold **SPACE** or your **PTT button** (button 21 on the VKB Gladiator EVO R stick; `JOY_BUTTON` in config.py), speak, release. The phrases below are handled instantly. Anything else goes to the
local model (about 1–1.5 s), which handles free-form requests and settings you name in your own words;
its answer is checked against what you said and the live state before George acts on it.
"George", "please" and "okay" are ignored, and so is a leading "and" or "so".

**Safety orders go first:** a short "cease fire", "hold fire", "check fire", "weapons hold / tight",
"stop lasing", "laser off" or "abort" (George, crew, a wingman or Outcast may be named first) runs at
once, ahead of anything still waiting, and cancels the orders you gave before it. The Comms page of the
kneeboard lists what was dropped.

## Fire control

| Say | George |
|---|---|
| **fire** · **send it** · **cleared hot** · **cleared to fire** · **shoot** | Consent to fire (Multifunctional Input). Only the whole sentence counts, and only within 3 s of saying it. |
| **cease fire** · **hold fire** · **check fire** · **stop tracking** · **stand down** | Stops everything, TADS fixed forward. |
| **weapons free** · **weapons hot** · **weapons hold** | Sets his ROE (FREE FIRE: he shoots when ready). |
| **toggle ROE** | Flips ROE and tells you which it is now. |

## George's weapon and settings

He switches weapon first when a setting needs it, and only answers once his wheel shows the result.

| Say | Sets |
|---|---|
| **missiles** · **gun** · **rockets** · **select missiles** · **give me the gun** · **no weapon** | George's weapon |
| **cycle weapon** | His next weapon |
| **burst twenty** (10, 20, 50, 100, all) · **change burst** | Gun burst |
| **rocket quantity four** (1, 2, 4, 8, 12, 24, all) · **cycle quantity** | Rockets per salvo |
| **rocket type smoke** (HE, illum, smoke) · **cycle rocket type** | Rocket type |
| **switch to RF** · **switch to SAL** · **cycle missile type** | Missile type |
| **LOBL** · **LOAL** | Lock-on before / after launch |
| **trajectory high** (high, low, direct) · **cycle trajectory** | Hellfire trajectory (switches to LOAL first) |

## Searching

| Say | George |
|---|---|
| **search** | Searches where you're looking (helmet sight), then reads out his list |
| **search again** | Searches along the TADS line of sight again |
| **search forward area** · **forward area** | Area search: forward area |
| **helmet area** · **FCR targets** (or **radar targets**) · **own PFZ** · **active PFZ** | The other area searches |
| **point search tango zero seven** · **point search T07** | Slaves the TADS to that TSD point and searches around it (T, C, W or H points) |
| **search list** · **point list** · **start area search** | Opens the lists by hand |

## Targets

After a search George reads his list out with clock positions, for example: "Target list: 1 ZSU-23-4 at 12 o'clock, …".

| Say | George |
|---|---|
| **lock target 3** · **select the second one** · **track target 3** · **lock priority target** | Tracks that entry of his list |
| **engage targets 1, 3 and 4** · **engage targets one through four** · **engage the priority target** | Marks them on his shoot list and engages them in that order |
| **where is target 2** · **where's the priority threat** | "Target 2, LARC-V, 2 o'clock, 2.1 kilometers." |
| **next target** · **scroll down** · **previous** · **scroll up** | Moves through the open list (in the Apache these stay George's; the kneeboard needs "kneeboard, scroll down") |
| **track it** · **that one** | Tracks the highlighted entry |
| **skip target** | Next target in a multi-target engagement |
| **exit list** · **cancel target list** | Closes the open list |

## Sensor

| Say | George |
|---|---|
| **laser on** · **laser off** · **lase** | Laser on / off (no weapon assigned) |
| **zoom in** · **zoom** · **zoom out** | TADS zoom where he can, refuses where he can't |
| **FLIR** · **TV** · **switch sensor** | FLIR / DTV |
| **store target** · **mark it** | Stores the target as a TSD point |
| **toggle threat warnings** | George's verbal threat warnings on / off |

## Your own switches (DCS-BIOS)

| Say | Does |
|---|---|
| **master arm on** · **arm** · **go hot** | Your master arm on (confirmed from the ARM light) |
| **master arm off** · **master arm safe** · **safe** · **disarm** | Your master arm safe |
| **action missiles** · **action my gun** · **my rockets** | Actions YOUR weapon (cyclic hat). Confirmed from your WPN page. |
| **de-action** · **WAS off** | Un-actions your weapon. Needs your WPN page up. |

"Action …" and "my …" always mean your weapon. On their own, "missiles" and "gun" mean George's.

## Other

| Say | Does |
|---|---|
| **status** · **sitrep** | Flight data, fuel, master arm, your weapon, George's settings, his list, the priority target |
| **fuel** · **fuel state** · **how much fuel** | Fuel only, in pounds, straight from DCS (your aircraft's fuel x its own tank size, so any loadout and external tanks are right): "Fuel 5530 pounds, with the external tanks." Needs george_comms_hook.lua; without it the Export.lua value is used |
| **show the wheel** · **hide the wheel** · **toggle wheel** | George's wheel (it also hides itself after 6 idle seconds) |

## Checklists

**before start checklist** · **start checklist** · **before takeoff checklist** · **shutdown checklist**
(also "let's start engines"). George reads each item and checks what he can see on DCS-BIOS himself:
canopy, parking brake, master ignition, rotor brake, power levers, APU light, CMWS, lights, and the
EUFD for warnings. For the rest he waits:

| Say | George |
|---|---|
| **check** · **next** · **done** | Item done |
| **set it** | Sets that switch for you (master ignition, rotor brake, parking brake, canopy, lights, CMWS) |
| **skip** · **repeat** · **stop checklist** | |

## Warnings and cautions (EUFD)

| Say | George |
|---|---|
| **any cautions** · **warnings** · **cautions** | Reads the EUFD's warnings, cautions and advisories |

## What George calls out on his own

- "Missile away, 7 remaining." · "Missiles winchester." · "Firing gun." · "Gun winchester."
- "Target destroyed. Next target, T-72B." · "Shoot list complete."
- Low altitude, high-threat air defence detected, TADS masked.
- "DCS not in focus." when DCS isn't the window in front (he sends no keys then).
- New EUFD warnings and cautions: "Warning, engine 1 out." "Caution, engine 1 oil pressure low."
- Warnings, cautions, threat warnings and low altitude also go to the kneeboard's Alerts page.

Radio menu, wingmen, Overlord, Darkside, Warlord, briefing, position, training mode and the kneeboard:
see `COMMANDS_COMMON.md` and `KNEEBOARD.md`.

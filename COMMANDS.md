# What George understands

Hold **SPACE**, speak, release. The phrases below are handled instantly. Anything else goes to the
LLM (2–5 s), which handles free-form requests and settings you name in your own words.
"George", "please" and "okay" are ignored, and so is a leading "and" or "so".

## Fire control

| Say | George |
|---|---|
| **fire** · **send it** · **cleared hot** · **cleared to fire** · **shoot** | Consent to fire (Multifunctional Input). Only the whole sentence counts, and only within 3 s of saying it. |
| **cease fire** · **hold fire** · **check fire** · **stop tracking** · **stand down** | Stops everything, TADS fixed forward. |
| **weapons free** · **weapons hold** | Sets his ROE (FREE FIRE: he shoots when ready). |
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
| **next target** · **scroll down** · **previous** · **scroll up** | Moves through the open list |
| **track it** · **that one** | Tracks the highlighted entry |
| **skip target** | Next target in a multi-target engagement |
| **exit list** · **cancel target list** | Closes the open list |

## Sensor

| Say | George |
|---|---|
| **laser on** · **laser off** · **lase** | Laser on / off (no weapon assigned) |
| **zoom in** · **zoom out** | TADS zoom where he can, refuses where he can't |
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
| **status** · **sitrep** | Flight data, master arm, your weapon, George's settings, his list, the priority target |
| **show the wheel** · **hide the wheel** · **toggle wheel** | George's wheel (it also hides itself after 6 idle seconds) |

## What George calls out on his own

- "Missile away, 7 remaining." · "Missiles winchester." · "Firing gun."
- "Target destroyed. Next target, T-72B." · "Shoot list complete."
- Low altitude, high-threat air defence detected, TADS masked.
- "DCS not in focus." when DCS isn't the window in front (he sends no keys then).

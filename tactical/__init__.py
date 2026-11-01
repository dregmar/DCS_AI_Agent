"""
tactical/ - the tactical picture (design: "DCS George AI — Design: Tactical Picture & Threat Fusion",
approved 31 Oct 2026).

One track per DCS object, built from what the program already receives, with every fact labelled with
where it came from. Built in steps; this package holds, so far:

    models.py    typed, immutable records (T1)
    clock.py     DCS model time <-> time.monotonic(), mission generations (T1)
    sources.py   state_store.snapshot() -> Reports + source health / coverage (T1)
    tracks.py    the track table: one track per DCS object, lifecycle, "is it gone?" (T2)
    events.py    meaningful changes as short lines, bounded log (T2)
    service.py   the daemon thread that keeps the picture current and publishes it (T3)
    fusion.py    George's Target List -> tracks, as uncertain associations, never merges (T4)
    threats.py   which units are threats, in what order, and the evidence for each (T4)

main.py starts the service and shows its Status row (T3); no feature reads the picture yet. Nothing here
talks to DCS, Olympus or the network.
"""

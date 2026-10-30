"""
jet_probe.py - checks the jet checklists' switch readings against the cockpit (run instead of main.py,
with DCS running in the A-10C or the Hornet and DCS-BIOS installed).

    python jet_probe.py              prints every checklist control when it changes

Flip each switch the checklists use and check the printed position matches the cockpit. Positions
marked "(from the doc)" came from DCS-BIOS's descriptions, not its position list: those are the ones
to check. Tell Claude which read wrong (e.g. "Hornet MASTER_ARM_SW reads SAFE when armed").
Ctrl+C to stop.
"""
import time

from common.jet_checklists import JetChecklists, CHECKLISTS, NAMES, controls_used, MODULE_FOR
from core.state import state_store, watch_raw
import config


def main():
    jc = JetChecklists(state_store.bios_words_snapshot, lambda: True,
                       json_dir=getattr(config, "DCS_BIOS_JSON_DIR", None))
    watch_raw(jc.addresses())
    from core.dcs_bios import start_dcs_bios_thread
    start_dcs_bios_thread()
    print("Listening to DCS-BIOS. Flip the switches; Ctrl+C to stop.")
    last = {}
    while True:
        time.sleep(0.5)
        words = state_store.bios_words_snapshot()
        ac = state_store.snapshot().get("aircraft") or ""
        keys = [k for k in CHECKLISTS if jc.key_for(ac) == k] or list(CHECKLISTS)
        for key in keys:
            mod = jc.module(key)
            if mod is None:
                continue
            for ident in controls_used(key):
                if ident not in mod.controls:
                    continue
                st = mod.state(words, ident)
                fr = mod.fraction(words, ident)
                val = st if st is not None else (None if fr is None else f"{fr:.2f}")
                if val is None or last.get((key, ident)) == val:
                    continue
                last[(key, ident)] = val
                doc = (" (from the doc)" if ident in NAMES.get(MODULE_FOR.get(key, key), {}) else
                       " (DCS-BIOS list turned round)" if ident in mod.reversed else "")
                print(f"{time.strftime('%H:%M:%S')}  {key:14s} {ident:24s} {val}{doc}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

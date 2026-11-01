"""
command/exchange.py - how the command roles talk (Phase 2, P2-7). Fixed wording only (D6).

    COMMAND_SHORT_FORM = False   full form (default): each call names you and the role, and a tasking is
                                 the whole exchange - Overlord's "Checking with Warlord", Warlord's
                                 "approved", Overlord's tasking, "Contact Darkside".
                     = True      short form: one line per step, no callsigns, no relay calls.
    AI_TO_AI_SPOKEN = False      calls between AI ("Hog 1, cleared hot") go to the comms log only;
                    = True       they are also spoken (routine priority, dropped when stale).

Safety-relevant words are the same in both forms: denials and their reasons, danger close, "abort",
the risk restated on an override, cautions about air defence.

main.py calls configure() once at start-up from config.py. Readers call short() / ai_spoken() /
lead(); the settings sit behind a lock (no unsynchronised globals).
"""
import threading

_lock = threading.Lock()
_short = False
_ai_spoken = False


def configure(short: bool = False, ai_spoken: bool = False) -> None:
    global _short, _ai_spoken
    with _lock:
        _short, _ai_spoken = bool(short), bool(ai_spoken)


def short() -> bool:
    with _lock:
        return _short


def ai_spoken() -> bool:
    with _lock:
        return _ai_spoken


def lead(callsign: str, role: str) -> str:
    """How a role starts a call to you: "Enfield 1-1, Darkside, " (full) or "Darkside, " (short)."""
    return f"{role}, " if short() else f"{callsign}, {role}, "

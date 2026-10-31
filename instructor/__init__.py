"""
instructor/ - Adaptive Instructor Mode (feature 7): scores exercises from real DCS telemetry.

Read-only by design: nothing in this package sends anything to DCS, DCS-BIOS, Olympus, the comms
hook or the keyboard. It reads state_store.snapshot() (through the observer), keeps its own pilot
history under data/instructor/, and speaks only through the callback it is given.

Scoring is deterministic and explainable: every criterion is either judged from good data or
"not judged" with the reason, every deduction carries its evidence, and no language model is
involved anywhere. See claude/instructor_mode.md in the project for the design and decisions.

Phase 1 (30 Oct 2026): core + the stationary hover exercise.
"""

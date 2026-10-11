# Tests

Run from the project folder (DCS does not need to be running):

    python -m unittest discover tests -b

(-b hides the driver's console output for tests that pass; add -v to list each test.)

Nothing here presses real keys, sends to DCS-BIOS or calls the LLM:
`tests/fakes.py` provides a simulated George (FakeGeorge) and keyboard, and the pipeline test
shuts down main.py's real driver before using its own. `tests/fixtures/` holds text captured
from DCS (George's wheel, his Target List, a WPN page) so the parsers are tested on real data.

Takes about a minute (the driver waits for the simulated wheel to redraw, like the real one).

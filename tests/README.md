# Tests

Run from the project folder (DCS does not need to be running). Both must pass:

    python -m unittest discover tests -b
    python -m unittest discover -s tests -t .

The two load the test modules under different names (`tests/fakes.pipeline_module()` handles that).
`-b` hides the console output of tests that pass; add `-v` to list each test. After a `.py` edit, a
Claude Code Stop hook runs the first command by itself (skipped while DCS.exe is running).

Nothing here presses real keys, sends to DCS-BIOS, DCS, Olympus or Ollama, or needs a microphone or a
sound card:
- `fakes.py`: a simulated George (FakeGeorge) and keyboard, fast test timings in config,
  `isolate_unit_types()` (tests never read or write your `data/unit_types.json`), and no reads or
  writes of your training list, instructor history or a real Game Master login. Olympus, Ollama and
  the audio output are replaced inside the tests that use them.
- `huey_fakes.py`: a simulated Huey crew and cockpit (FakeHuey).
- `tactical_fixtures.py`: hand-made tracks and George entries for the tactical picture tests.
- `instructor_sim.py`: a small flight simulator (4 Hz) for the Instructor's exercises.
- `fixtures/`: data captured from DCS and from real flights (George's wheel, his Target List, a WPN
  page, hovers and an approach) so parsers and scores are tested on real data.

Every code change comes with tests. A few minutes for the whole suite (the George driver waits for the
simulated wheel to redraw, like the real one). Known flaky under CPU load:
`test_driver.Settings.test_burst_value`. Don't run long tests while flying.

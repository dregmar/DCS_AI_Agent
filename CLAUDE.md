# DCS George AI

100% local AI crew middleware and VR kneeboard for DCS World, written by Andre:
George (CPG) in the AH-64D, the crew in the UH-1H, Overlord (C2) in any aircraft, the Instructor,
and a kneeboard web page shown in OpenKneeboard. See README.md for setup and COMMANDS*.md for
the voice commands.

The pipeline for everything: push-to-talk -> Whisper (small.en, CPU) -> fast path (regex) ->
router (qwen3:8b on Ollama, only when nothing matched) -> handler -> gates -> Piper voice.

## Hard rules (never break these)

**Secrets and Andre's files**
- `config.py` is Andre's. Never read, edit, print or overwrite it; it holds the Olympus passwords.
  To see settings, run `python .claude/tools/show_config.py [NAME ...]` (passwords redacted).
- A new setting: read it with `getattr(config, "NAME", default)` so the program runs without it,
  and tell Andre the exact line to add to config.py.
- Never print, log, test with or write passwords, hashes or auth headers anywhere.
- `data/unit_types.json` is learned on Andre's PC: never edit it, and tests must not read or write it
  (`tests/fakes.isolate_unit_types()` does this; import `tests/fakes` in tests that touch unit types).
- `data/learned_phrases.json`: edit only to remove a wrong learned phrase, and tell Andre to restart
  the program (the running copy keeps the old list in memory and can save it back).
- DCS manuals (`data/manuals/`) stay on this PC; never copy their text into docs or anywhere else.

**Audio (audio/tts.py, audio/dsp.py)**
- Piper: use `chunk.audio_int16_bytes` (or `_audio_int16_bytes`) and `chunk.audio_float_array`.
  Never `synthesize_stream_raw` or `chunk.audio_bytes`.
- Keep the dithered tail (`np.random.normal(0, 0.0001)`) and `time.sleep(total_duration + 0.1)`
  before `sd.stop()`: they stop WASAPI truncating speech on playback Device #5.
- Don't refactor the playback path unless the task is about it.

**DCS performance comes first**
- Never block the main thread, keyboard hook, PTT loop or telemetry loops with STT, LLM, TTS,
  HTTP, disk or other slow work: daemon threads or queues.
- Whisper stays at `cpu_threads=4`; the process runs below normal priority.
- Don't change the GPU set-up (Ollama on the RX 5700 XT via Vulkan, DCS on the 7900 XTX), the
  Ollama context, or `HSA_OVERRIDE_GFX_VERSION` (never set it). Don't change ports or bindings.

**Threads and shared state**
- Shared state goes through the locked `ApacheStateStore` (core/state.py) or locked registries.
  No unsynchronised globals across threads. Never hold a lock across HTTP, TTS or long work.

**Behaviour**
- Fast path before the LLM. Never replace a deterministic handler with an LLM decision.
- Every LLM answer passes the gates (what was said + live state) before it acts or is spoken.
- Overlord's voice only for Overlord / Olympus replies, and he only acts on utterances that start
  with "Overlord". Wingmen, Outcasts and radio-menu replies use the crew voice.
- Olympus first for wingman orders, radio menu as the fallback.
- Training targets only while training mode is on, through the Game Master client and the gate in
  common/training.py. Keep the Blue commander and Game Master logins separate.
- Instructor (instructor/) is read-only: no spawning, mission changes or commands; deterministic
  scoring, no LLM in scores or feedback.
- Kneeboard: 127.0.0.1 only; feeds sent only when they change; no animations (it renders next to DCS).

## Tests
- `python -m unittest discover tests -b` must pass before any change is called done. A Stop hook
  runs it automatically after .py edits (skipped while DCS.exe is running).
- For bigger changes also run `python -m unittest discover -s tests -t .`. The two load the test
  modules under different names; both must pass (tests use `tests/fakes.pipeline_module()` for this).
- Every code change comes with tests. Use the fakes in tests/ for DCS, Olympus, Ollama and audio;
  tests never need DCS, Olympus, Ollama, a microphone or a sound card.
- Known flaky under CPU load: `test_driver.Settings.test_burst_value` (fake wheel timing).

## Don't run
- `main.py` (it presses keys with pynput and talks to DCS); Andre runs it.
- Probes (`*_probe.py`), `llm_bench.py` or anything that sends to DCS, Olympus or Ollama without asking.
- Long test runs while Andre is flying.

## DCS scripts
- The Lua sources are in `dcs_scripts/`. The live copies are in
  `%USERPROFILE%\Saved Games\DCS\Scripts\` (Export.lua and the crew exports) and
  `...\Scripts\Hooks\` (george_comms_hook.lua).
- After editing a Lua file, copy it to Saved Games straight away and say so. `snapshot.bat` copies
  Saved Games -> dcs_scripts, so an uncopied edit is overwritten by Andre's next snapshot.
- No Lua interpreter here: check format strings against their arguments
  (tests/test_enemy_freshness.py does this for Export.lua) and say the first flight confirms it.

## Git
- Andre saves a snapshot (`snapshot.bat` = `git add -A` + commit) after each good test. Don't commit
  unless he asks; never push, reset --hard, clean or rewrite history.

## Working with Andre
- Big features: write the design (options, recommended choices, open questions) and wait for his
  approval before coding. Small fixes: just do them.
- Flight logs are `logs/george_<date>_<time>.log`. After a flight, read the newest one: check the
  `[LATENCY]` lines, errors, "say again" answers and anything the router learned; fix what it shows.
- Name the exact files changed. Keep reports short: what changed, the test count, what to fly next.
- Andre's PC: Windows, VKB Gladiator EVO R (PTT = button 21, joystick index 5), RX 7900 XTX + RX 5700 XT.

## Map of the code
- `main.py` start-up, PTT loop, voice / command workers, shared handlers, profile switching
  (apache / huey / other). `config.py` settings.
- `core/` state store, telemetry (UDP 5005), DCS-BIOS (5010 / 7778), fuel, lanes (urgent safety
  orders), metrics (latency per utterance).
- `intelligence/` ollama_chat, llm_client (George prompt + gates), fast_path, router (ROUTE
  rewrites, learned phrases), threat_engine, engagement_monitor.
- `apache/` George; `huey/` UH-1H crew; `instructor/` adaptive instructor (hover, approach, fuel,
  hover turn); `tactical/` tactical picture and threat fusion (tracks, fusion, threats, service).
- `common/` comms_menu (radio menu + hook messages), wingmen, olympus (REST client), independents
  (Outcasts), overlord, jtac, carrier, training (gate) and training_targets, briefing (BM25),
  position, airfields, debrief, orders, aircraft, fuel_watch, nav, user_waypoints ...
- `kneeboard/` server (HTTP + SSE), model (map, threat rings), pages, alerts, track, comms_log,
  static/index.html.
- `audio/` tts and dsp. `dcs_scripts/` Lua. `data/` JSON knowledge and learned data. `tests/`.
- Ports (localhost): 5005 Export, 5006/5007 George UI / weapons, 5008/5009 Huey, 5010/7778
  DCS-BIOS, 5011/5012 comms hook, 4512 Olympus, 11434 Ollama, 5000 kneeboard.

Design notes and history live in the claude.ai project "DCS_AI_George"; copies, if present, are
in `docs/`.

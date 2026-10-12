# DCS George AI

Local voice crew middleware for DCS. In the **AH-64D** you fly in the pilot seat and talk to George
(the AI CPG); in the **UH-1H** you talk to the Huey crew (door gunners, crew chief, co-pilot).
The program sees which aircraft you're in and switches by itself. Push-to-talk either way. Speech is transcribed by Faster-Whisper, simple commands go
through a deterministic fast path, everything else through a local Qwen 2.5 7B (Ollama), and
George is driven through his own AI wheel (simulated keys) and DCS-BIOS. George's wheel and
Target List are read back from DCS, so he confirms what he actually did.

## Run

1. Start Ollama (`ollama serve`, model `qwen2.5:7b`).
2. Start DCS and a mission in the Apache pilot seat.
3. `python main.py`, then hold SPACE or your throttle PTT button to talk. See **COMMANDS.md** for
   what George understands, and **COMMANDS_HUEY.md** for the Huey crew.

### UH-1H Huey setup (once)

1. Copy `dcs_scripts\george_huey_export.lua` and `dcs_scripts\Export.lua` into
   `%USERPROFILE%\Saved Games\DCS\Scripts\` (Export.lua now also sends the aircraft name,
   height above ground and vertical speed, and loads the Huey script).
2. Start a Huey mission. `Saved Games\DCS\Logs\george_huey_export_status.txt` should say
   "UH-1H detected, link active", and the console `[HUEY] UH-1H crew link is live.`
3. First flight: run `python huey_probe.py` instead of main.py, flip the switches it lists and
   show the Crew Status panel (LWin+H). It prints what George reads; some switch positions are
   inferred (marked "assumed") and need one check.

The Huey script reads gauges, caution lamps and the gunners' Crew Status panel, and accepts only
the game's own crew commands (gunner ROE / burst, crew panel, autopilot). Switches and radios go
through DCS-BIOS, as in the Apache.

### Push-to-talk on a joystick button

In `config.py`: `PTT_MODE` (`"keyboard"`, `"joystick"` or `"both"`), `JOY_NAME_CONTAINS`,
`JOY_DEVICE_INDEX`, `JOY_BUTTON`. Run `python joy_probe.py` to list your devices and see which
button number you press. For best results `pip install pygame-ce`; without it, Windows' built-in
joystick API is used (first 32 buttons only).

Console output is also saved to `logs\george_<date>_<time>.log` (newest 20 kept).

## Files

| Path | Role |
|---|---|
| `main.py` | Start-up, push-to-talk, the voice command pipeline, status reports; hands commands to the Huey profile while you fly the UH-1H |
| `config.py` | Ports, key bindings, timings, DCS-BIOS addresses |
| `core/george_interface.py` | Drives George's wheel; reads it back to confirm; Target List model |
| `core/george_ui.py` | Parses George's interface text from DCS (UDP 5007) |
| `core/wpn_page_listener.py` | Your actioned weapon / sight from the WPN page (UDP 5006) |
| `core/dcs_bios.py` | DCS-BIOS in (5010) and out (7778): master arm, weapon action switch |
| `core/state.py` | Thread-safe ApacheStateStore |
| `core/telemetry_listener.py` | Export.lua flight data, stores, nearby units (UDP 5005) |
| `core/focus.py` | Refuses to send keys unless DCS is the window in front |
| `core/console_log.py` | Mirrors the console to `logs/` |
| `core/joystick_ptt.py` | Push-to-talk on a joystick / throttle button |
| `joy_probe.py` | Lists joysticks and shows which button you press |
| `intelligence/fast_path.py` | Instant, deterministic phrase matching |
| `intelligence/llm_client.py` | Qwen prompt, output validation, background annunciator |
| `intelligence/engagement_monitor.py` | Missile away / target destroyed / shoot list callouts |
| `intelligence/threat_engine.py` | Threat ranking and range checks |
| `audio/` | Piper TTS, radio filter, mic clicks |
| `dcs_scripts/` | Copies of the DCS export scripts (see its README) |
| `huey/` | UH-1H crew: `profile.py` (pipeline), `commands.py` (phrases, LLM prompt), `crew.py` (gunners, autopilot), `cockpit.py` (DCS-BIOS switches, radios, flares), `checklists.py`, `crew_chief.py` (callouts), `link.py` / `gauges.py` / `panel.py` / `bios_map.py` (data from DCS) |
| `huey_probe.py` | Shows what George reads in the Huey (switches, gauges, crew panel) |
| `tests/` | Test suite with a simulated George and Huey (see its README) |
| `core/george_wheel.py`, `wheel_probe.py` | Old wheel driver and a manual key-probe tool (not used by main.py) |

## Test

    python -m unittest discover tests -b

No DCS needed; nothing presses real keys. About a minute.

## Snapshots (version control)

- Once: double-click `setup_git.bat` (needs Git for Windows).
- After each good test: double-click `snapshot.bat`, or run `snapshot "what changed"`.
- See the history: `git log --oneline`. Get a file back as it was: `git checkout <id> -- path\to\file.py`.

The Piper voice model (`en_US-ryan-low.onnx`) and `logs/` are not in the snapshots.

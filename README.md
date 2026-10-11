# DCS George AI

Local voice co-pilot middleware for the DCS AH-64D: you fly in the pilot seat and talk to George
(the AI CPG) over push-to-talk. Speech is transcribed by Faster-Whisper, simple commands go
through a deterministic fast path, everything else through a local Qwen 2.5 7B (Ollama), and
George is driven through his own AI wheel (simulated keys) and DCS-BIOS. George's wheel and
Target List are read back from DCS, so he confirms what he actually did.

## Run

1. Start Ollama (`ollama serve`, model `qwen2.5:7b`).
2. Start DCS and a mission in the Apache pilot seat.
3. `python main.py`, then hold SPACE to talk. See **COMMANDS.md** for what George understands.

Console output is also saved to `logs\george_<date>_<time>.log` (newest 20 kept).

## Files

| Path | Role |
|---|---|
| `main.py` | Start-up, push-to-talk, the voice command pipeline, status reports |
| `config.py` | Ports, key bindings, timings, DCS-BIOS addresses |
| `core/george_interface.py` | Drives George's wheel; reads it back to confirm; Target List model |
| `core/george_ui.py` | Parses George's interface text from DCS (UDP 5007) |
| `core/wpn_page_listener.py` | Your actioned weapon / sight from the WPN page (UDP 5006) |
| `core/dcs_bios.py` | DCS-BIOS in (5010) and out (7778): master arm, weapon action switch |
| `core/state.py` | Thread-safe ApacheStateStore |
| `core/telemetry_listener.py` | Export.lua flight data, stores, nearby units (UDP 5005) |
| `core/focus.py` | Refuses to send keys unless DCS is the window in front |
| `core/console_log.py` | Mirrors the console to `logs/` |
| `intelligence/fast_path.py` | Instant, deterministic phrase matching |
| `intelligence/llm_client.py` | Qwen prompt, output validation, background annunciator |
| `intelligence/engagement_monitor.py` | Missile away / target destroyed / shoot list callouts |
| `intelligence/threat_engine.py` | Threat ranking and range checks |
| `audio/` | Piper TTS, radio filter, mic clicks |
| `dcs_scripts/` | Copies of the DCS export scripts (see its README) |
| `tests/` | Test suite with a simulated George (see its README) |
| `core/george_wheel.py`, `wheel_probe.py` | Old wheel driver and a manual key-probe tool (not used by main.py) |

## Test

    python -m unittest discover tests -b

No DCS needed; nothing presses real keys. About a minute.

## Snapshots (version control)

- Once: double-click `setup_git.bat` (needs Git for Windows).
- After each good test: double-click `snapshot.bat`, or run `snapshot "what changed"`.
- See the history: `git log --oneline`. Get a file back as it was: `git checkout <id> -- path\to\file.py`.

The Piper voice model (`en_US-ryan-low.onnx`) and `logs/` are not in the snapshots.

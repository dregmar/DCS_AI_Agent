# DCS George AI

Local voice crew middleware for DCS. In the **AH-64D** you fly in the pilot seat and talk to George
(the AI CPG); in the **UH-1H** you talk to the Huey crew (door gunners, crew chief, co-pilot).
The program sees which aircraft you're in and switches by itself. Push-to-talk either way. Speech is transcribed by Faster-Whisper, simple commands go
through a deterministic fast path, everything else through a local Qwen 2.5 7B (Ollama), and
George is driven through his own AI wheel (simulated keys) and DCS-BIOS. George's wheel and
Target List are read back from DCS, so he confirms what he actually did.

## Run

1. Start Ollama (`ollama serve`) with the model in `config.py` (`LLM_MODEL`, now `qwen3:8b`:
   `ollama pull qwen3:8b`). Qwen3's "thinking" is switched off for speed (needs a 2025 or newer
   Ollama). To compare models on the phrases from your logs: `python llm_bench.py` (close DCS
   first; the report goes to `logs\llm_bench_*.txt`). Back to the old model: `LLM_MODEL = "qwen2.5:7b"`.
   Start Ollama before DCS: loading the model takes about 13 s, once; it then stays loaded.
   Two-GPU PCs: the LLM can run on its own card (see "LLM on a second GPU" below).
2. Start DCS and a mission in the Apache pilot seat.
3. `python main.py`, then hold SPACE or your throttle PTT button to talk. See **COMMANDS.md** for
   what George understands, **COMMANDS_HUEY.md** for the Huey crew, and **COMMANDS_COMMON.md** for
   the radio menu and navigation (both aircraft).

### Radio menu by voice (both aircraft)

`dcs_scripts\george_comms_hook.lua` goes in `%USERPROFILE%\Saved Games\DCS\Scripts\Hooks\`
(DCS reads that folder at start-up, so restart DCS once). It lets George read the comms menu and
pick items by name. `Saved Games\DCS\Logs\george_comms_hook_status.txt` should say "comms menu
module found". Without it, George can only press \ and F1–F12.

### First-flight checks for the new features

- **Sling loads:** `Saved Games\DCS\Logs\george_telemetry_status.txt` has a line "cargo objects in
  LoGetWorldObjects: N". 0 in a mission that has cargo means DCS doesn't list cargo to the export,
  and the crew chief can't talk you onto a load.
- **Route:** the same file says "route from LoGetRoute: N points". Check "next waypoint" names
  the waypoint your HSI shows.
- **Apache:** run `python apache_probe.py` once (instead of main.py): it prints the EUFD lines and
  where the power levers read OFF / IDLE / FLY, so the checklists and caution callouts can be
  fine-tuned.

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
| `intelligence/ollama_chat.py` | Talks to Ollama's own API: JSON answers, Qwen3 thinking off, `<think>` blocks stripped |
| `llm_bench.py` | Compares models (speed and right answers) on the phrases that reach the LLM |
| `intelligence/engagement_monitor.py` | Missile away / target destroyed / shoot list callouts |
| `intelligence/threat_engine.py` | Threat ranking and range checks |
| `audio/` | Piper TTS, radio filter, mic clicks |
| `dcs_scripts/` | Copies of the DCS export scripts (see its README) |
| `huey/` | UH-1H crew: `profile.py` (pipeline), `commands.py` (phrases, LLM prompt), `crew.py` (gunners, autopilot), `cockpit.py` (DCS-BIOS switches, radios, flares), `checklists.py`, `crew_chief.py` (callouts), `link.py` / `gauges.py` / `panel.py` / `bios_map.py` (data from DCS) |
| `huey_probe.py` | Shows what George reads in the Huey (switches, gauges, crew panel) |
| `common/` | Both aircraft: `comms_menu.py` (radio menu by voice), `nav.py` / `nav_commands.py` (waypoints, home, fuel to home) |
| `apache/` | AH-64D extras: `checklists.py`, `crew.py` (checklists, EUFD questions), `eufd.py` (warning / caution callouts), `bios_map.py` |
| `huey/talkdown.py` | Huey crew chief: landing / hover talk-down and sling-load talk-on |
| `apache_probe.py` | Shows what George reads in the Apache (EUFD, power levers, switches) |
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

## LLM on a second GPU

The setup this was tested with: an RX 5700 XT (8 GB) runs only the LLM, the RX 7900 XTX (24 GB)
only DCS. Ollama (0.40.6, Windows) uses its Vulkan backend for the 5700 XT, because ROCm doesn't
support that card (gfx1010). User environment variables:

```
HIP_VISIBLE_DEVICES=-1        # hides the 7900 XTX from ROCm
GGML_VK_VISIBLE_DEVICES=1     # Vulkan device 1 = the 5700 XT
OLLAMA_KEEP_ALIVE=-1          # never unload the model
OLLAMA_CONTEXT_LENGTH=8192
```

Don't set `HSA_OVERRIDE_GFX_VERSION` (it can break the 7900 XTX). `ollama ps` should show
`qwen3:8b` at about 6.2 GB, 100% GPU. Whisper and Piper run on the CPU, so neither GPU is touched
by them. The prompts are about 1,100 (George) and 650 (Huey) tokens plus a short history, far under
the 8192 context.

# DCS George AI

A 100% local voice crew, command chain and VR kneeboard for DCS World. Everything runs on your PC:
speech recognition, the language model, the voices and the kneeboard web page. Nothing goes to the
internet.

- **AH-64D:** you fly in the pilot seat and talk to **George** (the AI CPG). He is driven through his
  own AI wheel (simulated keys) and DCS-BIOS, and his wheel and Target List are read back from DCS,
  so he confirms what he actually did.
- **UH-1H:** you talk to the **Huey crew** (crew chief, door gunners, co-pilot): checklists, gunner
  orders, landing / hover talk-down and sling-load talk-on.
- **Any aircraft** (A-10C and F/A-18C settings included): **Overlord** (air control) sends AI flights
  through DCS Olympus, **Darkside** (JTAC) talks you onto targets and clears CAS, **Warlord**
  (mission commander) approves or denies tasking, your wingmen and independent **Outcasts** take
  orders, and the **Instructor** runs scored exercises in training mode.
- **VR kneeboard:** a web page shown in OpenKneeboard with the tactical map, briefing, checklists,
  comms log, system status, alerts and training scores.

The program sees which aircraft you're in and switches by itself. See **COMMANDS.md** (George),
**COMMANDS_HUEY.md** (Huey crew), **COMMANDS_COMMON.md** (everything else) and **KNEEBOARD.md**.

## How it works

    push-to-talk -> Whisper (small.en, CPU) -> fast path (fixed phrases, instant)
                 -> router (qwen3:8b on Ollama, only when no phrase matched)
                 -> handler -> gates (what you said + live state) -> Piper voice (radio filter)

- Most commands never reach the model. When one does, its answer must pass checks against what you
  said and the live state before anything is done or spoken. A rewrite that worked is saved to
  `data\learned_phrases.json` and caught instantly next time.
- Orders that are hard to undo are read back first ("Confirm, Outcast 2, engage air defences?";
  answer "affirm" or "negative").
- Safety orders ("cease fire", "abort", "weapons hold" ...) jump the queue and cancel older orders.
- Everything that talks to DCS, Olympus or Ollama runs in background threads, so DCS frame times
  are not affected.

## What's built

| Area | Status |
|---|---|
| George (AH-64D): weapons, TADS, Target List, checklists, EUFD warnings and cautions | Flown |
| Huey crew (UH-1H): checklists, gunners, talk-down, sling-load talk-on | Flown |
| Radio menu by voice, wingman orders (radio menu, or Olympus when it runs) | Flown |
| Independent wingmen ("Outcast 2, ...") through Olympus | Flown |
| Overlord: SEAD, CAS, CAP, escort, picture, pop-up / bandit warnings | Flown |
| Briefing questions (campaign, mission, procedures, imported aircraft manuals) | Flown |
| Position: grid, lat/long, bullseye; named waypoints and the objective; nearest field; debrief | Flown |
| Jets (A-10C, F/A-18C): per-aircraft settings, joker / bingo, Overlord as JTAC | Flown (joker / bingo not yet) |
| Supercarrier calls by voice, quiet window during the approach | Flown (Case I / ball not captured) |
| Tactical picture: one track per unit, threats, "what's changed", contact details | Flown |
| Warlord and Darkside (command roles): approval, denial and override, weapons-hold CAS cleared hot by Darkside | Flown 1 Nov |
| Training mode: training targets (Olympus Game Master) and the Instructor's exercises (hover, hover turn, approach, fuel), adaptive levels, scores | Flown 1 Nov |
| VR kneeboard: map, Brief, Checklist, Comms, Status, Alerts, Training; voice page commands | Flown (1 Nov amber redesign: not yet in VR) |
| Kneeboard map: your grid, friendly units within 5 km, tap a marker for its details | Built 1 Nov, not flown |

**Planned:** task from the map ("Outcast 2, attack that"), routes around threat rings, threat-ring
sizes from Olympus's unit database, map tiles (design waiting for answers); after-action replay;
spoken "system check"; Overlord as AWACS (BRAA, declare) and tanker; joystick buttons for the
kneeboard; Instructor phases 4-6 (weapons, sling load, control inputs); radio roles through SRS.

## Requirements

- Windows, DCS World, Python 3 with the packages the program imports (faster-whisper, piper,
  sounddevice, numpy, scipy, pynput; `pygame-ce` recommended for joystick PTT; `pypdf` only for
  `manual_import.py`).
- **Ollama** with `qwen3:8b` (`ollama pull qwen3:8b`; a 2025 or newer Ollama, so "thinking" can be
  switched off).
- **Piper voices** in this folder (each `.onnx` with its `.onnx.json`):

  | Voice file | Who |
  |---|---|
  | `en_US-ryan-low.onnx` | George and the Huey crew (intercom) |
  | `en_US-joe-medium.onnx` | Overlord |
  | `en_US-norman-medium.onnx` | Warlord |
  | `en_US-bryce-medium.onnx` | Darkside |
  | `jenny.onnx` | The Instructor |

  A missing voice falls back to another one; the Status page says which. `python voice_probe.py`
  plays one line in each voice.
- **DCS Olympus 2.0.5** (optional, but needed for Overlord's flights, Outcasts, Olympus wingman
  orders, smoke / laser marks and training targets).
- **DCS-BIOS** (switch read-back and cockpit commands).
- **OpenKneeboard** (optional) for the kneeboard in VR.

## Set up once

1. **DCS scripts** (sources in `dcs_scripts\`, see its README):
   - `Export.lua`, `george_ai_export.lua`, `george_export.lua`, `george_huey_export.lua` go in
     `%USERPROFILE%\Saved Games\DCS\Scripts\`. Export.lua is read at each mission start.
   - `george_comms_hook.lua` goes in `...\Saved Games\DCS\Scripts\Hooks\`. DCS loads hooks only when
     the game starts, so restart DCS after copying it. It gives the radio menu, the mission route,
     the briefing, your flight, unit types, airfields, DCS fuel and your callsign.
   - Status files: `Saved Games\DCS\Logs\george_*_status.txt`.
2. **config.py** (your settings; it also holds the Olympus logins, so keep it to yourself):
   - Olympus: `OLYMPUS_ENABLED`, `OLYMPUS_URL`, `OLYMPUS_USER`, `OLYMPUS_PASSWORD` (Blue commander).
     Training targets need a second, Game Master login: `OLYMPUS_GM_USER`, `OLYMPUS_GM_PASSWORD`.
   - Push-to-talk: `PTT_MODE` (`"keyboard"`, `"joystick"` or `"both"`), `JOY_NAME_CONTAINS`,
     `JOY_DEVICE_INDEX`, `JOY_BUTTON`. `python joy_probe.py` lists devices and button numbers.
   - Speech model: `WHISPER_MODEL = "small.en"` (about 1.1 s; `"base.en"` is faster, about 0.35 s,
     but mishears callsigns). Not `"distil-small.en"`.
   - Optional switches: `ROUTER_ENABLED`, `COMMAND_ROLES_ENABLED`, `COMMAND_SHORT_FORM`,
     `AI_TO_AI_SPOKEN`, `OPERATOR_CALLSIGN`, `OVERLORD_AIR_WARNINGS`, `FUEL_CALLS`,
     `INSTRUCTOR_ENABLED`, `KNEEBOARD_ENABLED`, `KNEEBOARD_PORT` (5000), `KNEEBOARD_TRACK`,
     `KNEEBOARD_AUTO_CHECKLIST`, `FRIENDLY_SHOW_M` (5000), `AIRCRAFT_OVERRIDES`.
     To list your current settings without the passwords: `python .claude\tools\show_config.py`.
3. **Kneeboard:** in OpenKneeboard, Settings > Tabs > Add tab > Web Dashboard, URL
   `http://127.0.0.1:5000/`. It also works in a normal browser on this PC.
4. **Your briefing (optional):** `data\campaign_briefing.json` by hand (format in `data\examples\`)
   or `python briefing_import.py my_briefing.txt`. Aircraft manuals:
   `python manual_import.py "<manual>.pdf" --aircraft <DCS type>` (they stay on this PC).
5. **First flights in a new aircraft:** `python apache_probe.py` (Apache), `python huey_probe.py`
   (Huey), `python jet_probe.py` (A-10C / Hornet checklists) instead of main.py, to check what the
   program reads.

## Run

1. Start Ollama before DCS (the model takes about 13 s to load once, then stays loaded).
2. Start DCS (and Olympus, if you use it) and a mission.
3. `python main.py`. Hold your PTT button (or SPACE) to talk. The console ends with
   `>>> ALL SYSTEMS ONLINE <<<`, and the kneeboard address is printed.

The console is also saved to `logs\george_<date>_<time>.log` (newest 20 kept). Debriefs go to
`logs\debrief_*.txt`.

## Where things are

| Path | What |
|---|---|
| `main.py` | Start-up, push-to-talk, voice and command workers, shared handlers, aircraft profile switching |
| `config.py` | Your settings (ports, PTT, models, Olympus logins) |
| `core/` | State store, telemetry (UDP 5005), DCS-BIOS (5010 / 7778), fuel, urgent-order lanes, latency metrics |
| `intelligence/` | Ollama client, George's prompt and gates, fast path, router, threat engine, engagement callouts |
| `apache/` | George: wheel map, crew, checklists, EUFD |
| `huey/` | UH-1H crew: commands, checklists, crew chief callouts, talk-down |
| `common/` | Radio menu, wingmen, Olympus client, Outcasts, Overlord, Darkside, Warlord, JTAC, carrier, training, briefing, position, waypoints, airfields, debrief, per-aircraft settings, fuel calls |
| `command/` | Command roles: tasking registry, permission table, rules, callsigns, wake words, exchange form |
| `tactical/` | Tactical picture: tracks, fusion, threats, the picture service |
| `instructor/` | The Instructor: exercises, metrics, scoring, adaptive levels, history (read-only: never commands anything) |
| `kneeboard/` | Kneeboard web server (HTTP + Server-Sent Events, 127.0.0.1 only), map model, pages, alerts, track, comms log, `static/index.html` |
| `audio/` | Piper voices, radio filter, mic clicks |
| `dcs_scripts/` | Copies of the DCS scripts |
| `data/` | Briefings, procedures, manuals, learned phrases, unit types, waypoints, instructor history |
| `tests/` | Unit tests with fakes for DCS, Olympus, Ollama and audio |
| `*_probe.py`, `llm_bench.py`, `llm_cache_check.py`, `briefing_import.py`, `manual_import.py` | Tools |

Ports (all localhost): 5005 Export, 5006 / 5007 George UI and weapons, 5008 / 5009 Huey, 5010 / 7778
DCS-BIOS, 5011 / 5012 comms hook, 4512 Olympus, 11434 Ollama, 5000 kneeboard.

## Test

    python -m unittest discover tests -b
    python -m unittest discover -s tests -t .

Both must pass (they load the tests under different names). No DCS, Olympus, Ollama, microphone or
sound card needed; nothing presses real keys. Don't run long tests while flying.

## Snapshots (version control)

- Once: double-click `setup_git.bat` (needs Git for Windows).
- After each good test: double-click `snapshot.bat`, or run `snapshot "what changed"`. It also
  copies the live DCS scripts from Saved Games into `dcs_scripts\` first.
- History: `git log --oneline`. A file as it was: `git checkout <id> -- path\to\file.py`.

The voice models and `logs/` are not in the snapshots.

## LLM on a second GPU

Tested set-up: an RX 5700 XT (8 GB) runs only the model, the RX 7900 XTX (24 GB) only DCS. Ollama
uses its Vulkan backend for the 5700 XT (ROCm doesn't support gfx1010). User environment variables:

```
HIP_VISIBLE_DEVICES=-1        # hides the 7900 XTX from ROCm
GGML_VK_VISIBLE_DEVICES=1     # Vulkan device 1 = the 5700 XT
OLLAMA_KEEP_ALIVE=-1          # never unload the model
OLLAMA_CONTEXT_LENGTH=8192
```

Don't set `HSA_OVERRIDE_GFX_VERSION` (it can break the 7900 XTX). `ollama ps` should show
`qwen3:8b` at about 6.2 GB, 100% GPU. Whisper and Piper run on the CPU. To compare models on the
phrases from your logs: `python llm_bench.py` (close DCS first; report in `logs\llm_bench_*.txt`).

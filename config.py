import os
from pynput.keyboard import Key

# --- PATHS & HARDWARE CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PIPER_MODEL_PATH = os.path.join(BASE_DIR, "en_US-ryan-low.onnx")

# Audio Output Device Index (User specified device #5)
AUDIO_OUTPUT_DEVICE = 5
MIC_DEVICE = 1            # None = Windows default input. Pin an index from sd.query_devices() if needed.
SAMPLE_RATE = 16000

# Push-To-Talk & Input Settings
PTT_KEY = Key.space
PTT_MODE = "both"                         # "joystick", "keyboard" or "both"
JOY_NAME_CONTAINS = "TECS MODERN THROTTLE"  # part of the device name, e.g. "Warthog", "VKB" ("" = use the index)
JOY_DEVICE_INDEX = 5                      # fallback: the [index] joy_probe.py prints (None = name only)
JOY_BUTTON = 17                           # 1-based button number joy_probe.py prints
JOY_BACKEND = "auto"                      # "auto" (pygame if installed, else Windows winmm), "pygame", "winmm"
KEY_DELAY = 0.08
MIN_UTTERANCE_SEC = 0.3      # ignore PTT taps shorter than this
TRAILING_CAPTURE_SEC = 0.2   # keep recording briefly after release so the last word isn't clipped

# --- STT ---
WHISPER_MODEL = "base.en"
WHISPER_CPU_THREADS = 4      # leave cores free for DCS
STT_PROMPT = ("Apache CPG commands: TADS, Hellfire, rockets, gun, master arm, "
              "arm, disarm, slave, Shilka, ZSU-23, lock target, search")

HUEY_STT_PROMPT = ("Huey crew commands: door gunners, left gunner, right gunner, co-pilot, weapons free, "
                   "hold fire, return fire, short bursts, long bursts, flares, UHF, VHF, FM, decimal, "
                   "checklist, check, autopilot, attitude hold, master arm, rockets")

# --- LLM ---
LLM_MODEL = "qwen2.5:7b"
LLM_TIMEOUT_SEC = 8
LLM_MAX_TOKENS = 160
UTTERANCE_MAX_AGE_SEC = 8.0  # PTT utterances queued longer than this are dropped unexecuted

# --- SAFETY / CONFIRMATION ---
READBACK_TIMEOUT_SEC = 0.5   # how long to wait for a DCS-BIOS switch to report the commanded state
TELEMETRY_STALE_SEC = 3.0    # telemetry older than this is treated as "no data"

# --- UH-1H HUEY (huey/) ---
HUEY_DATA_PORT = 5008               # in:  gauges, lamps, Crew Status panel (Scripts/george_huey_export.lua)
HUEY_CMD_PORT = 5009                # out: gunner ROE / burst, crew panel, autopilot (same script)
HUEY_CALLOUTS = True                # crew-chief callouts (limits, cautions, sink rate, gunners)
HUEY_CONTACT_CALLOUT_M = 1500       # call out enemy units closer than this (0 = never)
HUEY_AUTO_ARM_FLARES = True         # "flares!" arms the dispenser first if it's on SAFE
HUEY_FUEL_BURN_LBS_HR = 555         # for "about N minutes" (the 20-minute light is ~185 lbs)

# --- NETWORKING & PORTS ---
UDP_TELEMETRY_PORT = 5005   # Incoming Export.lua telemetry
GEORGE_UI_PORT = 5007       # Incoming George interface text (Scripts/george_ai_export.lua)
DCS_BIOS_IN_PORT = 5010     # Incoming DCS-BIOS multicast stream
DCS_BIOS_OUT_PORT = 7778    # Outgoing DCS-BIOS commands

# --- DCS-BIOS OUTPUT TABLE (what we READ from the 5010 stream) ---
# name -> (address, mask, shift).  Decoded field = (word & mask) >> shift
# Verified against Scripts/DCS-BIOS/doc/json/AH-64D.json (2026-10-10).
# The A/S control is a momentary PUSHBUTTON (PLT_MASTER_ARM_BTN, 0x86F4) that only reads 1 while
# held, so the armed STATE is read from the yellow ARM indicator light instead.
# Your actioned weapon (WAS) and selected sight are NOT in DCS-BIOS at all: they are read from
# the WPN page (george_export.lua -> UDP 5006 -> core/wpn_page_listener.py).
BIOS_OUTPUTS = {
    "master_arm":   (0x872A, 0x4000, 14),   # PLT_MASTER_ARM_ARM_L  (1 = ARM light on)
}

# --- MASTER ARM BUTTON (what we SEND to port 7778) ---
MASTER_ARM_IDENT = "PLT_MASTER_ARM_BTN"
MASTER_ARM_MODE = "pulse"          # pushbutton: press (1) then release (0) toggles ARM / SAFE
MASTER_ARM_ARMED_VALUE = 1         # decoded field value that means ARMED (flip to 0 if inverted)
BIOS_PRESS_HOLD_SEC = 0.10         # button hold time in "pulse" mode

# --- YOUR WEAPON ACTION SWITCH (cyclic hat, momentary; same direction again = de-action) ---
WAS_HAT = {                        # weapon -> (DCS-BIOS control, value to push; 1 = centre/off)
    "GUN": ("PLT_CYCLIC_WEAPON_UD", "2"),    # positions A / Off / G
    "MSL": ("PLT_CYCLIC_WEAPON_LR", "0"),    # positions M / Off / R
    "RKT": ("PLT_CYCLIC_WEAPON_LR", "2"),
}
WPN_PAGE_PORT = 5006               # WPN page text from Scripts/george_export.lua
WPN_PAGE_FRESH_SEC = 3.0           # WPN page data older than this = page not displayed
WAS_READBACK_SEC = 2.5             # george_export.lua scans once per second

# --- GEORGE WHEEL (core/george_wheel.py) ---
WHEEL_SHORT_HOLD_SEC = 0.15   # short press
WHEEL_LONG_HOLD_SEC = 0.70    # long press; DCS treats anything held > 0.5 s as long
WHEEL_OPEN_SETTLE_SEC = 0.20  # pause after opening the wheel before the next key
WHEEL_AUTO_HIDE_SEC = 0.0     # >0: hide the wheel after this many idle seconds (0 = leave it open)

# --- GEORGE CPG DRIVER (core/george_interface.py) ---
# Key names are pynput names: single characters, or Key.* names such as "ctrl_l".
GEORGE_SHOW_HIDE = ("ctrl_l", "v")
GEORGE_KEY_UP, GEORGE_KEY_DOWN = ("w",), ("s",)
GEORGE_KEY_LEFT, GEORGE_KEY_RIGHT = ("a",), ("d",)
GEORGE_KEY_MFI = ("f")              # bind "George AI - Multifunctional Input (Center)" in DCS, then set e.g. ("ctrl_r", "m")
GEORGE_KEY_STORE_TARGET = ("g")     # bind "George AI - TADS Store Target" in DCS, then set e.g. ("ctrl_r", "n")
GEORGE_SHORT_PRESS_S = 0.10        # < 0.5 s
GEORGE_LONG_PRESS_S = 0.75         # > 0.5 s
GEORGE_EXTRA_LONG_PRESS_S = 1.30   # > 1 s (CPG start-up / shutdown)
GEORGE_WEAPON_CYCLE = ["NO_WPN", "GUN", "MSL", "RKT"]   # only loaded weapons appear in George's cycle
GEORGE_IDLE_CLOSE_MAIN_S = 6.0     # hide George's wheel after this many idle seconds (None = never)

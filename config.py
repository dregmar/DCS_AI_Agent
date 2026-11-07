import os
from pynput.keyboard import Key

# --- PATHS & HARDWARE CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PIPER_MODEL_PATH = os.path.join(BASE_DIR, "en_US-ryan-low.onnx")        # George / crew (intercom)
OVERLORD_MODEL_PATH = os.path.join(BASE_DIR, "en_US-joe-medium.onnx")   # Overlord (command radio)
DARKSIDE_MODEL_PATH = os.path.join(BASE_DIR, "en_US-bryce-medium.onnx") # Darkside (JTAC radio)
WARLORD_MODEL_PATH = os.path.join(BASE_DIR, "en_US-norman-medium.onnx") #Warlord 
INSTRUCTOR_MODEL_PATH = os.path.join(BASE_DIR, "jenny.onnx") #Instructor

# Audio Output Device Index (User specified device #5)
AUDIO_OUTPUT_DEVICE = 5
MIC_DEVICE = 1            # None = Windows default input. Pin an index from sd.query_devices() if needed.
SAMPLE_RATE = 16000

# Push-To-Talk & Input Settings
PTT_KEY = Key.space
PTT_MODE = "both"                         # "joystick", "keyboard" or "both"
JOY_NAME_CONTAINS = "Gladiator EVO R"   # part of the device name, e.g. "Warthog", "VKB" ("" = use the index)
JOY_DEVICE_INDEX = 5                      # fallback: the [index] joy_probe.py prints (None = name only)
JOY_BUTTON = 21                           # 1-based button number joy_probe.py prints
# (was TECS MODERN THROTTLE button 17 until 19 Oct 2026)
JOY_BACKEND = "auto"                      # "auto" (pygame if installed, else Windows winmm), "pygame", "winmm"
KEY_DELAY = 0.08
MIN_UTTERANCE_SEC = 0.3      # ignore PTT taps shorter than this
TRAILING_CAPTURE_SEC = 0.2   # keep recording briefly after release so the last word isn't clipped

# --- STT ---
WHISPER_MODEL = "small.en"    # "base.en" (~0.35 s) or "small.en" (~1.1 s, hears callsigns better).
                              # Not "distil-small.en": it can't use the prompt and wrote garbage (24 Oct).
                              # The console shows each one's time: "[PILOT]: '...' (STT 0.42s for 2.1s audio)"
WHISPER_CPU_THREADS = 4      # leave cores free for DCS
STT_PROMPT = ("Apache CPG commands: fuel, status, TADS, Hellfire, rockets, gun, master arm, "
              "arm, disarm, slave, Shilka, ZSU-23, lock target, search, weapons free, weapons hold, "
              "cease fire, zoom, laser, waypoint, George wheel, wingman, flight, return to base, rejoin, "
              "engage my target, cover me, air defences, pincer, follow me, lase, laser code, pop smoke, "
              "weapons hold, take out the ZSU, Overlord, requesting SEAD strike, CAS, close air support, CAP, escort, north of my position, "
              "package priority, rules of engagement, briefing, sector, frequencies, go independent, Outcast 2, "
              "engage at will, RTB, grid, bullseye, mark our position as waypoint Alpha, Bravo, objective, "
              "nearest FARP, divert, debrief")

HUEY_STT_PROMPT = ("Huey crew commands: door gunners, left gunner, right gunner, co-pilot, weapons free, "
                   "hold fire, return fire, short bursts, long bursts, flares, UHF, VHF, FM, decimal, "
                   "checklist, check, autopilot, attitude hold, master arm, rockets, fuel, status, "
                   "sling load, hook the load, wingman, flight, return to base, rejoin, cover me, follow me, "
                   "land next to me, pop smoke, Overlord, requesting SEAD strike, CAS, close air support, CAP, escort, north of my position, "
              "package priority, rules of engagement, briefing, sector, frequencies, go independent, Outcast 2, "
              "engage at will, RTB, grid, bullseye, mark our position as waypoint Alpha, Bravo, objective, "
              "nearest FARP, divert, debrief")

JET_STT_PROMPT = ("Hornet and A-10 radio calls: Marshal, inbound, established, commencing, platform, check in, "
                  "request azimuth, abort inbound, ball, Clara, see you at ten, mother, say bingo, joker, bingo, "
                  "fuel to home, Overlord, requesting SEAD, CAS, CAP, escort, picture, bogey dope, two, go independent, "
                  "Outcast 2, rejoin, hold position, engage at will, RTB, weapons free, grid, bullseye, waypoint Alpha, "
                  "map carrier menu, nearest friendly airfield, debrief, JTAC, request target, next target, mark target, "
                  "BDA, good effect")

# --- DCS Olympus (wingman orders the radio menu can't give: attack a chosen target, follow,
# waypoints, land, weapons hold/free, lase, smoke). Saved Games\DCS\Config\olympus.json has the
# backend port; the password is the Blue commander one set in the Olympus Manager.
# Overlord calls enemy aircraft on his own within 40 km ("pop-up group"), and again inside 15 km
OVERLORD_AIR_WARNINGS = True
OLYMPUS_ENABLED = True
OLYMPUS_URL = "http://localhost:4512"     # "localhost", not 127.0.0.1: that's the name Olympus listens on
OLYMPUS_USER = "Pilot"
OLYMPUS_PASSWORD = "thor"                 # Blue commander

# --- Training targets (audit step 7; feature 8 spawns them). Off unless you turn it on here or say
# "Overlord, training mode on". Training uses a SECOND Olympus login, as Game Master, which may only
# spawn enemy units near you while training is on and only touch the units it spawned. Fill in the
# Game Master password (from the Olympus Manager) here only; it never goes anywhere else.
TRAINING_MODE = False             # start with training mode on
TRAINING_VOICE_SWITCH = True      # allow "Overlord, training mode on / off"
OLYMPUS_GM_USER = "Pilot"              # Game Master login name ("" = same as OLYMPUS_USER)
OLYMPUS_GM_PASSWORD = "zeus"          # Game Master password; empty = no training
TRAINING_MAX_GROUPS = 6           # training groups alive at once
TRAINING_MAX_UNITS = 24           # training units alive at once
TRAINING_MAX_RANGE_KM = 50        # spawn points must be this close to you

# --- LLM ---
# When no fixed phrase catches an order (or Overlord / an Outcast says "say again"), the crew's
# model may rewrite it into a standard order ("Ocast 2, destroy the defences" -> "Outcast 2,
# engage air defences"); see intelligence/router.py. What worked is saved in
# data/learned_phrases.json and recognised instantly next time. False = the old behaviour.
ROUTER_ENABLED = True

# --- VR kneeboard (kneeboard/): add http://127.0.0.1:5000/ as a web dashboard tab in OpenKneeboard ---
KNEEBOARD_ENABLED = True
KNEEBOARD_PORT = 5000             # change if another program uses 5000
KNEEBOARD_AUTO_CHECKLIST = True   # the kneeboard turns to the Checklist page when a checklist starts
KNEEBOARD_TRACK = True            # your track on the map ("kneeboard, track on / off" for this session)
DCS_BIOS_JSON_DIR = None          # None = Saved Games\DCS\Scripts\DCS-BIOS\doc\json (jet checklists read it)

# --- Jets (A-10C, F/A-18C...): common/aircraft.py has the numbers per type ---
FUEL_CALLS = False                 # Overlord's joker / bingo calls in jets
AIRCRAFT_OVERRIDES = {}           # e.g. {"FA-18C_hornet": {"reserve_lbs": 3500, "joker_margin_lbs": 2000}}
LLM_MODEL = "qwen3:8b"           # "ollama pull qwen3:8b". Back to the old one: "qwen2.5:7b".
                                 # Compare them with: python llm_bench.py
LLM_TIMEOUT_SEC = 12      # answers take ~0.7 s; a prompt Ollama has to read cold, ~7 s on the 5700 XT
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
HUEY_FUEL_RESERVE_LBS = 185         # "bingo fuel" when flying home would leave less than this
HUEY_TALKDOWN = True                # crew chief talks you down (heights, drift) and onto sling loads

# --- AH-64D EXTRAS (apache/) ---
APACHE_EUFD_CALLOUTS = True         # George reads new EUFD warnings / cautions aloud
APACHE_EUFD_WCA_ROWS = 6            # top EUFD lines that hold warnings | cautions | advisories
APACHE_EUFD_COLUMNS = [(0, 18), (19, 37), (38, 56)]

# --- RADIO MENU (common/comms_menu.py, Scripts/Hooks/george_comms_hook.lua) ---
COMMS_MENU_PORT = 5011              # in:  menu caption and items
COMMS_CMD_PORT = 5012               # out: OPEN / SELECT n / BACK / CLOSE

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
GEORGE_IDLE_CLOSE_MAIN_S = 0.0     # hide George's wheel after this many idle seconds (None = never)


# --- KNEEBOARD FRIENDLY UNITS DISPLAY ---
FRIENDLY_SHOW_M = 30000          # Maximum distance (metres) for displaying friendly units
                                 # on the kneeboard tactical map (30000 = 30 km).


# --- DIRECT ORDERS: RESTRICTED ZONE ENFORCEMENT ---
# Controls how the AI handles direct orders involving restricted or protected zones.
# "warn"  = report a zone conflict without automatically blocking the order.
# "block" = prevent orders that violate the configured zone restrictions.
DIRECT_ORDERS_ZONES = "warn"     # Options: "warn" or "block"


# --- JTAC LASER DESIGNATION RANGES ---
# Maximum permitted laser-designation distances for JTAC-controlled assets.
# Different limits apply to ground-based observers and airborne drones.
JTAC_GROUND_LASER_M = 8000       # Ground JTAC laser range in metres (8 km)
JTAC_DRONE_LASER_M = 20000       # Drone JTAC laser range in metres (20 km)


# --- WARLORD & DANGER-CLOSE SAFETY THRESHOLDS ---
# Distance and altitude thresholds used for tactical safety assessment.
WARLORD_CAUTION_M = 5000        # Caution distance in metres (5 km) for Warlord safety checks
DANGER_CLOSE_AGL_M = 300        # Altitude above ground level in metres (300 m)
                                 # used for danger-close safety assessment
DEFER_WAIT_S = 300


NINE_LINE_READBACK = False    # require lines 4 and 6 read back before IN
NINE_LINE_AI_TO_AI = False     # Darkside briefs the Hog flights too
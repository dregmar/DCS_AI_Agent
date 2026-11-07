"""
audio/tts.py - radio speech, two voices:
    george    intercom: George in the Apache, the crew in the Huey  (en_US-ryan-low)
    overlord  command radio: wingman / Olympus tasking and replies   (en_US-joe-medium)

Both Piper voices are loaded into RAM at start-up (CPU only). One worker thread plays everything,
so transmissions never overlap whichever voice they use. If the Overlord voice can't be loaded,
Overlord's lines are spoken in George's voice instead (nothing is lost).
"""
import heapq
import itertools
import os
import queue
import threading
import time

import numpy as np
import sounddevice as sd
from piper.voice import PiperVoice

from config import PIPER_MODEL_PATH, OVERLORD_MODEL_PATH, AUDIO_OUTPUT_DEVICE
from audio.dsp import apply_radio_filter, generate_mic_click
from core.metrics import METRICS

# Lower number = spoken first.
PRIORITY_ALERT = 0    # unsolicited warnings from the annunciator
PRIORITY_PILOT = 1    # replies to the pilot
PRIORITY_ROUTINE = 2  # low-importance callouts

GEORGE = "george"
OVERLORD = "overlord"
INSTRUCTOR = "instructor"      # Instructor Mode (instructor/): jenny.onnx (Andre, 30 Oct; was bryce-medium)
WARLORD = "warlord"            # command roles P2-3 (Andre, 1 Nov): en_US-norman-medium.onnx
DARKSIDE = "darkside"          # command roles P2-3 (Andre, 1 Nov): en_US-bryce-medium.onnx
# A role whose voice file isn't there speaks in this voice instead (and the Status page says so)
FALLBACK = {WARLORD: OVERLORD, DARKSIDE: OVERLORD}

# Set by main.py while the PTT key is held. Non-pilot callouts wait while it is set,
# so George never talks over you.
ptt_active = threading.Event()

_queue: "queue.PriorityQueue" = queue.PriorityQueue()
# Backlog cap (audit step 4): past this many waiting transmissions the oldest ROUTINE one is dropped.
# Pilot replies and alerts are never dropped for room, so the queue only exceeds this with those.
MAX_QUEUED = 10
_seq = itertools.count()
_worker_started = False
_worker_lock = threading.Lock()


def _load(path: str, label: str):
    try:
        v = PiperVoice.load(path)
        print(f"[AUDIO] Piper voice '{label}' loaded ({path}).")
        return v
    except Exception as e:
        print(f"[AUDIO ERROR] Could not load Piper voice '{label}' ({path}): {e}")
        return None


def _voice_path(setting: str, fallback: str) -> str:
    """A voice's model file: the one chosen on the kneeboard's Settings page (core/settings.py), else
    config.py's, else fallback. Not there? The same file in the voices subfolder (6 Nov: Andre is moving
    the voices into F:\\DCS_AI_Agent\\voices)."""
    import config as _config
    try:
        from core import settings
        path = settings.get(setting, None) or fallback
    except Exception:
        path = getattr(_config, setting, None) or fallback
    if path and not os.path.exists(path):
        alt = os.path.join(getattr(_config, "BASE_DIR", "") or "", "voices", os.path.basename(path))
        if os.path.exists(alt):
            return alt
    return path


def _load_instructor():
    """The instructor's voice, only if its model is in the project folder (a missing file isn't an
    error: until it is downloaded the instructor speaks in George's voice)."""
    import config as _config
    path = _voice_path("INSTRUCTOR_MODEL_PATH", os.path.join(getattr(_config, "BASE_DIR", ""), "jenny.onnx"))
    if not os.path.exists(path):
        print(f"[AUDIO] Instructor voice not found ({path}): the instructor will use George's voice. "
              f"Put jenny.onnx and jenny.onnx.json in the project folder.")
        return None
    return _load(path, INSTRUCTOR)


def _load_role(label: str, setting: str, filename: str):
    """A command role's voice (P2-3), only if its model is in the project folder; until it's
    downloaded the role speaks in Overlord's voice (a missing file isn't an error)."""
    import config as _config
    path = _voice_path(setting, os.path.join(getattr(_config, "BASE_DIR", ""), filename))
    if not os.path.exists(path):
        print(f"[AUDIO] {label.capitalize()} voice not found ({path}): {label} will use Overlord's voice. "
              f"Put {os.path.basename(path)} and its .json in the project folder.")
        return None
    return _load(path, label)


VOICES = {GEORGE: _load(_voice_path("PIPER_MODEL_PATH", PIPER_MODEL_PATH), GEORGE),
          OVERLORD: _load(_voice_path("OVERLORD_MODEL_PATH", OVERLORD_MODEL_PATH), OVERLORD),
          INSTRUCTOR: _load_instructor(),
          WARLORD: _load_role(WARLORD, "WARLORD_MODEL_PATH", "en_US-norman-medium.onnx"),
          DARKSIDE: _load_role(DARKSIDE, "DARKSIDE_MODEL_PATH", "en_US-bryce-medium.onnx")}
voice = VOICES[GEORGE]            # kept for older code that reads tts.voice
if VOICES[OVERLORD] is None and voice is not None:
    print("[AUDIO] Overlord will use George's voice.")


SAMPLE = "sample"                 # the Settings page's "play a sample" voice (6 Nov)
_sample_lock = threading.Lock()
_sample_path = None


def speak_sample(path: str, text: str) -> bool:
    """The Settings page: one line in a voice that may not be in use yet. Loaded once and kept while the
    same voice is sampled again (one sample voice in memory at a time); then spoken through the normal
    queue. Slow work (loading the model) - call it from a worker thread, never the main / PTT threads."""
    global _sample_path
    with _sample_lock:
        if _sample_path != path or VOICES.get(SAMPLE) is None:
            v = _load(path, SAMPLE)
            if v is None:
                return False
            VOICES[SAMPLE], _sample_path = v, path
    speak_radio(text, SAMPLE, priority=PRIORITY_ROUTINE, max_age=20.0)
    return True


def _voice_for(speaker: str):
    return VOICES.get(speaker) or VOICES.get(FALLBACK.get(speaker)) or VOICES[GEORGE]


def speak_radio(text: str, speaker: str = GEORGE, priority: int = PRIORITY_PILOT, max_age=None) -> None:
    """Queues a transmission in the speaker's voice. NON-BLOCKING and safe to call from any thread.

    speaker:  "george" (intercom / crew), "overlord" (command radio), "instructor", "warlord", "darkside".
    priority: lower is spoken first (queue order only; a transmission in progress is never cut off).
    max_age:  seconds a queued callout stays worth saying; older ones are dropped unspoken.
    """
    if not text or _voice_for(speaker) is None:
        return
    uid = METRICS.current()                 # the utterance this replies to (core/metrics.py), or None
    METRICS.queued(uid)
    _queue.put((priority, next(_seq), time.monotonic(), max_age, speaker, text, uid))
    _trim_backlog()
    start_speech_worker()


def _trim_backlog() -> None:
    """Over MAX_QUEUED waiting: drop the oldest routine callout (never a pilot reply or an alert)."""
    with _queue.mutex:                      # the PriorityQueue's own lock; only list work under it
        items = _queue.queue
        if len(items) <= MAX_QUEUED:
            return
        routine = [it for it in items if it[0] >= PRIORITY_ROUTINE]
        if not routine:
            return
        oldest = min(routine, key=lambda it: it[1])          # lowest sequence number = queued first
        items.remove(oldest)
        heapq.heapify(items)
        _queue.unfinished_tasks = max(0, _queue.unfinished_tasks - 1)
    print(f"[RADIO] Speech backlog full, dropped callout: '{oldest[5]}'")
    METRICS.count("speech_dropped_full")
    METRICS.dropped(oldest[6])


def speak_george_radio(text: str, priority: int = PRIORITY_PILOT, max_age=None) -> None:
    """George / crew on the intercom (same as speak_radio(text, "george", ...))."""
    speak_radio(text, GEORGE, priority, max_age)


def speak_overlord(text: str, priority: int = PRIORITY_PILOT, max_age=None) -> None:
    """Overlord on the command radio."""
    speak_radio(text, OVERLORD, priority, max_age)


def speak_instructor(text: str, priority: int = PRIORITY_PILOT, max_age=None) -> None:
    """The instructor (same queue, DSP and playback as the others; George's voice until jenny.onnx is there)."""
    speak_radio(text, INSTRUCTOR, priority, max_age)


def speak_warlord(text: str, priority: int = PRIORITY_PILOT, max_age=None) -> None:
    """Warlord, the mission commander (Overlord's voice until en_US-norman-medium.onnx is there)."""
    speak_radio(text, WARLORD, priority, max_age)


def speak_darkside(text: str, priority: int = PRIORITY_PILOT, max_age=None) -> None:
    """Darkside, the JTAC / FAC (Overlord's voice until en_US-bryce-medium.onnx is there)."""
    speak_radio(text, DARKSIDE, priority, max_age)


# Called with (speaker, text) for every transmission actually played (kneeboard comms log), or None.
# Called once Piper has produced the audio, just before playback: a line that fails to synthesise or
# is dropped as stale never reaches the log (audit step 4).
ON_SPOKEN = None


def start_speech_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_worker, daemon=True, name="george-speech").start()


def _worker() -> None:
    """The ONLY thread that ever touches the audio device, so transmissions cannot overlap."""
    while True:
        priority, _, enqueued, max_age, speaker, text, uid = _queue.get()
        try:
            while priority != PRIORITY_PILOT and ptt_active.is_set():
                time.sleep(0.05)
            if max_age is not None and (time.monotonic() - enqueued) > max_age:
                print(f"[RADIO] Dropped stale callout: '{text}'")
                METRICS.count("speech_dropped_stale")
                METRICS.dropped(uid)
                continue
            started = []

            def on_start(synth, speaker=speaker, text=text, uid=uid, enqueued=enqueued):
                started.append(METRICS.playing(uid, enqueued, synth, time.monotonic()))
                if ON_SPOKEN is not None:      # kneeboard comms log (never blocks: a deque append)
                    try:
                        ON_SPOKEN(speaker, text)
                    except Exception as e:
                        print(f"[TTS] comms log: {e}")

            _play(text, speaker, on_start=on_start)
            if not started:                    # no audio came out of Piper: nothing was played
                METRICS.dropped(uid)
        except Exception as e:
            print(f"[TTS ERROR] Failed during speech playback: {e}")
            METRICS.count("speech_errors")
            METRICS.dropped(uid)


def _synthesize(text: str, v):
    audio_bytes = b""
    for chunk in v.synthesize(text):
        if hasattr(chunk, 'audio_int16_bytes') and chunk.audio_int16_bytes:
            audio_bytes += chunk.audio_int16_bytes
        elif hasattr(chunk, '_audio_int16_bytes') and chunk._audio_int16_bytes:
            audio_bytes += chunk._audio_int16_bytes
        elif hasattr(chunk, 'audio_float_array') and chunk.audio_float_array is not None:
            audio_int16 = (chunk.audio_float_array * 32767).astype(np.int16)
            audio_bytes += audio_int16.tobytes()
    if not audio_bytes:
        return None, 0
    audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
    sample_rate = getattr(v.config, 'sample_rate', 16000)   # ryan-low 16 kHz, joe-medium 22.05 kHz
    return audio_int16.astype(np.float32) / 32768.0, sample_rate


def _build_transmission(speech_float: np.ndarray, sample_rate: int) -> np.ndarray:
    """Radio DSP chain - unchanged; works at either voice's sample rate."""
    filtered_speech = apply_radio_filter(speech_float, sample_rate=sample_rate)
    cockpit_noise = np.random.normal(0, 0.005, len(filtered_speech)).astype(np.float32)
    speech_with_noise = np.clip(filtered_speech + cockpit_noise, -0.9, 0.9)

    click_on = generate_mic_click(click_type="on", sample_rate=sample_rate)
    click_off = generate_mic_click(click_type="off", sample_rate=sample_rate)
    pad_gap = np.zeros(int(sample_rate * 0.08), dtype=np.float32)

    # 500ms dithered tail prevents WASAPI buffer truncation on the output device
    dithered_tail = np.random.normal(0, 0.0001, int(sample_rate * 0.50)).astype(np.float32)

    return np.concatenate([click_on, pad_gap, speech_with_noise, pad_gap, click_off, dithered_tail])


def _play(text: str, speaker: str = GEORGE, on_start=None) -> None:
    """on_start(seconds spent synthesising): called just before playback (latency metrics only)."""
    print(f"[RADIO OUT {speaker.upper()}]: '{text}'")
    t_synth = time.monotonic()
    speech_float, sample_rate = _synthesize(text, _voice_for(speaker))
    if speech_float is None:
        return
    full = _build_transmission(speech_float, sample_rate)
    total_duration = len(full) / sample_rate
    if on_start is not None:
        try:
            on_start(time.monotonic() - t_synth)
        except Exception as e:
            print(f"[TTS] metrics: {e}")

    if AUDIO_OUTPUT_DEVICE is not None:
        sd.play(full, samplerate=sample_rate, device=AUDIO_OUTPUT_DEVICE)
    else:
        sd.play(full, samplerate=sample_rate)

    # Same proven time-based hold as before; safe now because only this worker plays audio.
    time.sleep(total_duration + 0.1)
    sd.stop()

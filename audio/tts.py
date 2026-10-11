import itertools
import queue
import threading
import time

import numpy as np
import sounddevice as sd
from piper.voice import PiperVoice

from config import PIPER_MODEL_PATH, AUDIO_OUTPUT_DEVICE
from audio.dsp import apply_radio_filter, generate_mic_click

# Lower number = spoken first.
PRIORITY_ALERT = 0    # unsolicited warnings from the annunciator
PRIORITY_PILOT = 1    # replies to the pilot
PRIORITY_ROUTINE = 2  # low-importance callouts

# Set by main.py while the PTT key is held. Non-pilot callouts wait while it is set,
# so George never talks over you.
ptt_active = threading.Event()

_queue: "queue.PriorityQueue" = queue.PriorityQueue()
_seq = itertools.count()
_worker_started = False
_worker_lock = threading.Lock()

voice = None
try:
    voice = PiperVoice.load(PIPER_MODEL_PATH)
    print("[AUDIO] Piper TTS engine loaded successfully.")
except Exception as e:
    print(f"[AUDIO ERROR] Could not load Piper model: {e}")


def speak_george_radio(text: str, priority: int = PRIORITY_PILOT, max_age=None) -> None:
    """Queues a radio transmission. NON-BLOCKING and safe to call from any thread.

    priority: lower is spoken first (queue order only; a transmission in progress is never cut off).
    max_age:  seconds a queued callout stays worth saying; older ones are dropped unspoken.
    """
    if not text or voice is None:
        return
    _queue.put((priority, next(_seq), time.monotonic(), max_age, text))
    start_speech_worker()


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
        priority, _, enqueued, max_age, text = _queue.get()
        try:
            while priority != PRIORITY_PILOT and ptt_active.is_set():
                time.sleep(0.05)
            if max_age is not None and (time.monotonic() - enqueued) > max_age:
                print(f"[RADIO] Dropped stale callout: '{text}'")
                continue
            _play(text)
        except Exception as e:
            print(f"[TTS ERROR] Failed during speech playback: {e}")


def _synthesize(text: str):
    audio_bytes = b""
    for chunk in voice.synthesize(text):
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
    sample_rate = getattr(voice.config, 'sample_rate', 16000)
    return audio_int16.astype(np.float32) / 32768.0, sample_rate


def _build_transmission(speech_float: np.ndarray, sample_rate: int) -> np.ndarray:
    """Radio DSP chain - unchanged from the previous version."""
    filtered_speech = apply_radio_filter(speech_float, sample_rate=sample_rate)
    cockpit_noise = np.random.normal(0, 0.005, len(filtered_speech)).astype(np.float32)
    speech_with_noise = np.clip(filtered_speech + cockpit_noise, -0.9, 0.9)

    click_on = generate_mic_click(click_type="on", sample_rate=sample_rate)
    click_off = generate_mic_click(click_type="off", sample_rate=sample_rate)
    pad_gap = np.zeros(int(sample_rate * 0.08), dtype=np.float32)

    # 500ms dithered tail prevents WASAPI buffer truncation on the output device
    dithered_tail = np.random.normal(0, 0.0001, int(sample_rate * 0.50)).astype(np.float32)

    return np.concatenate([click_on, pad_gap, speech_with_noise, pad_gap, click_off, dithered_tail])


def _play(text: str) -> None:
    print(f"[RADIO OUT]: '{text}'")
    speech_float, sample_rate = _synthesize(text)
    if speech_float is None:
        return
    full = _build_transmission(speech_float, sample_rate)
    total_duration = len(full) / sample_rate

    if AUDIO_OUTPUT_DEVICE is not None:
        sd.play(full, samplerate=sample_rate, device=AUDIO_OUTPUT_DEVICE)
    else:
        sd.play(full, samplerate=sample_rate)

    # Same proven time-based hold as before; safe now because only this worker plays audio.
    time.sleep(total_duration + 0.1)
    sd.stop()
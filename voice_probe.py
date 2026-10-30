"""
voice_probe.py - hear both radio voices on your output device (run it INSTEAD of main.py).

    python voice_probe.py
"""
import time

from audio import tts

tts.speak_george_radio("George here. Intercom check, how do you read?")
tts.speak_overlord("Overlord, radio check. Reading you five by five.")
time.sleep(1.0)
while not tts._queue.empty():
    time.sleep(0.2)
time.sleep(6.0)          # let the last transmission finish

import numpy as np
from scipy.signal import butter, lfilter

def butter_bandpass(lowcut=300.0, highcut=3000.0, fs=16000, order=4):
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq
    b, a = butter(order, [low, high], btype='band')
    return b, a

def apply_radio_filter(audio_data, sample_rate=16000):
    if len(audio_data) == 0:
        return audio_data
    b, a = butter_bandpass(300.0, 3000.0, fs=sample_rate, order=4)
    filtered = lfilter(b, a, audio_data)
    boosted = filtered * 1.8
    return np.clip(boosted, -0.8, 0.8).astype(np.float32)

def generate_mic_click(click_type="on", sample_rate=16000):
    if click_type == "on":
        duration = 0.035
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        pop = np.sin(2 * np.pi * 450 * t) * np.exp(-t * 120)
        noise = np.random.normal(0, 0.15, len(t)) * np.exp(-t * 60)
        return ((pop * 0.7) + (noise * 0.3)).astype(np.float32)
    else:
        duration = 0.060
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        noise = np.random.normal(0, 0.25, len(t))
        b, a = butter_bandpass(400.0, 2500.0, fs=sample_rate, order=2)
        filtered_tail = lfilter(b, a, noise)
        envelope = np.exp(-t * 70)
        return (filtered_tail * envelope * 0.5).astype(np.float32)
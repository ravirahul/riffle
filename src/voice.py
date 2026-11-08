"""Voice search for the quick bar: speech to text on this PC, from the microphone.

Offline streaming recognition (sherpa-onnx, a small English zipformer model), so words
appear while you speak and no audio leaves the computer. Comes in the optional voice pack.

    v = Voice(model_dir)
    v.warm()                                   # optional: load the model early (~4 s)
    v.start(on_text=lambda text, final: ..., on_level=lambda level: ..., on_end=lambda err: ...)
    v.stop()

The mic opens at once and buffers while the model loads, so nothing said early is lost.
The model is freed after a few idle minutes to give the memory back.
"""
import os
import queue
import threading
import time

import numpy as np

RATE = 16000
CHUNK = 0.06                     # seconds per mic block: ~16 level updates a second
IDLE_UNLOAD = 300                # seconds


class Voice:
    def __init__(self, model_dir):
        self.dir = model_dir
        self.rec = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._unload_timer = None

    def available(self):
        return os.path.exists(os.path.join(self.dir, 'tokens.txt'))

    @property
    def busy(self):
        return bool(self._thread and self._thread.is_alive())

    def _load(self):
        with self._lock:
            if self.rec is None:
                import sherpa_onnx
                f = lambda n: os.path.join(self.dir, n)
                self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
                    tokens=f('tokens.txt'), encoder=f('encoder.int8.onnx'), decoder=f('decoder.int8.onnx'),
                    joiner=f('joiner.int8.onnx'), num_threads=2, sample_rate=RATE, feature_dim=80,
                    decoding_method='greedy_search', enable_endpoint_detection=True,
                    rule1_min_trailing_silence=2.4,      # nothing said yet: give up after this much silence
                    rule2_min_trailing_silence=1.3,      # said something: stop after a pause (1.0 cut words)
                    rule3_min_utterance_length=20)
            return self.rec

    def warm(self):
        if self.rec is None and self.available():
            threading.Thread(target=self._load, daemon=True, name='voice-load').start()

    def _unload(self):
        if not self.busy:
            with self._lock:
                self.rec = None

    def start(self, on_text, on_level, on_end, max_seconds=20):
        if self.busy:
            return
        if self._unload_timer:
            self._unload_timer.cancel()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_text, on_level, on_end, max_seconds),
                                        daemon=True, name='voice')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, on_text, on_level, on_end, max_seconds):
        err, last = None, ''
        q = queue.Queue()

        def heard(block, frames, t, status):          # audio thread: keep it tiny
            x = block[:, 0].copy()
            q.put(x)
            db = 20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9)      # quiet room ~ -65 dB, speech ~ -25 dB
            on_level(float(min(1.0, max(0.0, (db + 62) / 42))))
        try:
            import sounddevice as sd
            with sd.InputStream(channels=1, samplerate=RATE, dtype='float32',
                                blocksize=int(RATE * CHUNK), callback=heard):
                rec = self._load()                       # the mic is already buffering meanwhile
                s, t0 = rec.create_stream(), time.time()
                while not self._stop.is_set() and time.time() - t0 < max_seconds:
                    try:
                        s.accept_waveform(RATE, q.get(timeout=0.2))
                    except queue.Empty:
                        continue
                    while rec.is_ready(s):
                        rec.decode_stream(s)
                    text = clean(rec.get_result(s))
                    if text != last:
                        last = text
                        on_text(text, False)
                    if rec.is_endpoint(s):
                        break
            on_text(last, True)
        except Exception as e:                      # no microphone, mic blocked in Windows privacy, ...
            err = str(e) or type(e).__name__
        finally:
            on_level(0.0)
            self._unload_timer = threading.Timer(IDLE_UNLOAD, self._unload)
            self._unload_timer.daemon = True
            self._unload_timer.start()
            on_end(err)


def clean(text):
    """The model speaks in capitals without punctuation: make it read like typing."""
    return ' '.join(text.lower().split())


def transcribe(model_dir, samples, rate=RATE):
    """Whole-clip transcription (tests and tools)."""
    rec = Voice(model_dir)._load()
    s = rec.create_stream()
    s.accept_waveform(rate, samples)
    s.accept_waveform(rate, np.zeros(int(rate * 0.6), dtype=np.float32))    # flush the last word
    s.input_finished()
    while rec.is_ready(s):
        rec.decode_stream(s)
    return clean(rec.get_result(s))


if __name__ == '__main__':
    import sys
    import wave
    assert clean('  HELLO   WORLD ') == 'hello world'
    if len(sys.argv) > 2:                          # python voice.py <model dir> <file.wav>
        with wave.open(sys.argv[2]) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            print(repr(transcribe(sys.argv[1], pcm, w.getframerate())))
    print('voice ok')

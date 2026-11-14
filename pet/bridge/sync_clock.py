from array import array
from bisect import bisect_right
from dataclasses import dataclass
from io import BytesIO
import math
import sys
import wave


@dataclass(frozen=True)
class Viseme:
    offset: int
    mouth: int = 0
    subtitle: str = ''


class PlaybackClock:
    def __init__(self, duration, frames):
        self.duration = duration
        self.frames = tuple(frames)
        self.offsets = tuple(frame.offset for frame in self.frames)
        self.cues = tuple(frame for frame in self.frames if frame.subtitle)
        self.cue_offsets = tuple(frame.offset for frame in self.cues)

    @classmethod
    def from_wire(cls, event):
        duration = event.get('durationMs')
        frames = event.get('visemes')
        if type(duration) is not int or not 0 < duration <= 10_000 or not isinstance(frames, list) or len(frames) > 501:
            raise ValueError('Invalid utterance clock')
        result = []
        shapes = {'closed': 0, 'open_narrow': 1, 'open_wide': 2}
        for frame in frames:
            offset = frame['offsetMs']
            if type(offset) is not int or not 0 <= offset < duration or (result and offset <= result[-1].offset):
                raise ValueError('Invalid viseme timestamp')
            subtitle = frame.get('subtitleText', '')
            if not isinstance(subtitle, str) or len(subtitle) > 250:
                raise ValueError('Invalid subtitle')
            result.append(Viseme(offset, shapes[frame['mouthShape']], subtitle))
        return cls(duration, result)

    @classmethod
    def from_wav(cls, data):
        with wave.open(BytesIO(data), 'rb') as wav:
            channels, rate, count = wav.getnchannels(), wav.getframerate(), wav.getnframes()
            if wav.getsampwidth() != 2:
                raise ValueError('Expected PCM16 waveform')
            samples = array('h', wav.readframes(count))
        if sys.byteorder != 'little':
            samples.byteswap()
        frames = []
        duration = math.ceil(count * 1000 / rate)
        for offset in range(0, duration, 20):
            window = samples[offset * rate // 1000 * channels:(offset + 20) * rate // 1000 * channels]
            rms = math.sqrt(sum(value * value for value in window) / max(1, len(window))) / 32768
            frames.append(Viseme(offset, 2 if rms >= .08 else 1 if rms >= .018 else 0))
        return cls(duration, frames)

    def at(self, position_ms):
        index = bisect_right(self.offsets, position_ms) - 1
        return self.frames[index] if index >= 0 and position_ms < self.duration else Viseme(position_ms)

    def subtitle_at(self, position_ms):
        next_cue = bisect_right(self.cue_offsets, position_ms)
        end = self.cue_offsets[next_cue] if next_cue < len(self.cues) else self.duration
        if next_cue and position_ms < self.duration:
            return self.cues[next_cue - 1].subtitle, end
        return '', end

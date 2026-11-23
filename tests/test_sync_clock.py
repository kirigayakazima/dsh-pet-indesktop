from io import BytesIO
import wave

from pet.bridge.sync_clock import PlaybackClock
from pet.bridge.crash_guard import CrashGuard


def test_pcm_visemes_follow_media_position_and_close_at_end():
    buffer = BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', ''))
        output.writeframes(b'\0\0' * 160 + b'\0\x40' * 160)
    clock = PlaybackClock.from_wav(buffer.getvalue())
    assert [clock.at(ms).mouth for ms in (0, 19, 20, 39, 40, 0)] == [0, 0, 2, 2, 0, 0]


def test_heartbeat_requires_negotiation_and_expires_at_fifteen_seconds():
    guard = CrashGuard()
    assert not guard.expired(100)
    guard.beat(10)
    assert not guard.expired(24.99)
    assert guard.expired(25)
    guard.reset()
    assert not guard.expired(20)


def test_subtitles_persist_between_visemes_and_follow_seeks():
    clock = PlaybackClock.from_wire({'durationMs': 80, 'visemes': [
        {'offsetMs': 0, 'mouthShape': 'closed', 'subtitleText': 'first'},
        {'offsetMs': 20, 'mouthShape': 'open_wide'},
        {'offsetMs': 40, 'mouthShape': 'closed', 'subtitleText': 'second'},
    ]})
    assert clock.subtitle_at(25) == ('first', 40)
    assert clock.subtitle_at(40) == ('second', 80)
    assert clock.subtitle_at(80) == ('', 80)
    assert clock.subtitle_at(0) == ('first', 40)

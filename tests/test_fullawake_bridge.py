import base64
from io import BytesIO
import json
from types import SimpleNamespace
import wave
import pytest

from PySide6.QtCore import QEventLoop, QRect, QTimer, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer
from PySide6.QtWidgets import QWidget

from pet.fullawake_bridge import FullAwakePetBridge

PLAY = QMediaPlayer.play


class PetTarget:
    def __init__(self):
        self.animation = None
        self.interrupted = False
        self.show_text = True

    def show_bubble(self, text, *, duration_ms, reserve=True):
        return self.show_text

    def request_link_anim(self, name, *, interrupt=False):
        self.animation = name
        self.interrupted = interrupt


def wait_for(signal, trigger):
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    signal.connect(loop.quit)
    try:
        timer.start(5000)
        trigger()
        loop.exec()
        assert timer.isActive(), 'Timed out waiting for Qt signal'
    finally:
        signal.disconnect(loop.quit)


def test_expired_expression_uses_heart_mood(qapp, monkeypatch):
    monkeypatch.setattr(FullAwakePetBridge, '_check_connection', lambda self: None)
    monkeypatch.setattr('pet.fullawake_bridge.time.monotonic', lambda: 10)
    target = PetTarget()
    shell = type('Shell', (), {'instance': type('Instance', (), {'win': target})()})()
    bridge = FullAwakePetBridge(shell)
    try:
        bridge._on_message(json.dumps({'event': 'heart_mood', 'mood': 'tired'}))
        bridge._on_message(json.dumps({'event': 'action', 'intent': 'smile'}))
        bridge._on_tick()
        assert target.animation == '点击回应-开心跃动'
        monkeypatch.setattr('pet.fullawake_bridge.time.monotonic', lambda: 14)
        bridge._on_tick()
        assert target.animation == '原地小憩沉眠'
    finally:
        bridge.reconnect_timer.stop()
        bridge.clock_timer.stop()


@pytest.mark.parametrize('topology', ['legacy', 'overlay'])
def test_viseme_overlay_is_positioned_and_passive_in_both_topologies(qapp, monkeypatch, topology):
    monkeypatch.setattr(FullAwakePetBridge, '_check_connection', lambda self: None)
    host = QWidget()
    region = QRect(50, 70, 100, 160)
    if topology == 'legacy':
        host.character_local_region = lambda: region
        shell = SimpleNamespace(instance=SimpleNamespace(win=host))
    else:
        overlay = SimpleNamespace(show_bubble=lambda *args: None, sprite=SimpleNamespace(body_rect=lambda: region),
                                  host_for_sprite=lambda sprite: SimpleNamespace(overlay=host))
        shell = SimpleNamespace(_overlay_shell=overlay)
    bridge = FullAwakePetBridge(shell)
    try:
        bridge._set_mouth(2)
        mouth = bridge.mouth_overlay
        assert mouth.parent() is host
        assert mouth.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert region.contains(mouth.geometry())
        assert not mouth.isHidden()
        bridge.stop_audio()
        assert mouth.isHidden()
    finally:
        bridge.reconnect_timer.stop()
        bridge.clock_timer.stop()
        host.deleteLater()


def test_fullawake_interrupt_stops_media_and_rejects_late_audio(qapp, monkeypatch):
    server = QWebSocketServer('test', QWebSocketServer.SslMode.NonSecureMode)
    assert server.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    monkeypatch.setenv('FULLAWAKE_PORT', str(server.serverPort()))
    target = PetTarget()
    shell = type('Shell', (), {'instance': type('Instance', (), {'win': target})()})()
    bridges = []
    wait_for(server.newConnection, lambda: bridges.append(FullAwakePetBridge(shell)))
    bridge = bridges[0]
    peer = server.nextPendingConnection()
    monkeypatch.setattr(QMediaPlayer, 'play', PLAY)
    bridge.audio.setMuted(True)
    buf = BytesIO()
    with wave.open(buf, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', ''))
        wav.writeframes(b'\x00\x02' * 160000)
    audio = {'event': 'playback_audio', 'utteranceId': 'old', 'audioBase64': base64.b64encode(buf.getvalue()).decode()}
    received = []
    peer.textMessageReceived.connect(lambda raw: received.append(json.loads(raw)))

    def send(value):
        peer.sendTextMessage(json.dumps(value))

    try:
        send({'event': 'action', 'utteranceId': 'old'})
        wait_for(bridge.player.playingChanged, lambda: send(audio))
        assert bridge.player.isPlaying()
        bridge.clock_timer.stop()
        wait_for(peer.textMessageReceived, bridge.request_interrupt)
        assert received[-1] == {'event': 'interrupt_request'}
        wait_for(peer.textMessageReceived, lambda: send({'event': 'interrupt', 'utteranceId': 'old', 'interruptId': 'stop-1'}))
        assert received[-1]['event'] == 'interrupt_ack'
        assert received[-1]['interruptId'] == 'stop-1'
        assert not bridge.player.isPlaying()
        assert bridge.current_mouth == 0 and target.animation == '东张西望' and target.interrupted
        wait_for(bridge.socket.textMessageReceived, lambda: send(audio))
        assert not bridge.player.isPlaying() and bridge.buffer is None
    finally:
        bridge.socket.abort()
        bridge.reconnect_timer.stop()
        bridge.clock_timer.stop()
        peer.abort()
        server.close()


@pytest.mark.parametrize('outcome', ['played', 'text_delivered', 'discarded'])
def test_fullawake_receipt_follows_media_end_or_visible_text(qapp, monkeypatch, outcome):
    server = QWebSocketServer('receipt', QWebSocketServer.SslMode.NonSecureMode)
    assert server.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    monkeypatch.setenv('FULLAWAKE_PORT', str(server.serverPort()))
    target = PetTarget()
    target.show_text = outcome != 'discarded'
    shell = type('Shell', (), {'instance': type('Instance', (), {'win': target})()})()
    bridges = []
    wait_for(server.newConnection, lambda: bridges.append(FullAwakePetBridge(shell)))
    bridge = bridges[0]
    peer = server.nextPendingConnection()
    monkeypatch.setattr(QMediaPlayer, 'play', PLAY)
    bridge.audio.setMuted(True)
    bridge.clock_timer.stop()
    receipts = []
    peer.textMessageReceived.connect(lambda raw: receipts.append(json.loads(raw)))

    def send(value):
        peer.sendTextMessage(json.dumps(value))

    action = {'event': 'action', 'utteranceId': 'current', 'voiceBridge': True, 'text': 'test',
              'bridgeText': outcome != 'played'}
    try:
        if outcome == 'played':
            send(action)
            buf = BytesIO()
            with wave.open(buf, 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', ''))
                wav.writeframes(b'\x00\x02' * 8000)
            audio = {'event': 'playback_audio', 'utteranceId': 'current',
                     'audioBase64': base64.b64encode(buf.getvalue()).decode()}
            wait_for(peer.textMessageReceived, lambda: send(audio))
            assert not bridge.player.isPlaying()
        else:
            wait_for(peer.textMessageReceived, lambda: send(action))
        assert len(receipts) == 1
        assert receipts[0]['utteranceId'] == 'current' and receipts[0]['outcome'] == outcome
    finally:
        bridge.socket.abort()
        bridge.reconnect_timer.stop()
        peer.abort()
        server.close()

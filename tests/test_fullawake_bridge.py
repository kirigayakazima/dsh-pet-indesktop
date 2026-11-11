import base64
from io import BytesIO
import json
import wave
import pytest

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer

from pet.fullawake_bridge import FullAwakePetBridge

PLAY = QMediaPlayer.play


class PetTarget:
    def __init__(self):
        self.animation = None
        self.interrupted = False
        self.show_text = True

    def show_bubble(self, text, *, duration_ms):
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

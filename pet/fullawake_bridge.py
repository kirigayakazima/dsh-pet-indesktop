# -*- coding: utf-8 -*-
"""FullAwake 桌面身体桥接器 (FullAwake Pet Bridge).

连接 PC FullAwake 网关 (ws://127.0.0.1:{port}/ws/pet?player=1)。
小满主脑始终在手机 DSHA 运行；PC 桌宠仅作为小满在电脑上的物理实体呈现器 (Client Renderer):
1. 接收手机小满的心智/陪看动作指令 (action)，展示气泡文字并驱动对应 webm 动画；
2. 接收手机/PC 生成的 RVC 语音流 (playback_audio)，播放音频并计算口型 (mouth) 反馈；
3. 用户在桌宠上的点击/拖拽等交互事件，实时回流观察流给 FullAwake (observe)；
4. 严格禁用桌宠自带的独立 LLM 聊天或第二人格系统，确保小满心智唯一。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, QUrl, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWidgets import QLabel, QWidget
from shiboken6 import isValid

from .bridge.crash_guard import CrashGuard
from .bridge.intent_smoother import AnimationSmoother, Priority
from .bridge.sync_clock import PlaybackClock

log = logging.getLogger("fullawake-bridge")


# Intent 到角色动作的映射表（按 shenshen 实际具备的 100+ webm 动画名映射）
INTENT_ANIM_CANDIDATES = {
    "think": ["深度思考碎碎念", "写代码"],
    "nod": ["点击回应-元气挥手", "点击回应-开心跃动"],
    "happy": ["点击回应-开心跃动", "可爱宅舞", "轻快摇摆舞"],
    "talk": ["悠闲哼歌", "深度思考碎碎念"],
    "curious": ["东张西望"],
    "surprised": ["被吓一跳", "点击回应-害羞惊讶"],
    "shock": ["被吓一跳"],
    "smile": ["点击回应-开心跃动"],
    "wave": ["点击回应-元气挥手"],
    "sleep": ["原地小憩沉眠", "打瞌睡被惊醒"],
    "eat": ["吃Token", "吃冰淇淋融化"],
    "praise": ["放烟花", "凭空生花", "点击回应-开心跃动"],
}

MOOD_IDLE_CLIPS = {
    "tired": "原地小憩沉眠",
    "cheerful": "悠闲哼歌",
    "excited": "轻快摇摆舞",
    "engaged": "工作状态-思考冒泡",
}


class FullAwakePetBridge(QObject):
    def __init__(self, app_shell, port: int = 8765):
        super().__init__()
        self.shell = app_shell
        self.port = int(os.environ.get("FULLAWAKE_PORT", port))
        self.url = QUrl(f"ws://127.0.0.1:{self.port}/ws/pet?player=1&heartbeat=1")

        self.socket = QWebSocket(parent=self)
        self.socket.setMaxAllowedIncomingMessageSize(5_000_000)
        self.socket.textMessageReceived.connect(self._on_message)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.connected.connect(self._on_connected)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.mediaStatusChanged.connect(self._on_media_status)

        self.current_utterance = ""
        self.bridged = False
        self.envelope: PlaybackClock | None = None
        self.buffer: QBuffer | None = None
        self.song_playing = False
        self.current_mouth = 0
        self.guard = CrashGuard()
        self.smoother = AnimationSmoother()
        self.heart_mood = "calm"
        self.pending_text = ""
        self.subtitle = ""
        self.mouth_overlay = None

        # 重连定时器
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.timeout.connect(self._check_connection)
        self.reconnect_timer.start(2000)

        # 口型与时钟同步定时器 (30fps)
        self.clock_timer = QTimer(self)
        self.clock_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.clock_timer.timeout.connect(self._on_tick)
        self.clock_timer.start(33)

        self._check_connection()
        log.info("FullAwakePetBridge initialized for %s", self.url.toString())

    def _get_target(self):
        # 优先从 overlay_shell 获取呈现目标
        overlay = getattr(self.shell, "_overlay_shell", None)
        if overlay is not None and hasattr(overlay, "show_bubble"):
            return overlay
        # 回退到 legacy 窗口
        inst = getattr(self.shell, "instance", None)
        if inst and getattr(inst, "win", None):
            return inst.win
        return None

    def _check_connection(self):
        if self.socket.state() == QAbstractSocket.SocketState.UnconnectedState:
            self.socket.open(self.url)

    def request_interrupt(self):
        if self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            self.socket.sendTextMessage(json.dumps({'event': 'interrupt_request'}))
        else:
            self.stop_audio()

    def _on_connected(self):
        self.guard.reset()
        log.info("Connected to FullAwake PC gateway (%s)", self.url.toString())
        target = self._get_target()
        if target and hasattr(target, "show_bubble"):
            target.show_bubble("小满已经连接到电脑桌面啦 ~", duration_ms=2500)

    def _on_disconnected(self):
        log.warning("Disconnected from FullAwake PC gateway")
        self.stop_audio()
        self.guard.reset()
        self.smoother.reset()
        target = self._get_target()
        if target and hasattr(target, "request_link_idle"):
            target.request_link_idle()

    def _on_message(self, raw: str):
        try:
            event = json.loads(raw)
        except Exception:
            return

        if not isinstance(event, dict):
            return
        ev_type = event.get("event")
        if ev_type == "ping":
            self.guard.beat(time.monotonic())
            self.socket.sendTextMessage(json.dumps({"event": "pong", "nonce": event.get("nonce")}))
        elif ev_type == "action":
            self._handle_action(event)
        elif ev_type == "heart_mood":
            mood = event.get("mood")
            if isinstance(mood, str) and (mood == "calm" or mood in MOOD_IDLE_CLIPS):
                self.heart_mood = mood
        elif ev_type == "playback_audio":
            self._handle_playback_audio(event)
        elif ev_type == "playback_song" and event.get("utteranceId") == self.current_utterance:
            task = event.get("task")
            if isinstance(task, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", task) and not self.buffer and not self.song_playing:
                self.song_playing = True
                self.player.setSource(QUrl(f"http://127.0.0.1:{self.port}/v1/voice/sing/download/{task}?format=mp3"))
                self.player.play()
        elif ev_type == "interrupt":
            self._handle_interrupt(event)
        elif ev_type == "playback_duck":
            utt = event.get("utteranceId")
            vol = event.get("volume")
            if (not utt or utt == self.current_utterance) and isinstance(vol, (int, float)):
                self.audio.setVolume(max(0.0, min(1.0, float(vol))))
        elif (ev_type == "playback_clock" and event.get("mode") == "idle"
              and event.get("utteranceId") in ("", self.current_utterance)):
            self.stop_audio()

    def _handle_interrupt(self, event: dict):
        utterance = event.get("utteranceId")
        interrupt_id = event.get("interruptId")
        if not utterance or not interrupt_id or self.current_utterance not in ("", utterance):
            return
        position = self.player.position() if self.current_utterance else 0
        self.stop_audio()
        self.socket.sendTextMessage(json.dumps({
            "event": "interrupt_ack", "interruptId": interrupt_id,
            "utteranceId": utterance, "elapsedAudioMs": position,
        }))
        target = self._get_target()
        if target and hasattr(target, "request_link_anim"):
            if self.smoother.request(Priority.HIGH_ALERT, time.monotonic(), 0):
                target.request_link_anim("东张西望", interrupt=True)

    def _handle_action(self, event: dict):
        text = event.get("text", "")
        intent = event.get("intent", "talk")
        duration_ms = event.get("durationMs", 3500)
        has_audio = event.get("hasAudio") is True or bool(event.get("audioBase64"))
        if (self.buffer is not None or self.song_playing) and event.get("utteranceId") != self.current_utterance:
            if event.get("bridgeText") and event.get("utteranceId"):
                self._send_clock("idle", outcome="discarded", utterance=event["utteranceId"])
            if intent in ("shock", "surprised"):
                self._show_intent(intent, Priority.HIGH_ALERT, duration_ms)
            return
        self.current_utterance = event.get("utteranceId", "")
        self.bridged = event.get("voiceBridge") is True
        self.pending_text = text if has_audio else ""

        target = self._get_target()
        if not target:
            if event.get("bridgeText"):
                self._send_clock("idle", outcome="discarded")
            return

        shown = False
        if text and not has_audio and hasattr(target, "show_bubble"):
            shown = target.show_bubble(text, duration_ms=duration_ms)
        if event.get("bridgeText"):
            self._send_clock("idle", outcome="text_delivered" if shown is True else "discarded")

        priority = (Priority.HIGH_ALERT if intent in ("shock", "surprised") else
                    Priority.SPEAKING if has_audio else Priority.EMOTION)
        self._show_intent(intent, priority, duration_ms)

        if event.get("audioBase64"):
            self._handle_playback_audio(event)

    def _show_intent(self, intent, priority, duration_ms):
        target = self._get_target()
        animation = INTENT_ANIM_CANDIDATES.get(intent, ["悠闲哼歌"])[0]
        if hasattr(target, "request_link_anim") and self.smoother.request(priority, time.monotonic(), duration_ms / 1000):
            if priority >= Priority.SPEAKING:
                target.request_link_anim(animation, interrupt=True)
            else:
                target.request_link_anim(animation)

    def _handle_playback_audio(self, event: dict):
        if event.get("utteranceId") != self.current_utterance:
            return
        audio_b64 = event.get("audioBase64", "")
        if not audio_b64:
            return
        try:
            wav_bytes = base64.b64decode(audio_b64, validate=True)
            self.envelope = PlaybackClock.from_wire(event) if "visemes" in event else (
                PlaybackClock.from_wav(wav_bytes) if event.get("mediaType") != "audio/mpeg" else None)
            self.buffer = QBuffer(self)
            self.buffer.setData(QByteArray(wav_bytes))
            self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            extension = "mp3" if event.get("mediaType") == "audio/mpeg" else "wav"
            self.audio.setVolume(1.0)
            self.player.setSourceDevice(self.buffer, QUrl(f"memory.{extension}"))
            self.player.play()
        except Exception as exc:
            log.error("Failed to decode and play audio: %s", exc)
            if self.bridged:
                self._send_clock("idle", outcome="discarded")
            self.stop_audio()

    def _on_media_status(self, status):
        if (self.buffer is not None or self.song_playing) and status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            played = status == QMediaPlayer.MediaStatus.EndOfMedia and self.buffer is not None
            self._send_clock("idle", outcome=("played" if played else "discarded") if self.bridged else None)
            self.stop_audio()

    def stop_audio(self):
        self.current_utterance = ""
        self.bridged = False
        self.envelope = None
        self.pending_text = self.subtitle = ""
        self._set_mouth(0)
        buffer, self.buffer = self.buffer, None
        song, self.song_playing = self.song_playing, False
        if buffer is not None or song:
            self.player.stop()
            self.player.setSource(QUrl())
        self.audio.setVolume(1.0)
        if buffer is not None:
            buffer.deleteLater()

    def _set_mouth(self, level):
        self.current_mouth = level
        if self.mouth_overlay is not None and not isValid(self.mouth_overlay):
            self.mouth_overlay = None
        if not level:
            if self.mouth_overlay is not None:
                self.mouth_overlay.hide()
            return
        target = self._get_target()
        # Animated assets are flat clips. Overlay a small mouth on the active
        # renderer without changing its clip or input hit-testing.
        region = None
        if not isinstance(target, QWidget) and getattr(target, "sprite", None) is not None:
            sprite = target.sprite
            host = target.host_for_sprite(sprite)
            region = sprite.body_rect()
            target = host.overlay if host is not None else None
        if not isinstance(target, QWidget) or not isValid(target):
            if self.mouth_overlay is not None:
                self.mouth_overlay.hide()
            return
        if self.mouth_overlay is None or self.mouth_overlay.parent() is not target:
            if self.mouth_overlay is not None:
                self.mouth_overlay.deleteLater()
            self.mouth_overlay = QLabel(target)
            self.mouth_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if region is None:
            region = target.character_local_region() if hasattr(target, "character_local_region") else target.rect()
        width = max(3, round(region.width() * (.035 if level == 1 else .055)))
        height = max(2, round(width * (.45 if level == 1 else .75)))
        if self.mouth_overlay.height() != height or not self.mouth_overlay.styleSheet():
            self.mouth_overlay.setStyleSheet(f"background:#62343c;border-radius:{height // 2}px")
        self.mouth_overlay.setGeometry(region.center().x() - width // 2, region.y() + round(region.height() * .40), width, height)
        self.mouth_overlay.show()
        self.mouth_overlay.raise_()

    def _on_tick(self):
        now = time.monotonic()
        if self.guard.expired(now):
            self.stop_audio()
            self.socket.abort()
            return
        if self.buffer is None and not self.song_playing and self.smoother.expire(now):
            target = self._get_target()
            clip = MOOD_IDLE_CLIPS.get(self.heart_mood)
            if clip and hasattr(target, "request_link_anim"):
                target.request_link_anim(clip)
            elif hasattr(target, "request_link_idle"):
                target.request_link_idle()
        if (self.buffer is not None or self.song_playing) and self.player.isPlaying():
            pos = self.player.position()
            frame = self.envelope.at(pos) if self.envelope else None
            self._set_mouth(frame.mouth if frame else 0)
            cue, end = self.envelope.subtitle_at(pos) if self.envelope else ("", self.player.duration())
            subtitle = cue or self.pending_text
            if subtitle and subtitle != self.subtitle:
                target = self._get_target()
                if target and hasattr(target, "show_bubble") and target.show_bubble(
                        subtitle, duration_ms=max(1, end - pos), reserve=False):
                    self.subtitle = subtitle
            self._send_clock("speaking", pos)
        else:
            self._set_mouth(0)

    def _send_clock(self, mode: str, pos: int = 0, *, outcome=None, utterance=None):
        utterance = utterance or self.current_utterance
        if utterance and self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            payload = {
                "event": "playback_clock",
                "mode": mode,
                "utteranceId": utterance,
                "positionMs": pos,
                "mouth": self.current_mouth,
            }
            if outcome is not None:
                payload["outcome"] = outcome
            self.socket.sendTextMessage(json.dumps(payload))

    def observe_interaction(self, action_type: str, details: dict | None = None):
        """回流用户交互观察到手机小满心智"""
        if self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            msg = {
                "event": "observe",
                "source": "desktop_pet",
                "action": action_type,
                "timestamp": time.time(),
                "details": details or {},
            }
            self.socket.sendTextMessage(json.dumps(msg))

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
from io import BytesIO
import json
import logging
import math
import os
import time
import wave
from array import array

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, QUrl, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtNetwork import QAbstractSocket

log = logging.getLogger("fullawake-bridge")


class MouthEnvelope:
    def __init__(self, data: bytes):
        with wave.open(BytesIO(data), "rb") as wav:
            self.samples = array("h", wav.readframes(wav.getnframes()))
            self.samples_per_ms = wav.getframerate() * wav.getnchannels() / 1000.0
        self.level = 0
        self.rms = 0.0

    def at(self, position_ms: int) -> int:
        start = int(max(0, position_ms - 16) * self.samples_per_ms)
        end = int((position_ms + 16) * self.samples_per_ms)
        samples = self.samples[start:end]
        if not samples:
            return 0
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
        self.rms += (rms - self.rms) * (0.55 if rms > self.rms else 0.24)
        low = 0.018 if self.level == 0 else 0.012
        high = 0.055 if self.level == 2 else 0.08
        self.level = 2 if self.rms >= high else 1 if self.rms >= low else 0
        return self.level


# Intent 到角色动作的映射表（按 shenshen 实际具备的 100+ webm 动画名映射）
INTENT_ANIM_CANDIDATES = {
    "think": ["深度思考碎碎念", "写代码"],
    "nod": ["点击回应-元气挥手", "点击回应-开心跃动"],
    "happy": ["点击回应-开心跃动", "可爱宅舞", "轻快摇摆舞"],
    "talk": ["悠闲哼歌", "深度思考碎碎念"],
    "curious": ["东张西望"],
    "surprised": ["被吓一跳", "点击回应-害羞惊讶"],
    "sleep": ["原地小憩沉眠", "打瞌睡被惊醒"],
    "eat": ["吃Token", "吃冰淇淋融化"],
    "praise": ["放烟花", "凭空生花", "点击回应-开心跃动"],
}


class FullAwakePetBridge(QObject):
    def __init__(self, app_shell, port: int = 8765):
        super().__init__()
        self.shell = app_shell
        self.port = int(os.environ.get("FULLAWAKE_PORT", port))
        self.url = QUrl(f"ws://127.0.0.1:{self.port}/ws/pet?player=1")

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
        self.envelope: MouthEnvelope | None = None
        self.buffer: QBuffer | None = None
        self.current_mouth = 0

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

    def _on_connected(self):
        log.info("Connected to FullAwake PC gateway (%s)", self.url.toString())
        target = self._get_target()
        if target and hasattr(target, "show_bubble"):
            target.show_bubble("小满已经连接到电脑桌面啦 ~", duration_ms=2500)

    def _on_disconnected(self):
        log.warning("Disconnected from FullAwake PC gateway")
        self.stop_audio()
        target = self._get_target()
        if target and hasattr(target, "request_link_idle"):
            target.request_link_idle()

    def _on_message(self, raw: str):
        try:
            event = json.loads(raw)
        except Exception:
            return

        ev_type = event.get("event")
        if ev_type == "action":
            self._handle_action(event)
        elif ev_type == "playback_audio":
            self._handle_playback_audio(event)
        elif ev_type == "playback_clock" and event.get("mode") == "idle":
            self.stop_audio()
            target = self._get_target()
            if target and hasattr(target, "request_link_idle"):
                target.request_link_idle()

    def _handle_action(self, event: dict):
        text = event.get("text", "")
        intent = event.get("intent", "talk")
        duration_ms = event.get("durationMs", 3500)
        self.current_utterance = event.get("utteranceId", "")

        target = self._get_target()
        if not target:
            return

        if text and hasattr(target, "show_bubble"):
            target.show_bubble(text, duration_ms=duration_ms)

        # 触发匹配的动作
        candidates = INTENT_ANIM_CANDIDATES.get(intent, ["悠闲哼歌"])
        anim_to_play = candidates[0] if candidates else None
        if anim_to_play and hasattr(target, "request_link_anim"):
            target.request_link_anim(anim_to_play)

        if event.get("audioBase64"):
            self._handle_playback_audio(event)

    def _handle_playback_audio(self, event: dict):
        if event.get("utteranceId") != self.current_utterance:
            return
        audio_b64 = event.get("audioBase64", "")
        if not audio_b64:
            return
        try:
            wav_bytes = base64.b64decode(audio_b64, validate=True)
            self.envelope = MouthEnvelope(wav_bytes)
            self.buffer = QBuffer(self)
            self.buffer.setData(QByteArray(wav_bytes))
            self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self.player.setSourceDevice(self.buffer, QUrl("memory.wav"))
            self.player.play()
        except Exception as exc:
            log.error("Failed to decode and play audio: %s", exc)
            self.stop_audio()

    def _on_media_status(self, status):
        if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            self._send_clock("idle")
            self.stop_audio()
            target = self._get_target()
            if target and hasattr(target, "request_link_idle"):
                target.request_link_idle()

    def stop_audio(self):
        self.envelope = None
        self.current_mouth = 0
        if self.buffer is not None:
            self.player.stop()
            self.player.setSource(QUrl())
            self.buffer.deleteLater()
            self.buffer = None

    def _on_tick(self):
        if self.envelope and self.player.isPlaying():
            pos = self.player.position()
            self.current_mouth = self.envelope.at(pos)
            self._send_clock("speaking", pos)
        else:
            self.current_mouth = 0

    def _send_clock(self, mode: str, pos: int = 0):
        if self.current_utterance and self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            payload = {
                "event": "playback_clock",
                "mode": mode,
                "utteranceId": self.current_utterance,
                "positionMs": pos,
                "mouth": self.current_mouth,
            }
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

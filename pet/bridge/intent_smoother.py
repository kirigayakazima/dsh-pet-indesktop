from enum import IntEnum


class Priority(IntEnum):
    IDLE = 0
    EMOTION = 1
    SPEAKING = 2
    HIGH_ALERT = 3


class AnimationSmoother:
    def __init__(self):
        self.reset()

    def reset(self):
        self.priority = Priority.IDLE
        self.until = 0.0

    def request(self, priority, now, duration):
        if now < self.until and priority <= self.priority:
            return False
        self.priority = priority
        self.until = now + max(4 if priority in (Priority.EMOTION, Priority.HIGH_ALERT) else 0, duration)
        return True

    def expire(self, now):
        if self.priority != Priority.IDLE and now >= self.until:
            self.reset()
            return True
        return False

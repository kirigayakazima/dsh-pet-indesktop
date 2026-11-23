class CrashGuard:
    """A legacy gateway never arms the application-level watchdog."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.last_ping = None

    def beat(self, now):
        self.last_ping = now

    def expired(self, now):
        return self.last_ping is not None and now - self.last_ping >= 15

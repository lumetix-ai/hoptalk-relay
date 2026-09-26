from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class ManualClock:
    """Wall time that moves only when a test moves it, for services that take `now`."""

    current_time: datetime

    def now(self) -> datetime:
        return self.current_time

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> datetime:
        self.current_time += timedelta(seconds=seconds, minutes=minutes, hours=hours)
        return self.current_time

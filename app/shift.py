import json
from dataclasses import dataclass, field
from datetime import datetime

from .clock import instant


@dataclass(frozen=True)
class Shift:
    name: str
    start: datetime
    end: datetime
    stations: dict
    trucks: dict
    opening_road: int
    min_travel_minutes: int
    next_shift: dict = field(default=None)


def load_shift(raw):
    data = json.loads(raw)
    return Shift(data['name'], instant(data['start']), instant(data['end']),
                 data['stations'], data['trucks'], data['opening_road'],
                 data['min_travel_minutes'], data.get('next_shift'))

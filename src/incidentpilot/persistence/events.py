"""The investigation event log (spec §7.3, §12.3). Run inside owner_transaction.

Each event takes the next sequence number from its investigation's row, under that
row's lock, so concurrent appends never collide or leave gaps. Payloads are sanitized
here, at the one place every event passes through (§15.1).
"""

import re
from collections.abc import Mapping

from psycopg.types.json import Jsonb

from incidentpilot.domain.enums import EventType
from incidentpilot.domain.ids import InvestigationId
from incidentpilot.persistence.database import Connection
from incidentpilot.policies.redaction import sanitize

PAYLOAD_VERSION = 1
MAX_PAYLOAD_TEXT = 2_000
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NEXT_SEQ = (
    "UPDATE investigations SET next_event_seq = next_event_seq + 1"
    " WHERE id = %s RETURNING next_event_seq - 1"
)

type Payload = Mapping[str, object]


class EventError(Exception):
    """An event can't be appended as given."""


def clean_payload(value: object) -> object:
    """Sanitize every string, and refuse keys an event schema wouldn't use."""
    match value:
        case str():
            return sanitize(value, limit=MAX_PAYLOAD_TEXT).text
        case Mapping():
            cleaned = {}
            for key, item in value.items():
                if not isinstance(key, str) or not _KEY.fullmatch(key):
                    raise EventError(f"event payload key {key!r} isn't snake_case")
                cleaned[key] = clean_payload(item)
            return cleaned
        case list() | tuple():
            return [clean_payload(item) for item in value]
        case None | bool() | int() | float():
            return value
        case _:
            raise EventError(f"event payloads can't hold {type(value).__name__}")


async def append_event(
    connection: Connection,
    investigation_id: InvestigationId,
    event_type: EventType,
    payload: Payload,
) -> int:
    """Append one event and return its sequence number."""
    cleaned = clean_payload(payload)
    row = await (await connection.execute(_NEXT_SEQ, (investigation_id,))).fetchone()
    if row is None:
        raise EventError(f"investigation {investigation_id} doesn't exist")
    (seq,) = row
    await connection.execute(
        """
        INSERT INTO run_events
            (owner_id, investigation_id, seq, event_type, payload_version, payload)
        VALUES (current_owner_id(), %s, %s, %s, %s, %s)
        """,
        (investigation_id, seq, event_type, PAYLOAD_VERSION, Jsonb(cleaned)),
    )
    return int(seq)

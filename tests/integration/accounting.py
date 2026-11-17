"""The investigation's ledger counters, checked against the rows they summarize (E18)."""

from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.fence import Lease

COUNTERS = (
    "model_requests_reserved",
    "model_requests_dispatched",
    "model_requests_settled",
    "model_requests_unknown",
    "cost_reserved_micro_usd",
    "cost_known_micro_usd",
    "cost_unknown_micro_usd",
    "input_tokens_actual",
    "output_tokens_actual",
)
# E18: what each counter means, recomputed from the rows.
RECOUNT = """
SELECT
    count(*) FILTER (WHERE NOT (state = 'FAILED' AND dispatched_at IS NULL)),
    count(*) FILTER (WHERE dispatched_at IS NOT NULL),
    count(*) FILTER (WHERE state = 'SETTLED'),
    count(*) FILTER (WHERE state = 'UNKNOWN'),
    coalesce(sum(reserved_cost_micro_usd)
             FILTER (WHERE state IN ('RESERVED', 'DISPATCHED')), 0),
    coalesce(sum(actual_cost_micro_usd) FILTER (WHERE state = 'SETTLED'), 0),
    coalesce(sum(reserved_cost_micro_usd) FILTER (
        WHERE state = 'UNKNOWN' OR (state = 'FAILED' AND dispatched_at IS NOT NULL)
    ), 0),
    coalesce(sum(input_tokens), 0),
    coalesce(sum(output_tokens), 0)
FROM model_calls WHERE investigation_id = %s
"""


async def counters(connection: Connection, lease: Lease) -> dict[str, int]:
    """The investigation's counters, after checking they match its rows."""
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            f"SELECT {', '.join(COUNTERS)} FROM investigations WHERE id = %s",  # noqa: S608
            (lease.investigation_id,),
        )
        stored = await cursor.fetchone()
        recounted = await (await connection.execute(RECOUNT, (lease.investigation_id,))).fetchone()
    assert stored is not None
    assert recounted is not None
    assert tuple(stored) == tuple(recounted)
    return {name: value for name, value in zip(COUNTERS, stored, strict=True) if value}

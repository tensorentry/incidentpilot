"""Identities from spec §3.2. Distinct types stop one ID being passed as another."""

from typing import NewType
from uuid import UUID

OwnerId = NewType("OwnerId", UUID)
IncidentId = NewType("IncidentId", UUID)
InvestigationId = NewType("InvestigationId", UUID)
AttemptId = NewType("AttemptId", UUID)
EvidenceId = NewType("EvidenceId", UUID)
InvocationId = NewType("InvocationId", UUID)
ModelCallId = NewType("ModelCallId", UUID)
SnapshotId = NewType("SnapshotId", str)
LeaseEpoch = NewType("LeaseEpoch", int)

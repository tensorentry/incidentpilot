from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from incidentpilot.domain.ids import InvestigationId, LeaseEpoch, SnapshotId


class Sample(BaseModel):
    investigation_id: InvestigationId
    snapshot_id: SnapshotId
    lease_epoch: LeaseEpoch


def test_identity_types_validate_as_their_base_types() -> None:
    raw = uuid4()

    sample = Sample.model_validate(
        {"investigation_id": str(raw), "snapshot_id": "snap-1", "lease_epoch": 7}
    )

    assert sample.investigation_id == raw
    assert isinstance(sample.investigation_id, UUID)


def test_malformed_uuid_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Sample.model_validate(
            {"investigation_id": "not-a-uuid", "snapshot_id": "snap-1", "lease_epoch": 7}
        )

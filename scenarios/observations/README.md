# Observation bundles

One `ObservationBundle` per snapshot (`src/incidentpilot/adapters/fixture_bundle.py`). This is the only part of `scenarios/` that workers mount, and everything here may reach the model.

File and snapshot names must be opaque: nothing here may hint at a case's cause.

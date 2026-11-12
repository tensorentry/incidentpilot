"""Name types shared by reports, measurements and snapshots."""

from typing import Annotated

from pydantic import Field

LocalId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
ServiceName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Category = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
MetricName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]

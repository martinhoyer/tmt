# mypy: disable-error-code="assignment"
from __future__ import annotations

import pydantic

if pydantic.__version__.startswith('1.'):
    from pydantic import (
        BaseModel,
        Extra,
        HttpUrl,
        ValidationError,
        Field,
    )
else:
    from pydantic.v1 import (
        BaseModel,
        Extra,
        HttpUrl,
        ValidationError,
        Field,
    )

__all__ = [
    "BaseModel",
    "Field",
    "Extra",
    "HttpUrl",
    "ValidationError",
]

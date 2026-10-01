import inspect
import sys
from typing import Generic, Optional, TypeVar


from pydantic import BaseModel, Field, model_serializer
from uuid import UUID


T = TypeVar("T")

class DetailField(BaseModel):
    msg: str
    correlationId: UUID
    code: Optional[str] = Field(
        default=None, description="A machine-readable reason for an error (e.g. `same_person`); "
                                  "left out when there is none.")

    @model_serializer(mode="wrap")
    def _without_empty_code(self, handler):
        # an envelope without a code serializes exactly as it did before `code` existed
        out = handler(self)
        if out.get("code") is None:
            out.pop("code", None)
        return out


class DefaultResponse(BaseModel, Generic[T]):
    status_code: int
    details: DetailField
    data: T


class ErrorResponse(BaseModel):
    status_code: int
    details: DetailField
    # always empty: warning_/error_response send `data: []`
    data: list = Field(default_factory=list)


class ErrorValidObject(DetailField):
    field: str




_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]


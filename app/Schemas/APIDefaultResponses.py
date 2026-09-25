import inspect
import sys
from typing import TypeVar, Generic


from pydantic import BaseModel, Field
from uuid import UUID


T = TypeVar("T")

class DetailField(BaseModel):
    msg: str
    correlationId: UUID


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


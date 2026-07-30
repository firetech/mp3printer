# pyright: strict

from typing import Literal, TypeAlias

import pydantic as pyd

#############
# Responses #
#############


class AddressResponse(pyd.BaseModel):
    type: Literal["address"] = "address"
    address: str | None


class ListContentResponseEntry(pyd.BaseModel):
    id: str
    title: str
    nick: str | None
    address: str | None
    prio: int

    model_config = {"extra": "ignore"}


class ListContentResponse(pyd.BaseModel):
    type: Literal["list"] = "list"
    position: float
    list: list[ListContentResponseEntry]


class ListFallbackResponse(pyd.BaseModel):
    type: Literal["fallback"] = "fallback"
    description: str


ListResponse: TypeAlias = ListContentResponse | ListFallbackResponse


class ProgressResponse(pyd.BaseModel):
    type: Literal["progress"] = "progress"
    position: float


Response: TypeAlias = AddressResponse | ListResponse | ProgressResponse

############
# Requests #
############


class CancelRequest(pyd.BaseModel):
    type: Literal["cancel"] = "cancel"
    id: str


Request: TypeAlias = CancelRequest

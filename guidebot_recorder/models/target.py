"""Target — namiar na element (unia dyskryminowana po `strategy`), §4.3 specu.

Jedno źródło prawdy dla: wyjścia Reasonera, pola `scope`, `cachedAction`.
Locator Playwrighta budowany jest z tych pól wyłącznie w zaufanym kodzie.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # rekurencyjne zawężenie do poddrzewa przodka
    scope: "Target | None" = None


class RoleTarget(_Base):
    strategy: Literal["role"] = "role"
    role: str
    name: str
    exact: bool = True
    nth: int | None = None


class TextTarget(_Base):
    strategy: Literal["text"] = "text"
    text: str
    exact: bool = True


class LabelTarget(_Base):
    strategy: Literal["label"] = "label"
    label: str
    exact: bool = True


class TestidTarget(_Base):
    strategy: Literal["testid"] = "testid"
    testid: str


Target = Annotated[
    Union[RoleTarget, TextTarget, LabelTarget, TestidTarget],
    Field(discriminator="strategy"),
]

for _m in (RoleTarget, TextTarget, LabelTarget, TestidTarget):
    _m.model_rebuild()

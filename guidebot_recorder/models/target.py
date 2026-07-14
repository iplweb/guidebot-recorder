"""Target — a reference to an element (union discriminated by `strategy`), spec §4.3.

Single source of truth for: the Reasoner's output, the `scope` field, and `cachedAction`.
The Playwright locator is built from these fields exclusively in trusted code.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # recursive narrowing to an ancestor's subtree
    scope: Target | None = None


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
    RoleTarget | TextTarget | LabelTarget | TestidTarget,
    Field(discriminator="strategy"),
]

for _m in (RoleTarget, TextTarget, LabelTarget, TestidTarget):
    _m.model_rebuild()

"""Identity — a frozen element identity, independent of the locator (§4.3).

Guards against page drift: render compares the identity of the matched element
against the frozen one. `role`/`name` are NOT criteria (the locator is built from
them — the comparison would be tautological).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: str
    testid: str | None = None
    href: str | None = None
    ancestry_digest: str
    identity_version: int = 1

    def matches(self, other: Identity) -> bool:
        """Equality: all present fields equal AND `identity_version` equal."""
        return (
            self.identity_version == other.identity_version
            and self.tag == other.tag
            and self.testid == other.testid
            and self.href == other.href
            and self.ancestry_digest == other.ancestry_digest
        )

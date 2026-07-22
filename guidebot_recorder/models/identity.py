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
    #: Digest of the element's DOM path — a drift signal for `compile`, not a
    #: criterion of identity. Deliberately outside `matches()`: the path changes
    #: on any new element among the ancestors, so a hard comparison would break
    #: working renders. The cost of a false alarm decides where the signal may
    #: live — in `compile` it is one redundant resolution, in `render`/`guide` a
    #: stopped film. `None` in sidecars predating this change, which keeps every
    #: existing one valid: no recompile.
    dom_path_digest: str | None = None
    identity_version: int = 1

    def matches(self, other: Identity) -> bool:
        """Equality: all present fields equal AND `identity_version` equal.

        `dom_path_digest` is **not** compared — see the field comment.
        """
        return (
            self.identity_version == other.identity_version
            and self.tag == other.tag
            and self.testid == other.testid
            and self.href == other.href
            and self.ancestry_digest == other.ancestry_digest
        )

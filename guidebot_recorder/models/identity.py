"""Identity — zamrożona tożsamość elementu, niezależna od locatora (§4.3).

Chroni przed dryfem strony: render porównuje tożsamość trafionego elementu z
zamrożoną. `role`/`name` NIE są kryterium (locator jest z nich budowany —
porównanie byłoby tautologiczne).
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

    def matches(self, other: "Identity") -> bool:
        """Równość: wszystkie obecne pola równe ORAZ `identity_version` równa."""
        return (
            self.identity_version == other.identity_version
            and self.tag == other.tag
            and self.testid == other.testid
            and self.href == other.href
            and self.ancestry_digest == other.ancestry_digest
        )

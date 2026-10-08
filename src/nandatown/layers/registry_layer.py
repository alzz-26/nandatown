"""Registry layer: the town's internal index.

Agents inside the sandbox do not belong on the main NANDA Index; the
town keeps its own separate index. Cards are published with a signature,
verification is checked at publish time, and lookups return verified
cards ranked by trust.
"""

from __future__ import annotations

import copy
from typing import Any

from . import register
from ..records import fingerprint


@register("registry", "index.v1")
class TownIndex:
    """Publish signed agent cards, look peers up by capability."""

    def __init__(self, engine):
        self.engine = engine
        self.cards: dict[str, dict[str, Any]] = {}

    def publish(self, publisher: str, card: dict[str, Any],
                signature: str) -> bool:
        auth = self.engine.layers["auth"]
        verified = auth.verify(card["name"], card, signature,
                               subject=card["name"])
        entry = {"card": card, "verified": verified, "publisher": publisher}
        self.cards[card["name"]] = entry
        if verified:
            self.engine.emit("town", "card_registered", card["name"],
                             {"capabilities": card["capabilities"],
                              "verified": True})
        else:
            self.engine.emit("town", "card_unverified", card["name"],
                             {"capabilities": card["capabilities"],
                              "publisher": publisher})
        return verified

    def lookup(self, capability: str,
               include_unverified: bool = False) -> list[dict[str, Any]]:
        trust = self.engine.layers["trust"]
        hits = []
        for entry in self.cards.values():
            if capability not in entry["card"]["capabilities"]:
                continue
            if not entry["verified"] and not include_unverified:
                continue
            hits.append(entry["card"])
        return sorted(hits, key=lambda c: (-trust.score(c["name"]), c["name"]))

    def names_with(self, capability: str) -> list[str]:
        return [c["name"] for c in self.lookup(capability)]


@register("registry", "index.owned.v1")
class OwnedIndex(TownIndex):
    """A listing belongs to its name's key: forged cards cannot replace it.

    index.v1 stores whatever card arrives under its name, even one whose
    signature fails, so anyone can erase a verified listing by publishing
    a forged card in its name. Here a card that fails verification never
    replaces a verified listing; only a card signed by the name's own key
    can update it, and only a request signed by that key can withdraw it.
    An unverified card for an unclaimed name is still stored unverified,
    as in index.v1, and does not claim the name.

    Cards are copied on the way in and on the way out, so a caller that
    keeps or edits a card dict cannot change a stored listing without a
    signed update. Each name has a generation that moves on every change
    to its verified listing; a withdrawal signs the current generation,
    so it removes that listing once and is useless afterwards.

    A card carries a version, an integer of at least 1; a card without
    one is version 1. The registry remembers the highest version it has
    accepted for each name, also after a withdrawal, and refuses any card
    at or below it. So a signed card is valid once: replaying an older
    one cannot roll an update back or bring a withdrawn listing back, and
    an owner updates or returns by publishing a higher version.
    """

    def __init__(self, engine):
        super().__init__(engine)
        self.generations: dict[str, int] = {}
        self.versions: dict[str, int] = {}

    def _changed(self, name: str) -> None:
        self.generations[name] = self.generations.get(name, 0) + 1

    def lookup(self, capability: str,
               include_unverified: bool = False) -> list[dict[str, Any]]:
        return copy.deepcopy(super().lookup(capability, include_unverified))

    def publish(self, publisher: str, card: dict[str, Any],
                signature: str) -> bool:
        card = copy.deepcopy(card)
        name = card["name"]
        version = card.get("version", 1)
        if type(version) is not int or version < 1:
            return self._refuse(card, publisher,
                                "version must be an integer of at least 1")
        highest = self.versions.get(name)
        stale = f"version {version} is not above {highest}"
        current = self.cards.get(name)
        if current is None or not current["verified"]:
            if highest is not None and version <= highest:
                return self._refuse(card, publisher, stale)
            verified = super().publish(publisher, card, signature)
            if verified:
                self._accepted(name, version)
            return verified
        auth = self.engine.layers["auth"]
        if not auth.verify(name, card, signature, subject=name):
            return self._refuse(card, publisher,
                                "not signed by the listing's key")
        if version <= highest:
            return self._refuse(card, publisher, stale)
        self.cards[name] = {"card": card, "verified": True,
                            "publisher": publisher}
        self._accepted(name, version)
        self.engine.emit("town", "card_registered", name,
                         {"capabilities": card["capabilities"],
                          "verified": True, "update": True,
                          "version": version})
        return True

    def _accepted(self, name: str, version: int) -> None:
        self.versions[name] = version
        self._changed(name)

    def _refuse(self, card: dict[str, Any], publisher: str,
                reason: str) -> bool:
        self.engine.emit("town", "card_publish_refused", card["name"],
                         {"capabilities": card["capabilities"],
                          "publisher": publisher, "reason": reason})
        return False

    def withdrawal(self, name: str, card: dict[str, Any]) -> dict[str, Any]:
        """The payload an owner signs to withdraw this exact listing once.
        It binds the card's fingerprint and the name's current generation,
        so a captured withdrawal cannot remove a later listing, even one
        identical to the card it was signed for (A, then B, then A)."""
        return {"action": "withdraw", "name": name,
                "card": fingerprint(card),
                "generation": self.generations.get(name, 0)}

    def withdraw(self, requester: str, name: str, signature: str) -> bool:
        current = self.cards.get(name)
        if current is None or not current["verified"]:
            self.engine.emit("town", "card_withdraw_refused", name,
                             {"requester": requester,
                              "reason": "no verified listing"})
            return False
        auth = self.engine.layers["auth"]
        payload = self.withdrawal(name, current["card"])
        if not auth.verify(name, payload, signature, subject=name):
            self.engine.emit("town", "card_withdraw_refused", name,
                             {"requester": requester,
                              "reason": "not signed by the listing's key"})
            return False
        del self.cards[name]
        self._changed(name)
        self.engine.emit("town", "card_withdrawn", name,
                         {"requester": requester})
        return True

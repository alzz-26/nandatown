"""index.owned.v1: a forged card cannot erase or withdraw a listing."""

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.records import TownEvent
from nandatown.sim.engine import Engine
from nandatown.sim.runner import run_lab
from nandatown.sim.scenario import ScenarioSpec, load_bundled
from nandatown.sim.validators import VALIDATORS, Trace


def town(registry: str) -> Engine:
    spec = ScenarioSpec(name="registry-unit", agents=[],
                        layers={"registry": registry})
    return Engine(spec)


def signed_card(engine, name, signer, capabilities=("sell.widget",),
                facts=None):
    card = engine.layers["identity"].card(name, list(capabilities),
                                          facts or {})
    engine.layers["identity"].create(signer)
    return card, engine.layers["auth"].sign_as(signer, card)


def kinds(engine):
    return [e.kind for e in engine.events]


def test_default_index_lets_a_forged_card_erase_a_verified_listing():
    """The gap: index.v1 stores the forged card over the verified one."""
    engine = town("index.v1")
    registry = engine.layers["registry"]
    registry.publish("honest", *signed_card(engine, "honest", "honest"))
    assert registry.names_with("sell.widget") == ["honest"]

    registry.publish("rival", *signed_card(engine, "honest", "rival"))
    assert registry.names_with("sell.widget") == []


def test_forged_card_is_refused_and_the_listing_survives():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    card, signature = signed_card(engine, "honest", "honest")
    assert registry.publish("honest", card, signature)

    forged, forged_sig = signed_card(engine, "honest", "rival",
                                     facts={"note": "closed"})
    assert not registry.publish("rival", forged, forged_sig)
    assert registry.names_with("sell.widget") == ["honest"]
    assert registry.cards["honest"]["card"] == card
    refused = [e for e in engine.events if e.kind == "card_publish_refused"]
    assert [e.detail["publisher"] for e in refused] == ["rival"]


def test_owner_can_update_its_listing():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    registry.publish("honest", *signed_card(engine, "honest", "honest"))
    updated, signature = signed_card(engine, "honest", "honest",
                                     capabilities=("sell.gadget",))
    assert registry.publish("honest", updated, signature)
    assert registry.names_with("sell.widget") == []
    assert registry.names_with("sell.gadget") == ["honest"]
    assert engine.events[-1].detail["update"] is True


def test_unverified_squat_does_not_claim_the_name():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    assert not registry.publish("rival",
                                *signed_card(engine, "honest", "rival"))
    assert registry.names_with("sell.widget") == []
    assert registry.publish("honest",
                            *signed_card(engine, "honest", "honest"))
    assert registry.names_with("sell.widget") == ["honest"]


def test_owner_withdraws_and_a_forged_withdrawal_is_refused():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    auth = engine.layers["auth"]
    card, signature = signed_card(engine, "honest", "honest")
    registry.publish("honest", card, signature)
    request = registry.withdrawal("honest", card)
    engine.layers["identity"].create("rival")

    assert not registry.withdraw("rival", "honest",
                                 auth.sign_as("rival", request))
    assert registry.names_with("sell.widget") == ["honest"]

    assert registry.withdraw("honest", "honest",
                             auth.sign_as("honest", request))
    assert registry.names_with("sell.widget") == []
    assert kinds(engine)[-1] == "card_withdrawn"
    assert not registry.withdraw("honest", "honest",
                                 auth.sign_as("honest", request))


def test_a_replayed_withdrawal_cannot_remove_a_newer_listing():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    auth = engine.layers["auth"]
    first, signature = signed_card(engine, "honest", "honest")
    registry.publish("honest", first, signature)
    engine.layers["identity"].create("rival")
    old = auth.sign_as("honest", registry.withdrawal("honest", first))
    assert registry.withdraw("honest", "honest", old)

    second, signature = signed_card(engine, "honest", "honest",
                                    facts={"reopened": True})
    registry.publish("honest", second, signature)
    assert not registry.withdraw("rival", "honest", old)
    assert registry.names_with("sell.widget") == ["honest"]


def test_eviction_scenario_passes_and_verifies(tmp_path):
    bundle_dir, result = run_lab("registry_eviction", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "passed", stages
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    assert [e.subject for e in events
            if e.kind == "card_publish_refused"] == ["seller-honest"]
    paid = [e.detail["to"] for e in events if e.kind == "escrow_released"]
    assert paid == ["seller-honest"]


def test_unowned_control_fails_because_the_listing_was_erased(tmp_path):
    """Negative control: the same run on index.v1 fails at listing_intact,
    and the rival, not the honest seller, is paid."""
    bundle_dir, result = run_lab("registry_eviction_unowned", str(tmp_path))
    stages = {s.name: s.status for s in result.stages}
    assert result.verdict == "failed", stages
    assert stages["forgery_detected"] == "passed"
    assert stages["listing_intact"] == "failed"
    assert stages["honest_trade_completed"] == "failed"
    assert stages["ledger_conserved"] == "passed"
    assert verify_bundle(bundle_dir) == []
    events = load_bundle(bundle_dir)["events"]
    paid = [e.detail["to"] for e in events if e.kind == "escrow_released"]
    assert paid == ["seller-rival"]


def test_lookup_results_are_copies():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    registry.publish("honest", *signed_card(engine, "honest", "honest"))

    found = registry.lookup("sell.widget")[0]
    found["capabilities"].clear()
    found["facts"]["note"] = "tampered"

    assert registry.names_with("sell.widget") == ["honest"]
    assert registry.cards["honest"]["card"]["facts"] == {}


def test_published_card_is_snapshotted():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    card, signature = signed_card(engine, "honest", "honest")
    assert registry.publish("honest", card, signature)

    card["capabilities"].clear()
    card["facts"]["note"] = "tampered"

    assert registry.names_with("sell.widget") == ["honest"]
    assert registry.cards["honest"]["card"]["facts"] == {}


def test_a_withdrawal_is_refused_after_the_same_card_returns():
    """A, then B, then A again: the withdrawal signed for the first A
    belongs to an older generation and cannot remove the second A."""
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    auth = engine.layers["auth"]
    engine.layers["identity"].create("rival")
    first_a, signature_a = signed_card(engine, "honest", "honest")
    registry.publish("honest", first_a, signature_a)
    old = auth.sign_as("honest", registry.withdrawal("honest", first_a))

    b, signature_b = signed_card(engine, "honest", "honest",
                                 facts={"v": 2})
    registry.publish("honest", b, signature_b)
    registry.publish("honest", first_a, signature_a)

    assert not registry.withdraw("rival", "honest", old)
    assert registry.names_with("sell.widget") == ["honest"]


def test_a_fresh_withdrawal_works_after_the_same_card_returns():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    auth = engine.layers["auth"]
    a, signature_a = signed_card(engine, "honest", "honest")
    b, signature_b = signed_card(engine, "honest", "honest",
                                 facts={"v": 2})
    registry.publish("honest", a, signature_a)
    registry.publish("honest", b, signature_b)
    registry.publish("honest", a, signature_a)

    fresh = auth.sign_as("honest", registry.withdrawal("honest", a))
    assert registry.withdraw("honest", "honest", fresh)
    assert registry.names_with("sell.widget") == []


def test_a_used_withdrawal_cannot_remove_the_same_card_republished():
    engine = town("index.owned.v1")
    registry = engine.layers["registry"]
    auth = engine.layers["auth"]
    engine.layers["identity"].create("rival")
    card, signature = signed_card(engine, "honest", "honest")
    registry.publish("honest", card, signature)
    used = auth.sign_as("honest", registry.withdrawal("honest", card))
    assert registry.withdraw("honest", "honest", used)

    registry.publish("honest", card, signature)
    assert not registry.withdraw("rival", "honest", used)
    assert registry.names_with("sell.widget") == ["honest"]


def event(n, event_kind, subject, observer="town", **detail):
    return TownEvent(event_id=f"ev-{n}", run_id="run", at=float(n),
                     observer=observer, kind=event_kind, subject=subject,
                     detail=detail)


FORGERY = event(1, "card_unverified", "seller-honest",
                publisher="seller-rival", capabilities=["sell.widget"])


def judge(events):
    spec = load_bundled("registry_eviction")
    stages = VALIDATORS["registry_eviction"](spec, Trace(events))
    return {s.name: s.status for s in stages}


def test_listing_intact_needs_the_request_to_arrive():
    """A quote request sent after the forgery but refused on delivery
    does not show the honest seller was reachable."""
    sent = event(2, "message_sent", "m-1", observer="buyer-1",
                 to="seller-honest", kind="quote_request")
    delivered = event(3, "message_delivered", "m-1",
                      to="seller-honest", kind="quote_request")
    failed = event(4, "delivery_failed", "m-1", to="seller-honest",
                   reason="bad signature")

    assert judge([FORGERY, sent, delivered])["listing_intact"] == "passed"
    assert judge([FORGERY, sent, delivered, failed])["listing_intact"]         == "failed"
    assert judge([FORGERY, sent])["listing_intact"] == "failed"


def test_honest_trade_needs_an_order_placed_after_the_forgery():
    """A payment to the honest seller from before the forgery, or for an
    order the post-forgery workflow never placed, does not count."""
    early_pay = event(0, "escrow_released", "order-0",
                      to="seller-honest", cents=3780)
    order = event(2, "message_sent", "m-2", observer="buyer-1",
                  to="seller-honest", kind="purchase_order",
                  body={"order_id": "order-1", "quantity": 2})
    pay = event(3, "escrow_released", "order-1", to="seller-honest",
                cents=3780)
    other_pay = event(3, "escrow_released", "order-9",
                      to="seller-honest", cents=3780)

    stage = "honest_trade_completed"
    assert judge([early_pay, FORGERY])[stage] == "failed"
    assert judge([FORGERY, order, other_pay])[stage] == "failed"
    assert judge([FORGERY, order, pay])[stage] == "passed"

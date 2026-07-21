"""The SHIPPED default rules — what every robot does out of the box (t15).

``reachy/behavior/default_rules.toml`` is the LOWER of the two rule layers and
the only one that reaches a robot with **no operator-authored config at all**.
Its content is therefore a product decision with a blast radius of "every
deployed robot on its next upgrade", and this module is where that decision is
pinned.

What is asserted here, and why each assertion earns its place
=============================================================

**The shipped set itself.** Ids, count, and per-rule shape. Rule ids are a
PUBLIC INTERFACE — an operator overrides or tombstones a shipped rule *by id*
(``reachy.behavior.rules.merge_rules``) — so renaming one silently orphans
every box-local override of it. The id list is pinned so a rename is a
deliberate, visible edit rather than a side effect.

**Corroboration (c32).** No shipped rule may key on bare ``speech``: it read
true 45.8 % of the time in a quiet room with nobody speaking
(``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 2). A
:class:`reachy.behavior.rules.Rule` carries exactly ONE predicate — the schema
has no conjunction — so "corroborated" cannot mean ANDing two predicates. It
means keying on a field that is corroborated *in itself*, and every shipped
rule is asserted to do so.

**Wired, not inert.** Every shipped predicate field must be in
:data:`reachy.behavior.sense.FED_SENSE_FIELDS`. A field outside it validates
cleanly and then silently never fires — the exact silent no-op the senselog
discipline exists to prevent.

**Bounded.** Every shipped react rule targeting an unbounded-looping library
entry must carry ``duration_s``. Validation already refuses otherwise; the
assertion here pins the INTENT (these particular durations were chosen), not
just the schema.

**The quiet-room property.** Driven against a real
:class:`reachy.behavior.rule_engine.RuleEngine`, the shipped set admits NOTHING
on an at-rest snapshot — including the adversarial one where the daemon's
``speech_detected`` flag is true but nothing corroborates it. This is the
regression test for c32.

**Clean env.** The whole module runs ``offline`` (sockets blocked, every
service env var pointed nowhere) inside an isolated ``REACHY_STATE_DIR`` with
no overlay written and no ``REACHY_PAT_*`` override in sight — so it exercises
what a FRESH INSTALL does, never what the deployed box's
``reachy-runtime.service.d/pat-sense.conf`` drop-in makes it do.

**Malformed-overlay resilience.** Four ways an operator can fumble the overlay
(bad TOML, an unknown field, an unknown generator, a bounded-lifetime
violation), each asserted to leave the SHIPPED layer in force with exactly one
logged drop and no exception. Now that the shipped layer carries content this
is a live production path, not a hypothetical one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reachy.behavior import library as behavior_library
from reachy.behavior import rules as rules_mod
from reachy.behavior.orient import OrientParams
from reachy.behavior.reload_driver import ReloadDriver
from reachy.behavior.rule_engine import STAGE as RULE_STAGE
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import CORROBORATING_SENSE_FIELDS, load_rules, load_shipped_rules
from reachy.behavior.sense import EMPTY_SENSE, FED_SENSE_FIELDS, Sense
from reachy.cli._commands import behavior as behavior_cmd

pytestmark = pytest.mark.offline

SENSE_LOGGER = "reachy.sense"

#: The shipped rule ids, in file order. A PUBLIC INTERFACE (see the module
#: docstring) — changing this list is an interface break for every box-local
#: overlay that overrides or tombstones one of these ids.
SHIPPED_IDS = ["pat-acknowledge", "look-toward-sound", "greet-when-addressed"]


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """A FRESH INSTALL: an empty state dir, no overlay, no ``REACHY_PAT_*``.

    The deployed robot carries a ``reachy-runtime.service.d/pat-sense.conf``
    drop-in with five ``REACHY_PAT_*`` overrides; anything it sets would mask
    what the shipped defaults do on their own, so every one of them is stripped
    here rather than merely "probably absent in CI".
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path / "state"))
    for name in list(__import__("os").environ):
        if name.startswith("REACHY_PAT_"):
            monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Test doubles                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    """The engine's per-tick seam surface, recording what a rule did."""

    now: float = 0.0
    tick: int = 0
    sense: object = EMPTY_SENSE
    ownership: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        return {"ok": True, "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        return {"ok": True, "target": name}

    def active_names(self) -> set[str]:
        return {b.name for b in self.admits} - set(self.evicts)


def _drive(sense, *, spoken: list[str] | None = None) -> _Ctx:
    """Run ONE tick of the real shipped rules against *sense*."""
    engine = RuleEngine(
        load_shipped_rules(),
        speech=(spoken.append if spoken is not None else None),
    )
    ctx = _Ctx(now=100.0, tick=1, sense=sense)
    engine.on_tick(ctx)
    return ctx


def _admitted(ctx: _Ctx) -> list[str]:
    return [b.name for b in ctx.admits]


def _by_id(config, rule_id: str):
    for rule in (*config.react, *config.inhibit):
        if rule.id == rule_id:
            return rule
    raise AssertionError(f"no shipped rule {rule_id!r}")


def _write_overlay(text: str) -> Path:
    path = rules_mod.default_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sense_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


# --------------------------------------------------------------------------- #
# The shipped set — identity and shape                                        #
# --------------------------------------------------------------------------- #


def test_the_shipped_layer_ships_exactly_the_three_default_rules() -> None:
    """Few, calm, obviously-correct — a shipped set is not a demo reel."""
    config = load_shipped_rules()
    assert [r.id for r in config.react] == SHIPPED_IDS
    assert config.inhibit == ()
    assert config.modes == {}
    assert config.active_mode is None


def test_shipped_rule_ids_are_a_stable_public_override_interface() -> None:
    """An operator overrides/tombstones BY ID; a rename orphans their override."""
    config = load_shipped_rules()
    # The deployed box already carries a `pat-acknowledge` overlay entry and the
    # repo fixtures + operating guide use that id. Shipping under any other name
    # would silently run BOTH the shipped rule and their override.
    assert "pat-acknowledge" in {r.id for r in config.react}


def test_every_shipped_rule_targets_a_real_library_entry() -> None:
    config = load_shipped_rules()
    for rule in config.react:
        assert rule.behavior in behavior_library.LIBRARY


# --------------------------------------------------------------------------- #
# c32 — corroboration, given a one-predicate schema                           #
# --------------------------------------------------------------------------- #


def test_no_shipped_rule_keys_on_bare_speech() -> None:
    """`speech_detected` read true 45.8 % at rest — a rule on it is a coin flip."""
    config = load_shipped_rules()
    fields = {r.when.field for r in (*config.react, *config.inhibit)}
    assert fields & rules_mod.UNCORROBORATED_SENSE_FIELDS == set()


def test_every_shipped_rule_keys_on_a_self_corroborating_field() -> None:
    """One predicate per rule, so corroboration must live INSIDE the field."""
    config = load_shipped_rules()
    for rule in config.react:
        assert rule.when.field in CORROBORATING_SENSE_FIELDS, rule.id


def test_every_shipped_predicate_field_is_actually_fed() -> None:
    """A field outside FED_SENSE_FIELDS validates and then never fires."""
    config = load_shipped_rules()
    for rule in (*config.react, *config.inhibit):
        assert rule.when.field in FED_SENSE_FIELDS, rule.id


# --------------------------------------------------------------------------- #
# Bounded lifetimes                                                           #
# --------------------------------------------------------------------------- #


def test_every_shipped_rule_on_an_unbounded_looping_entry_carries_a_duration() -> None:
    config = load_shipped_rules()
    for rule in config.react:
        entry = behavior_library.LIBRARY[rule.behavior]
        if entry.looping and entry.default_duration is None:
            assert rule.duration_s is not None and rule.duration_s > 0, rule.id


def test_the_orienting_window_covers_a_whole_turn_hold_recenter_cycle() -> None:
    """Cut short mid-turn, the head would SNAP back to base presence.

    `orient-to-sound` eases home over `recenter_after` and then abstains, so the
    shipped window must outlast turn + hold + recenter for the hand-back to be
    smooth. The number is derived from the behavior's own defaults, never typed
    in independently of them.
    """
    p = OrientParams()
    minimum = p.max_dur + p.hold + p.recenter_after
    assert _by_id(load_shipped_rules(), "look-toward-sound").duration_s >= minimum


def test_the_orienting_rule_admits_no_lower_than_the_behaviors_own_gate() -> None:
    """The rule's ratio IS `OrientParams.rms_ratio` — one number, not two."""
    rule = _by_id(load_shipped_rules(), "look-toward-sound")
    assert rule.when.field == "rms_ratio"
    assert rule.when.value == pytest.approx(OrientParams().rms_ratio)


# --------------------------------------------------------------------------- #
# Coverage — pat, sound, words, and a voice                                   #
# --------------------------------------------------------------------------- #


def test_the_shipped_set_covers_touch_sound_and_words() -> None:
    config = load_shipped_rules()
    assert {r.when.field for r in config.react} == {"pat", "rms_ratio", "transcript"}


def test_exactly_one_shipped_rule_has_a_voice_and_it_is_short() -> None:
    """A shipped utterance fires on real robots in real rooms."""
    config = load_shipped_rules()
    speaking = [r for r in config.react if r.say]
    assert len(speaking) == 1
    assert len(speaking[0].say) <= 40


def test_the_voice_rule_is_separable_from_the_reactions_that_do_not_speak() -> None:
    """Muting the robot must not also cost the operator the pat reaction."""
    config = load_shipped_rules()
    assert _by_id(config, "pat-acknowledge").say is None
    assert _by_id(config, "look-toward-sound").say is None


# --------------------------------------------------------------------------- #
# The quiet-room property (c32's regression test)                             #
# --------------------------------------------------------------------------- #


def test_an_at_rest_snapshot_admits_nothing() -> None:
    assert _admitted(_drive(EMPTY_SENSE)) == []


def test_a_quiet_room_with_the_speech_flag_stuck_true_still_admits_nothing() -> None:
    """The measured 45.8 %-at-rest case, verbatim: flag true, nothing corroborating."""
    quiet = Sense(doa_angle=1.7, speech_detected=True, rms=0.004, rms_ratio=1.0)
    assert _admitted(_drive(quiet)) == []


def test_ambient_hum_below_the_orienting_ratio_admits_nothing() -> None:
    """A hum LOUDER than the retired 0.02 floor, but only 2x its own room.

    The #102 case in one line: absolute loudness says "fire", the room says
    "that is just the room", and the room wins.
    """
    below = OrientParams().rms_ratio - 1.0
    assert _admitted(_drive(Sense(speech_detected=True, rms=0.09, rms_ratio=below))) == []


# --------------------------------------------------------------------------- #
# ...and it DOES react to the three things (offline, no operator config)       #
# --------------------------------------------------------------------------- #


def test_a_pat_admits_the_pet_reaction() -> None:
    ctx = _drive(Sense(pat_event=("scratch", "level1")))
    assert _admitted(ctx) == ["pet-reaction"]


def test_audible_sound_admits_bounded_orienting() -> None:
    ctx = _drive(Sense(doa_angle=1.1, rms=0.05, rms_ratio=12.0))
    assert _admitted(ctx) == ["orient-to-sound"]
    assert ctx.admits[0].lifetime.duration == pytest.approx(
        _by_id(load_shipped_rules(), "look-toward-sound").duration_s
    )


def test_an_addressed_utterance_speaks_and_bobs() -> None:
    spoken: list[str] = []
    ctx = _drive(Sense(transcript="reachy, are you there"), spoken=spoken)
    assert _admitted(ctx) == ["speak"]
    assert spoken == [_by_id(load_shipped_rules(), "greet-when-addressed").say]


def test_the_shipped_defaults_need_no_overlay_at_all() -> None:
    """A fresh install: nothing at the box-local path, yet the rules are live."""
    assert not rules_mod.default_rules_path().exists()
    assert [r.id for r in load_rules().react] == SHIPPED_IDS


def test_behavior_rules_check_is_clean_on_a_fresh_install(capsys) -> None:
    payload = behavior_cmd._rules_check_payload(rules_mod.default_rules_path())
    capsys.readouterr()
    assert payload["reasons"] == []
    assert payload["warnings"] == []
    assert payload["ok"] is True
    assert payload["counts"]["react"] == len(SHIPPED_IDS)


# --------------------------------------------------------------------------- #
# Acceptance 2 — a malformed overlay degrades to the SHIPPED layer            #
# --------------------------------------------------------------------------- #

#: Four ways an operator fumbles the overlay by hand, each a different layer of
#: the validator: the TOML parser, the schema's unknown-field sweep, the
#: library-name check, and the bounded-lifetime invariant.
MALFORMED_OVERLAYS = {
    "bad-toml": "this is [ not valid toml\n",
    "unknown-field": 'mystery = 1\n\n[[react]]\nid = "x"\n',
    "unknown-generator": (
        '[[react]]\nid = "x"\nwhen = { field = "pat", op = "is_true" }\n'
        'run = "not-a-real-behavior"\n'
    ),
    "unbounded-lifetime": (
        '[[react]]\nid = "x"\nwhen = { field = "pat", op = "is_true" }\nrun = "nod"\n'
    ),
}


@pytest.mark.parametrize("flavor", sorted(MALFORMED_OVERLAYS))
def test_a_malformed_overlay_keeps_the_shipped_layer_in_force(flavor, caplog) -> None:
    """Base presence is the floor of LAST resort, not the first response.

    The operator broke THEIR file; the shipped rules are ours and are still
    perfectly valid, so taking them away too would punish a typo twice.
    """
    _write_overlay(MALFORMED_OVERLAYS[flavor])

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        seam = behavior_cmd._boot_tick_seam()

    assert isinstance(seam, ReloadDriver)
    assert [r.id for r in seam.loader.current.react] == SHIPPED_IDS


@pytest.mark.parametrize("flavor", sorted(MALFORMED_OVERLAYS))
def test_a_malformed_overlay_logs_exactly_one_named_drop(flavor, caplog) -> None:
    """ONE line, in the senselog grammar, naming the reason — never a silent no-op."""
    _write_overlay(MALFORMED_OVERLAYS[flavor])

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        behavior_cmd._boot_tick_seam()

    drops = [ln for ln in _sense_lines(caplog) if f"stage={RULE_STAGE}" in ln]
    assert len(drops) == 1
    assert drops[0].startswith(f"[SENSE stage={RULE_STAGE} source=rules event=boot]")


@pytest.mark.parametrize("flavor", sorted(MALFORMED_OVERLAYS))
def test_a_malformed_overlay_never_raises(flavor) -> None:
    """A raise here would feed systemd's Restart=on-failure a crash loop."""
    _write_overlay(MALFORMED_OVERLAYS[flavor])
    assert behavior_cmd._boot_tick_seam() is not None  # no exception, no None


def test_a_malformed_overlay_still_reacts_to_a_pat() -> None:
    """The end-to-end shape of acceptance 2: degraded, but still present."""
    _write_overlay(MALFORMED_OVERLAYS["bad-toml"])
    seam = behavior_cmd._boot_tick_seam()

    engine = RuleEngine(seam.loader.current)
    ctx = _Ctx(now=100.0, tick=1, sense=Sense(pat_event=("scratch", "level1")))
    engine.on_tick(ctx)

    assert _admitted(ctx) == ["pet-reaction"]


def test_a_valid_overlay_still_overrides_a_shipped_rule_by_id() -> None:
    """The two-layer contract survives the shipped layer gaining content."""
    _write_overlay(
        '[[react]]\nid = "pat-acknowledge"\n'
        'when = { field = "pat", op = "is_true" }\nrun = "thoughtful"\n'
    )
    config = load_rules()
    assert [r.id for r in config.react] == SHIPPED_IDS  # position kept
    assert _by_id(config, "pat-acknowledge").behavior == "thoughtful"


def test_a_tombstone_disables_a_shipped_rule_without_touching_the_others() -> None:
    _write_overlay('[[react]]\nid = "greet-when-addressed"\nenabled = false\n')
    config = load_rules()
    assert [r.id for r in config.react] == ["pat-acknowledge", "look-toward-sound"]


# --------------------------------------------------------------------------- #
# The boot banner must not claim the operator's rules loaded when they did not #
# --------------------------------------------------------------------------- #


@dataclass
class _Cfg:
    compose_hz: float = 50.0
    base_layer: bool = True


class _Transport:
    name = "http"


def _banner() -> str:
    return behavior_cmd._engine_live_line(
        _Cfg(), _Transport(), behavior_cmd._boot_tick_seam(), None
    )


def test_the_banner_says_rules_when_the_overlay_is_fine() -> None:
    assert " + rules;" in _banner()


def test_the_banner_admits_the_overlay_was_rejected() -> None:
    """`+ rules` alone would read as "your file loaded" — it did not.

    The shipped layer surviving is the RIGHT outcome, but an operator who just
    fumbled an edit must not be told everything is fine.
    """
    _write_overlay(MALFORMED_OVERLAYS["bad-toml"])
    line = _banner()
    assert "rejected" in line
    assert "shipped" in line


def test_the_banner_still_reports_bare_presence_when_nothing_survives(monkeypatch) -> None:
    monkeypatch.setattr(rules_mod, "shipped_rules_text", lambda: None)
    _write_overlay(MALFORMED_OVERLAYS["bad-toml"])
    assert "base presence only" in _banner()


def test_rules_list_does_not_call_the_shipped_defaults_nothing() -> None:
    """ "No overlay" stopped meaning "no rules" once the release shipped some."""
    payload = behavior_cmd._rules_config_payload(
        load_rules(), path=rules_mod.default_rules_path(), exists=False
    )
    assert "nothing configured" not in payload["note"]
    assert str(len(SHIPPED_IDS)) in payload["note"]

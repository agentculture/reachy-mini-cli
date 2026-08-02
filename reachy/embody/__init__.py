"""The embodiment layer — an external, detachable realtime harness.

The layer is cognition that runs *beside* the symbolic runtime, never inside
it: it hears and speaks over one lobes ``/v1/realtime`` duplex session, thinks
over the streaming ``/v1/chat/completions`` lane, and operates the robot only
through the sanctioned direct-operation action set (the intents spool, the
rules overlay, the speech seams). Enabling or disabling it changes nothing
about how the robot behaves on its own.

Import boundary — asserted by tests, not merely intended:

* Nothing here constructs a ``ReachyMini`` or opens an SDK media session. The
  single-SDK-owner model gives that to the runtime; audio reaches this package
  through the runtime's tee socket (or a bench device), never a second client.
* Nothing under ``reachy/behavior/`` or ``reachy/motion/`` imports this
  package. The dependency runs one way: the layer reads the runtime's exported
  surfaces and writes its spools.
* Command modules import this package *inside functions*, never at module
  scope — ``_build_parser()`` must stay cognition-free (see
  ``tests/test_zero_llm_boundary.py``).

See ``docs/specs/2026-08-01-embodiment-layer.md`` for the converged spec and
``docs/plans/2026-08-01-embodiment-layer.md`` for the build plan.
"""

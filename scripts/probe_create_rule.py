"""create_rule against the LIVE runtime: does the rule land, reload, and persist?"""

import json
import pathlib
import time

from reachy.embody.tools import EmbodyToolRegistry

RULES = pathlib.Path.home() / ".local/state/reachy/behavior/rules.toml"
before = RULES.read_text() if RULES.exists() else ""
print(f"rules.toml before: {len(before)} bytes, embody rules: {before.count('embody-')}")

reg = EmbodyToolRegistry()  # defaults -> the SHARED state dir the engine drains
print("tools:", sorted(reg.names()))

args = json.dumps(
    {
        "id": "embody-pat-thanks",
        "when": {"field": "pat", "op": "is_true"},
        "run": "nod",
        "say": "thank you for the pat",
        "duration_s": 2.0,
    }
)
t0 = time.monotonic()
result = reg.dispatch("create_rule", args, "call-1")
print(f"dispatch took {time.monotonic()-t0:.2f}s")
print("result:", json.dumps(result)[:400])

after = RULES.read_text() if RULES.exists() else ""
print(f"rules.toml after: {len(after)} bytes, embody rules: {after.count('embody-')}")
new = [line for line in after.splitlines() if line not in before.splitlines()]
print("--- added lines ---")
print("\n".join(new[:20]))

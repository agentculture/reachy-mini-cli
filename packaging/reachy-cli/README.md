# reachy-cli

**Transitional alias for [`reachy-mini-cli`](https://pypi.org/project/reachy-mini-cli/).**

`pip install reachy-cli` installs `reachy-mini-cli` — the agent-first CLI for
operating the Reachy Mini expressive robot (device setup, app management, and
runtime ops). It ships no code of its own; it only depends on the canonical
package at the matching version.

Prefer installing the canonical name directly:

```bash
pip install reachy-mini-cli
```

Extras are forwarded, so these are equivalent:

```bash
pip install 'reachy-cli[daemon]'   ==   pip install 'reachy-mini-cli[daemon]'
pip install 'reachy-cli[sdk]'      ==   pip install 'reachy-mini-cli[sdk]'
```

Either package provides the same console command: `reachy` (or `reachy-mini-cli`).

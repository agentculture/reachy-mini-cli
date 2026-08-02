"""Does the gateway emit `reasoning` deltas with enable_thinking FALSE (our default)?"""

import json
import os
import time
import urllib.request

BASE = os.environ["REACHY_OPENAI_URL_BASE"].rstrip("/")
KEY = os.environ["REACHY_OPENAI_API_KEY"]


def probe(label, model, thinking):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Briefly: why is the sky blue?"}],
        "stream": True,
        "temperature": 0.7,
    }
    if thinking is not None:
        body["chat_template_kwargs"] = {"enable_thinking": thinking}
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    keys, chunks, first_content, first_reasoning = set(), 0, None, None
    t0 = time.monotonic()
    gaps = []
    last = t0
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    d = json.loads(payload)["choices"][0].get("delta", {})
                except Exception:
                    continue
                now = time.monotonic()
                gaps.append(now - last)
                last = now
                chunks += 1
                for k, v in d.items():
                    if v not in (None, ""):
                        keys.add(k)
                    if k == "content" and v and first_content is None:
                        first_content = now - t0
                    if k in ("reasoning", "reasoning_content") and v and first_reasoning is None:
                        first_reasoning = now - t0
    except Exception as e:
        print(f"{label}: ERROR {type(e).__name__}: {e}")
        return
    print(f"{label}: model={model} thinking={thinking} chunks={chunks} keys={sorted(keys)}")
    print(
        f"    first_content={first_content!r}s first_reasoning={first_reasoning!r}s "
        f"max_gap={max(gaps) if gaps else 0:.3f}s total={time.monotonic()-t0:.1f}s"
    )


for model in ("worker", "cortex"):
    for thinking in (False, True, None):
        probe(f"[{model}/thinking={thinking}]", model, thinking)

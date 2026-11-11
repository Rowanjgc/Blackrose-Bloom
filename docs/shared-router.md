# Sharing a llama-router

Blackrose normally starts its own llama-router and stops it on exit. If other
clients use the same router, run the router as an independent service instead
(for example a systemd user unit) and let Blackrose connect as one client among
several. Closing a client then cancels only its own requests and disconnects; it
never unloads a model another client might still be using.

In Blackrose's startup TOML:

```toml
router_autostart = false
router_api_key_file = "~/.config/llama-router/shared-router.key"
```

Restart Blackrose after changing startup settings. Setting a key file alone does
not change ownership; `router_autostart = true` remains the standalone default.

## Router service

Start the shared router with `--host 127.0.0.1`, `--models-max 1`,
`--models-autoload` and `--api-key-file` naming a persistent `0600` file. Give
the service no lifetime dependency on any one client (for example no `PartOf=`
on a client unit) and no model preload hook. A client may still use `Wants=` and
`After=` so that starting it brings the router up, while stopping it leaves the
router running.

Use the common section of the presets file to release memory after inactivity:

```ini
[*]
sleep-idle-seconds = 120
```

A sleeping model process stays alive but releases model and KV memory. Inference
wakes it; catalog and properties checks can leave it asleep. Blackrose accepts
both `loaded` and `sleeping` during readiness, and waits for a model already
loading elsewhere without sending another load request. Management tools should
avoid polling `/slots` when idle sleep is configured, because `/slots` can wake a
model.

The key does not rotate when a client exits. Each client reads the same key file
or its own private copy, so deliberate rotation means updating every client.
Keep the key out of process arguments, for example by passing authorization
through a header file or pipe. Each client selects its own model; Blackrose has
no model-selection policy for other clients.

## Contention

With one model slot, a second client's management load during a generation is
refused with HTTP 500 `model limit reached, try again later`. Blackrose retries
only that capacity error and the already-running race, with a deadline and
cancellation, and succeeds once the first generation finishes; unrelated HTTP
errors surface immediately. A second client's inference request instead waits
for the slot and then completes. Explicit unload, switch or stop actions from
another tool remain deliberate administrative operations and may interrupt work.

These behaviors were measured with two clients and a small CPU model on llama.cpp
build b11429. They do not establish fairness or freedom from starvation under
continuous traffic.

## Reproduce

The probe starts an authenticated router on an unused loopback port, uses no GPU
offload, stops only its own process, and never touches a running service:

```sh
.venv/bin/python scripts/check_shared_router.py \
  --model /path/to/a/small-local-model.gguf \
  --output artifacts/shared-router-probe
```

Its two-second idle timeout keeps the check short. Raw router output and a
compact JSON report go to `--output`. Do not commit credentials or router logs.

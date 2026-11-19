# CLAUDE.md

Shared storage box for No Man's Sky (PC, Cosmos 7.x) between two players, built on NMS.py/pyMHF.

## Layout
- `docs/superpowers/specs/` — design spec (source of truth for behaviour)
- `relay/` — WebSocket relay (Python 3.12), deployed as container `nms-relay` via `~/is-infra` (planned)
- `mod/` — NMS.py mod `shared_box.py` + `shared_box.toml` (planned)

## Services
- Relay: container `nms-relay`, port 8080 inside the `self-hosted` Docker network, **no published port**.
  Public only via the Cloudflare tunnel at `nms.xyyoutube.com` (hostname added in the Cloudflare dashboard).
- Env: `NMS_RELAY_TOKEN` (server `.env`, shared with both mods).

## Game-side facts (nmspy 180132)
- Launch: `& "$env:LOCALAPPDATA\Programs\Python\Python311\Scripts\pymhf.exe" run nmspy`
- Mods folder: `C:\Users\computer\NMSpyMods`; logs: `<NMS>\Binaries\logs\pymhf-*.log`
- `mInventories[12]` = Container 0. `NMS.exe+0x580000` = menu move (hook point). See spec §2.

## Deploy
- Relay code: this repo, cloned on tt-server at `/home/tt/nms-shared-box` (pull manually: `ssh tt@192.168.1.9 'cd ~/nms-shared-box && git pull'`).
- Compose service `nms-relay` lives in `~/is-infra`; `git push && deploy` rebuilds it.
- Token: `NMS_RELAY_TOKEN` in `projects/self-hosted/.env` on the server; same value in each player's `shared_box.toml`.
- Public hostname `nms.xyyoutube.com → http://nms-relay:8080` is set in the Cloudflare dashboard (token-managed tunnel).
- Probe: `.venv/bin/python tools/relay_ping.py wss://nms.xyyoutube.com <token>`

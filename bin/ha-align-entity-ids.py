#!/usr/bin/env python3
"""Align Home Assistant entity ids with the object_id published by discovery.

`object_id` only decides an entity id when Home Assistant creates the registry
entry. An entity discovered before mqtt.py started publishing object_id keeps
whatever id HA derived from its display name, forever -- HA matches on
unique_id, so re-discovery reattaches to the old entry rather than renaming it.
That left one tower straddling two schemes:

    light.gardyn_light                    (no identifier at all -- collides
                                           with a second tower)
    sensor.gardyn_1_gardyn_water_depth    (HA disambiguating a duplicate
                                           device name)

This renames each entry in place to `<domain>.<unique_id>`, which is what
discovery now asks for. Renaming beats deleting the device: the registry entry
survives, so recorder history and long-term statistics follow the entity
instead of being orphaned.

Registry writes are websocket-only (the REST API cannot do this), hence the
dependency. Reads first, prints a plan, and only writes with --apply.

    python bin/ha-align-entity-ids.py --url http://ha.local:8123
    python bin/ha-align-entity-ids.py --url http://ha.local:8123 --apply

The token is read from --token-file (default .ha_token, which .gitignore
excludes) so it is never passed on a command line or committed.
"""

import argparse
import asyncio
import json
import pathlib
import sys

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")


async def run(url, token, prefix, apply_changes):
    ws_url = url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
    async with websockets.connect(f"{ws_url}/api/websocket", max_size=16 * 1024 * 1024) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            sys.exit(f"auth failed: {auth}")

        msg_id = 1
        await ws.send(json.dumps({"id": msg_id, "type": "config/entity_registry/list"}))
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == msg_id and reply.get("type") == "result":
                break
        if not reply.get("success"):
            sys.exit(f"registry list failed: {reply}")

        planned, skipped, conflicts = [], [], []
        existing = {e["entity_id"] for e in reply["result"]}

        for entry in reply["result"]:
            unique_id = entry.get("unique_id") or ""
            if entry.get("platform") != "mqtt" or not unique_id.startswith(prefix):
                continue
            domain = entry["entity_id"].split(".")[0]
            target = f"{domain}.{unique_id}"
            if entry["entity_id"] == target:
                skipped.append(target)
            elif target in existing:
                conflicts.append((entry["entity_id"], target))
            else:
                planned.append((entry["entity_id"], target))

        print(f"already correct: {len(skipped)}")
        for before, after in sorted(planned):
            print(f"  rename  {before}\n       ->  {after}")
        print(f"to rename: {len(planned)}")
        for before, after in conflicts:
            print(f"  CONFLICT {before} -> {after} (target id already exists; skipped)")

        if not apply_changes:
            print("\ndry run -- pass --apply to write these changes")
            return

        done = 0
        for before, after in planned:
            msg_id += 1
            await ws.send(
                json.dumps(
                    {
                        "id": msg_id,
                        "type": "config/entity_registry/update",
                        "entity_id": before,
                        "new_entity_id": after,
                    }
                )
            )
            while True:
                res = json.loads(await ws.recv())
                if res.get("id") == msg_id and res.get("type") == "result":
                    break
            if res.get("success"):
                done += 1
            else:
                print(f"  FAILED {before}: {res.get('error')}")
        print(f"\nrenamed {done}/{len(planned)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="e.g. http://192.168.1.10:8123")
    ap.add_argument("--token-file", default=".ha_token")
    ap.add_argument("--prefix", default="gardyn_01", help="unique_id prefix (MQTT_IDENTIFIER)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    token = pathlib.Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        sys.exit(f"{args.token_file} is empty")
    asyncio.run(run(args.url, token, args.prefix + "_", args.apply))


if __name__ == "__main__":
    main()

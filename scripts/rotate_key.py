#!/usr/bin/env python3
"""Rotate an account's full-access key on testnet (add new, delete old).

Usage: python3 scripts/rotate_key.py <account.testnet>

Reads the current key from ~/.near-credentials/testnet/<account>.json, adds a
fresh full-access key, proves the new key signs (1-yocto self-transfer),
deletes the old key, then updates the local credential file (chmod 600).
The account itself — balances, contracts, roles — is unaffected.
"""
import json
import os
import stat
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "sweeft"
sys.path.insert(0, str(BASE))
os.chdir(BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("NEAR_RPC_URL", "https://test.rpc.fastnear.com")

import django  # noqa: E402

django.setup()

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from apps.escrow.server_tx import (  # noqa: E402
    _parse_key,
    _transfer_action,
    add_full_access_key_action,
    send_transaction,
)
from apps.wallets.base58 import b58encode  # noqa: E402

if len(sys.argv) != 2:
    sys.exit("usage: python3 scripts/rotate_key.py <account.testnet>")
account = sys.argv[1]
cred_path = Path.home() / ".near-credentials" / "testnet" / f"{account}.json"
cred = json.loads(cred_path.read_text())
old_priv = cred["private_key"]

priv = Ed25519PrivateKey.generate()
seed = priv.private_bytes(
    serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
)
pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
new_priv = "ed25519:" + b58encode(seed + pub)
new_pub = "ed25519:" + b58encode(pub)

print(f"rotating {account}")
print(f"  old key: {cred['public_key']}")
print(f"  new key: {new_pub}")

r = send_transaction(signer=account, private_key=old_priv, receiver=account, action=add_full_access_key_action(pub))
if not r["success"]:
    sys.exit(f"add-key failed: {r['error']}")
print(f"  add new key: ok ({r['tx_hash']})")
time.sleep(4)

r = send_transaction(signer=account, private_key=new_priv, receiver=account, action=_transfer_action(1))
if not r["success"]:
    sys.exit(f"new-key test-sign failed: {r['error']}")
print(f"  new key signs: ok ({r['tx_hash']})")
time.sleep(4)

_, old_pub_raw = _parse_key(old_priv)
r = send_transaction(signer=account, private_key=new_priv, receiver=account, action=b"\x06\x00" + old_pub_raw)
if not r["success"]:
    sys.exit(f"delete-old-key failed: {r['error']}")
print(f"  old key deleted: ok ({r['tx_hash']})")

cred_path.write_text(json.dumps({"public_key": new_pub, "private_key": new_priv}))
os.chmod(cred_path, stat.S_IRUSR | stat.S_IWUSR)
print(f"  credentials updated: {cred_path} (0600)")
print("done — a browser wallet holding the old key must re-import the new one.")

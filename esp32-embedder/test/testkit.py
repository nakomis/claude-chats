"""Prepares age's test kit (C2SP CCTV, age-testkit/) for test_age.c.

Each vector is a text header ("expect: success", "identity: AGE-SECRET-KEY-1...", ...), a blank
line, then an age file, maybe zlib-compressed. This writes, per vector Kata's age.c should handle,
<out>/<name>.meta (the header, less anything but the keys test_age reads) and <name>.age (the age
file, decompressed). Vectors outside an X25519-only decrypter's remit are listed in
<out>/SKIPPED with the reason: armoured files, passphrases (scrypt) and post-quantum identities.

    python3 -I testkit.py age-testkit build/testkit
"""
import os
import sys
import zlib

src, out = sys.argv[1:3]
os.makedirs(out, exist_ok=True)
kept, skipped = [], []
for name in sorted(os.listdir(src)):
    path = os.path.join(src, name)
    if not os.path.isfile(path) or name.startswith(".") or name.endswith(".md"):
        continue
    data = open(path, "rb").read()
    head, _, body = data.partition(b"\n\n")
    meta = {}
    identities = []
    for line in head.decode().split("\n"):
        k, _, v = line.partition(": ")
        if k == "identity":
            identities.append(v)
        else:
            meta[k] = v
    why = None
    if meta.get("armored") == "yes":
        why = "armoured"
    elif "passphrase" in meta:
        why = "passphrase"
    elif not identities or any(not i.startswith("AGE-SECRET-KEY-1") for i in identities):
        why = "not an X25519 identity"
    if why:
        skipped.append(f"{name}: {why}")
        continue
    if meta.get("compressed") == "zlib":
        body = zlib.decompress(body)
    elif "compressed" in meta:
        sys.exit(f"{name}: unknown compression {meta['compressed']}")
    with open(os.path.join(out, name + ".meta"), "w") as f:
        f.write(f"expect: {meta['expect']}\n")
        if "payload" in meta:
            f.write(f"payload: {meta['payload']}\n")
        if "file key" in meta:
            f.write(f"file key: {meta['file key']}\n")
        for i in identities:
            f.write(f"identity: {i}\n")
    open(os.path.join(out, name + ".age"), "wb").write(body)
    kept.append(name)
open(os.path.join(out, "INDEX"), "w").write("\n".join(kept) + "\n")
open(os.path.join(out, "SKIPPED"), "w").write("\n".join(skipped) + "\n")
print(f"age test kit: {len(kept)} vectors for X25519, {len(skipped)} skipped")

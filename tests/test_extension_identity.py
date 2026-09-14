"""Chrome extension identity is derived from the checked-in public key only."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from xhs_workbench.extension_identity import derive_extension_id

ROOT = Path(__file__).parents[1]


def test_derives_chrome_extension_id_from_first_sixteen_sha256_bytes() -> None:
    public_key = b"chrome-extension-public-key-test-vector"

    assert derive_extension_id(public_key) == "fdccfhkafkfpoeejiggchaneihbohfci"


def test_checked_in_public_key_has_the_recorded_deterministic_extension_id() -> None:
    manifest = json.loads((ROOT / "chrome_extension" / "public" / "manifest.json").read_text())

    assert derive_extension_id(base64.b64decode(manifest["key"], validate=True)) == "ohcadfmflnjoofmimlidfgnehpkfoofg"

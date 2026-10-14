#!/usr/bin/env python3
"""Regenerate index/manifest.json from the shard files.

The manifest is a build artifact (§4). Rebuild it; never edit it.
"""

import sys

import shard

if __name__ == "__main__":
    manifest = shard.build_index()
    print(f"index/manifest.json: {manifest['shard_count']} shard(s)")
    sys.exit(0)

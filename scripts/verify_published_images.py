"""Check a sealed image ledger against a locally supplied, immutable publisher index."""

import argparse
from collections.abc import Iterator
from hashlib import sha256
import json
from pathlib import Path

from resource_policy import published_images


def string_values(document) -> Iterator[str]:
    if isinstance(document, str):
        yield document
    elif isinstance(document, dict):
        for child in document.values():
            yield from string_values(child)
    elif isinstance(document, list):
        for child in document:
            yield from string_values(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--publisher-index", type=Path, required=True)
    args = parser.parse_args()
    registry = Path(__file__).resolve().parents[1] / "profiles"
    lock = json.loads((registry / "lock.json").read_bytes())[args.profile]
    policy = json.loads((registry / "public_resources.json").read_bytes())[
        lock["package"]
    ]
    if "published_images" not in policy:
        parser.error("profile has no sealed image ledger")
    images = published_images(args.snapshot, lock, policy, registry)
    ledger = json.loads((registry / policy["published_images"]["ledger"]).read_bytes())
    body = args.publisher_index.read_bytes()
    if sha256(body).hexdigest() != ledger["publisher_index_sha256"]:
        raise ValueError("publisher index differs from the recorded revision")
    published = set(string_values(json.loads(body)))
    if not images.keys() <= published:
        raise ValueError("sealed URL is absent from the immutable publisher index")
    print(f"{len(images)} exact URLs verified")


if __name__ == "__main__":
    main()

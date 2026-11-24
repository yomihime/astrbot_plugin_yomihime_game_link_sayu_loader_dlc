"""Compare standalone and registered trigger grammar with the pinned implementation."""

import argparse
import asyncio
from itertools import product
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace


async def compare():
    from native_contract import ROOT, offline

    sys.addaudithook(offline)
    sys.path.insert(0, str(Path.cwd()))
    from gsuid_core import trigger as native
    from gsuid_core.models import Event

    sys.path.insert(0, str(ROOT / "worker"))
    import plugin_trigger as loader
    from plugin_messages import Event as WorkerEvent

    stats = {}
    for mode in (
        "command",
        "fullmatch",
        "prefix",
        "suffix",
        "keyword",
        "regex",
        "file",
        "message",
        "meta",
    ):
        tested = 0
        for prefix, keyword, to_me in product(
            ("", "qx"),
            ("图A", "abc", "車C", "two words")
            if mode != "regex"
            else ("^(?P<id>[a-z]+)([0-9]+)?$",),
            (False, True),
        ):
            service = SimpleNamespace(aside=None, pm=6)
            args = (mode, keyword, None, prefix, True, to_me, None, service)
            nt, lt = native.Trigger(*args), loader.Trigger(*args)
            for module, trig in ((native, nt), (loader, lt)):
                assert module.dispatch_sort_key(trig, 2) == native.dispatch_sort_key(
                    nt, 2
                )
                assert (
                    trig.gap_positions(),
                    trig.head,
                    trig.trie_head(),
                    trig.aside,
                ) == (nt.gap_positions(), nt.head, nt.trie_head(), nt.aside)
            for head, word, tail in product(
                (prefix, " " + prefix + " ", prefix + "\t", "?"),
                (keyword, keyword[:1] + " " + keyword[1:], "abc3", ""),
                ("", " ", "  body"),
            ):
                test = dict(
                    raw_text=head + word + tail,
                    is_tome=bool(head),
                    file="fixture",
                    file_name="fixture." + word,
                    meta_event_type=word,
                )
                ne, le = Event(**test), WorkerEvent(**test)
                observed = nt.check_command(ne)
                assert observed == lt.check_command(le), (mode, keyword, test, observed)
                if observed:
                    await nt.get_command(ne)
                    await lt.get_command(le)
                    for name in ("text", "command", "regex_group", "regex_dict"):
                        assert getattr(ne, name) == getattr(le, name), (
                            mode,
                            test,
                            name,
                        )
                tested += 1
        stats[mode] = tested
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from native_contract import copy_reference, REFERENCE_COMMIT

    if args.child:
        counts = asyncio.run(compare())
        args.output.write_text(
            json.dumps(
                {"reference": REFERENCE_COMMIT, "matched_observations": counts},
                indent=2,
            ),
            encoding="utf8",
        )
        return
    with tempfile.TemporaryDirectory(prefix="sayu-triggers-") as tmp:
        copy_reference(args.reference.resolve(), Path(tmp))
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--reference",
                tmp,
                "--output",
                str(args.output.resolve()),
                "--child",
            ],
            cwd=tmp,
            check=True,
        )


if __name__ == "__main__":
    main()

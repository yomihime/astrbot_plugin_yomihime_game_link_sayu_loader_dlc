"""Compare scheduled execution, misfires and retirement in isolated scheduler processes."""

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_contract import (
    ROOT,
    REFERENCE_COMMIT,
    copy_reference,
    offline,
    worker_archive,
)


def generation(mode, archive):
    if mode == "native":
        sys.path.insert(0, str(Path.cwd()))
        from gsuid_core import aps

        return aps.scheduler, aps.shutdown_scheduler

    sys.path.insert(0, str(archive))
    import sayu_compat
    from plugin_runtime import PluginRuntime

    root = Path.cwd()
    sayu_compat.install("Fixture", "fx", root, source_root=root)
    runtime = PluginRuntime(root)
    runtime.install(root, "Fixture")
    return runtime.scheduler, runtime.stop


async def backlog(scheduler):
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_MISSED

    anchor = datetime.now(timezone.utc)
    events = defaultdict(list)
    calls = Counter()
    completed = asyncio.Event()
    expected = {
        "default": [[-60, "ran"]],
        "all": [[-180, "ran"], [-120, "ran"], [-60, "ran"]],
        "mixed": [[-180, "missed"], [-120, "missed"], [-60, "ran"]],
        "expired": [[-180, "missed"]],
    }

    def record(event):
        offset = (event.scheduled_run_time - anchor).total_seconds()
        kind = "ran" if event.code == EVENT_JOB_EXECUTED else "missed"
        events[event.job_id].append([offset, kind])
        if all(len(events[k]) >= len(v) for k, v in expected.items()):
            completed.set()

    async def callback(key):
        calls[key] += 1
        return key

    scheduler.add_listener(record, EVENT_JOB_EXECUTED | EVENT_JOB_MISSED)
    scheduler.start(paused=True)
    for key, start, options in (
        ("default", -180, {}),
        ("all", -180, {"coalesce": False, "misfire_grace_time": None}),
        ("mixed", -180, {"coalesce": False}),
        ("expired", -300, {}),
    ):
        # Explicit next_run_time is essential: an interval registered after its
        # start_date otherwise computes the next future occurrence and skips the backlog.
        first = anchor + timedelta(seconds=start)
        scheduler.add_job(
            callback,
            "interval",
            id=key,
            args=[key],
            seconds=60,
            start_date=first,
            end_date=first + timedelta(seconds=120),
            next_run_time=first,
            **options,
        )
    job = scheduler.get_job("default")
    defaults = [job.coalesce, job.misfire_grace_time]
    scheduler.resume()
    await asyncio.wait_for(completed.wait(), 10)
    scheduler.remove_listener(record)
    assert dict(events) == expected, events
    assert calls == {"default": 1, "all": 3, "mixed": 1}, calls
    assert defaults == [True, 90], defaults
    return {
        "defaults": defaults,
        "events": dict(events),
        "callback_counts": dict(calls),
    }


async def retirement(mode, archive, old, stop):
    from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN

    loop = asyncio.get_running_loop()
    async_entered, async_cancelled = asyncio.Event(), asyncio.Event()
    sync_entered, sync_done = asyncio.Event(), asyncio.Event()
    stopped, replacement_tick = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    calls = []

    async def in_flight():
        async_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            async_cancelled.set()
            raise

    def sync_in_flight():
        loop.call_soon_threadsafe(sync_entered.set)
        if not release.wait(15):
            raise TimeoutError("test did not release the running synchronous callback")
        loop.call_soon_threadsafe(sync_done.set)

    async def tick(label):
        calls.append(label)
        if label == "new":
            replacement_tick.set()

    old.add_listener(lambda _: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
    old.add_job(in_flight, "date", id="already-running")
    old.add_job(sync_in_flight, "date", id="sync-running")
    try:
        await asyncio.wait_for(
            asyncio.gather(async_entered.wait(), sync_entered.wait()), 10
        )
        # Keep a repeating timer armed for an actual deadline after retirement.
        due = datetime.now(timezone.utc) + timedelta(seconds=1)
        old.add_job(
            tick,
            "interval",
            id="same-id",
            args=["old"],
            start_date=due,
            next_run_time=due,
            seconds=0.1,
        )
        await asyncio.sleep(0)
        await stop()
        await asyncio.wait_for(stopped.wait(), 5)
        await asyncio.wait_for(async_cancelled.wait(), 5)
        assert not old.running and old.get_jobs() == []
    finally:
        release.set()
    await asyncio.wait_for(sync_done.wait(), 5)

    new, stop_new = generation(mode, archive)
    new.start()
    try:
        # Reuse the retired id, then wait until its old recurring interval would
        # also have elapsed. Only the replacement generation may call tick().
        new.add_job(
            tick,
            "date",
            id="same-id",
            args=["new"],
            run_date=due + timedelta(milliseconds=250),
            misfire_grace_time=None,
        )
        await asyncio.wait_for(replacement_tick.wait(), 10)
        await asyncio.sleep(0.25)
        assert calls == ["new"], calls
    finally:
        await stop_new()
        await asyncio.sleep(0)
    return {
        "shutdown_event": stopped.is_set(),
        "async_inflight_cancelled": async_cancelled.is_set(),
        "sync_inflight_finished_after_stop": sync_done.is_set(),
        "callbacks": calls,
    }


async def observe(mode, archive):
    sys.addaudithook(offline)
    scheduler, stop = generation(mode, archive)
    return {
        "apscheduler": version("apscheduler"),
        "backlog": await backlog(scheduler),
        "retirement": await retirement(mode, archive, scheduler, stop),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("native", "loader"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / ".sayu-local/reference"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode:
        result = asyncio.run(observe(args.mode, args.archive))
    else:
        result = {"reference": REFERENCE_COMMIT}
        with tempfile.TemporaryDirectory(prefix="sayu-aps-timing-") as tmp:
            root = Path(tmp)
            archive = root / "worker.pyz"
            data = (
                args.archive.read_bytes()
                if args.archive
                else worker_archive({"package": "Fixture", "prefix": "fx"})
            )
            archive.write_bytes(data)
            result["worker_sha256"] = hashlib.sha256(data).hexdigest()
            for mode in ("native", "loader"):
                work = root / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference, work)
                out = work / "observations.json"
                subprocess.run(
                    [
                        sys.executable,
                        "-IB",
                        "-X",
                        "utf8",
                        __file__,
                        "--mode",
                        mode,
                        "--archive",
                        str(archive),
                        "--output",
                        str(out),
                    ],
                    cwd=work,
                    timeout=60,
                    check=True,
                )
                result[mode] = json.loads(out.read_bytes())
        assert result["native"] == result["loader"], result
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()

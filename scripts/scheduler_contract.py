"""Compare native APS public helpers and real APScheduler Job objects."""

import argparse
import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_local import worker_archive  # noqa: E402
from native_contract import REFERENCE_COMMIT, copy_reference, offline  # noqa: E402


async def job_a():
    return "a"


async def job_b():
    return "b"


async def observe(mode, archive):
    sys.addaudithook(offline)
    work = Path.cwd()
    if mode == "native":
        sys.path.insert(0, str(work))
    else:
        sys.path.insert(0, str(archive))
        import sayu_compat
        from plugin_runtime import PluginRuntime

        sayu_compat.install("Fixture", "fx", work, source_root=work)
        PluginRuntime(work).install(work, "Fixture")
    from gsuid_core import aps

    scheduler = aps.scheduler

    def listing():
        jobs = aps.get_all_aps_job()
        return [
            type(jobs).__name__,
            [(type(j).__module__, type(j).__name__, j.id, j.name) for j in jobs],
        ]

    results = {
        "list_parameters": list(inspect.signature(aps.get_all_aps_job).parameters),
        "add_parameters": list(inspect.signature(aps.add_scheduled_job).parameters),
    }

    async def make(key, year, func=job_a, **kw):
        return await aps.add_scheduled_job(
            None, func, "date", key, run_date=f"{year}-01-02T12:00:00+00:00", **kw
        )

    await make("later", 2091)
    await make("early", 2090)
    results["pending"] = listing()
    scheduler.start(paused=True)
    results["sorted"] = listing()
    job = scheduler.get_job("early")
    job.modify(name="changed")
    job.pause()
    results["paused"] = listing()
    job.resume()
    job.reschedule("date", run_date=datetime(2092, 1, 1, tzinfo=timezone.utc))
    results["rescheduled"] = listing()
    results["replaced"] = (await make("later", 2091, job_b, job_name="other")).name
    results["conflict"] = (await make("later", 2091, replace_existing=False)) is None
    results["bad_trigger"] = [
        (await aps.add_scheduled_job(None, job_a, "invalid")) is None,
        (await aps.add_scheduled_job(None, job_a, "date")) is None,
        (await aps.add_scheduled_job(None, job_b, "cron", hour="27")) is None,
    ]
    await make("dup_a", 2091, job_name="same")
    await make("dup_b", 2092, job_b, job_name="same")
    aps.remove_repeat_job()
    results["deduplicated"] = listing()
    job.remove()
    try:
        scheduler.remove_job("missing")
    except Exception as error:
        results["missing"] = type(error).__name__
    results["deleted"] = listing()
    interval = await aps.add_scheduled_job(
        None,
        job_a,
        "interval",
        job_id="recur",
        hours=1,
        start_date="2090-01-01T00:00:00+00:00",
    )
    cron = await aps.add_scheduled_job(
        None, job_b, "cron", job_id="weekly", day_of_week="wed", hour="16"
    )
    results["descriptions"] = [
        aps._get_trigger_description(x.trigger) for x in (interval, cron)
    ]
    results["decorated"] = (
        scheduler.scheduled_job(
            "cron", id="decorated", replace_existing=False, hour="23"
        )(job_a)
        is job_a
    )
    await aps.shutdown_scheduler()
    await asyncio.sleep(0)
    results["stopped"] = [scheduler.running, listing()]
    await aps.start_scheduler()
    await aps.start_scheduler()
    results["restarted"] = scheduler.running
    if mode == "loader":
        from plugin_scheduler import GenerationScheduler, install_scheduler
        from port_errors import Unsupported

        owner = ContextVar("fixture_owner", default=None)
        fired = asyncio.get_running_loop().create_future()

        async def detached():
            fired.set_result(owner.get())

        token = owner.set("private")
        try:
            scheduler.add_job(
                detached,
                "date",
                id="fire",
                run_date=datetime.now(timezone.utc) + timedelta(milliseconds=35),
            )
            assert await asyncio.wait_for(fired, 1) is None
        finally:
            owner.reset(token)
        other = GenerationScheduler()
        port = install_scheduler(ModuleType("aps"), other)
        other.add_job(
            job_a,
            "date",
            id="other",
            run_date=datetime(2090, 1, 1, tzinfo=timezone.utc),
        )
        scheduler.add_job(
            job_b,
            "date",
            id="other",
            run_date=datetime(2091, 1, 1, tzinfo=timezone.utc),
        )
        assert [x.func for x in port.get_all_aps_job()] == [job_a]
        assert [x.func for x in aps.get_all_aps_job()] == [job_b]
        other.get_job("other").remove()
        assert not port.get_all_aps_job() and scheduler.get_job("other").func is job_b
        try:
            other.add_jobstore("memory", "foreign")
        except Unsupported:
            pass
        else:
            raise AssertionError("unexpected extra job store")
        other.remove_all_jobs()
    await aps.shutdown_scheduler()
    await asyncio.sleep(0)
    return results


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
        with tempfile.TemporaryDirectory(prefix="sayu-aps-") as t:
            root = Path(t)
            archive = root / "worker.pyz"
            archive.write_bytes(worker_archive({"package": "Fixture", "prefix": "fx"}))
            for mode in ("native", "loader"):
                work = root / mode
                work.mkdir()
                if mode == "native":
                    copy_reference(args.reference, work)
                out = work / "result.json"
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
        if result["native"] != result["loader"]:
            raise AssertionError(result)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )


if __name__ == "__main__":
    main()

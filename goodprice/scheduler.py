from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger


def build_scheduler(session_factory, submit_fn, task_service) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        _sync_tasks,
        trigger=IntervalTrigger(minutes=5),
        args=[session_factory, submit_fn, task_service, scheduler],
        id="sync_tasks",
        replace_existing=True,
        max_instances=1,
    )
    _sync_tasks(session_factory, submit_fn, task_service, scheduler)
    return scheduler


def _sync_tasks(session_factory, submit_fn, task_service, scheduler) -> None:
    enabled_ids = sorted(task.id for task in task_service.enabled_tasks())
    job_ids = {job.id for job in scheduler.get_jobs()}
    for task_id in enabled_ids:
        job_id = f"crawl_{task_id}"
        task = task_service.get_task(task_id)
        trigger = IntervalTrigger(minutes=max(1, task.interval_minutes))
        if job_id in job_ids:
            current = scheduler.get_job(job_id)
            current_seconds = getattr(getattr(current, "trigger", None), "interval", None)
            current_seconds = (
                current_seconds.total_seconds() if current_seconds is not None else None
            )
            if current_seconds != trigger.interval.total_seconds():
                scheduler.reschedule_job(job_id, trigger=trigger)
            continue
        scheduler.add_job(
            submit_fn,
            trigger=trigger,
            args=[task_id],
            id=job_id,
            next_run_time=datetime.now(),
            replace_existing=True,
            max_instances=1,
        )
    for job_id in list(job_ids):
        if not job_id.startswith("crawl_"):
            continue
        task_id = int(job_id.removeprefix("crawl_"))
        if task_id not in enabled_ids:
            scheduler.remove_job(job_id)

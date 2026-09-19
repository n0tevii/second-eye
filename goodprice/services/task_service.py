from typing import Optional

from goodprice.models import Listing, Notification, WatchTask


class TaskService:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def list_tasks(self) -> list[WatchTask]:
        with self._session_factory() as session:
            return session.query(WatchTask).order_by(WatchTask.id).all()

    def get_task(self, task_id: int) -> Optional[WatchTask]:
        with self._session_factory() as session:
            return session.get(WatchTask, task_id)

    def create_task(self, data: dict) -> WatchTask:
        task = WatchTask(
            name=data.get("name", ""),
            keyword=data["keyword"],
            max_price=float(data.get("max_price") or 0),
            min_price=float(data.get("min_price") or 0),
            exclude_words=data.get("exclude_words", ""),
            condition_requirement=data.get("condition_requirement", ""),
            min_condition_score=int(data.get("min_condition_score") or 0),
            platform=data.get("platform", "xianyu"),
            interval_minutes=int(data.get("interval_minutes") or 20),
            fetch_detail=bool(data.get("fetch_detail", True)),
            enabled=bool(data.get("enabled", True)),
        )
        with self._session_factory() as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def toggle_task(self, task_id: int) -> Optional[WatchTask]:
        with self._session_factory() as session:
            task = session.get(WatchTask, task_id)
            if task:
                task.enabled = not task.enabled
                session.commit()
                session.refresh(task)
            return task

    def delete_task(self, task_id: int) -> bool:
        with self._session_factory() as session:
            task = session.get(WatchTask, task_id)
            if not task:
                return False
            session.delete(task)
            session.commit()
            return True

    def enabled_tasks(self) -> list[WatchTask]:
        with self._session_factory() as session:
            return session.query(WatchTask).filter(WatchTask.enabled.is_(True)).all()

    def update_task(self, task_id: int, data: dict) -> Optional[WatchTask]:
        with self._session_factory() as session:
            task = session.get(WatchTask, task_id)
            if not task:
                return None
            old_requirement = (task.condition_requirement or "").strip()
            if data.get("keyword"):
                task.keyword = data["keyword"].strip()
            if "name" in data:
                task.name = data.get("name", "").strip()
            if "max_price" in data:
                task.max_price = float(data.get("max_price") or 0)
            if "min_price" in data:
                task.min_price = float(data.get("min_price") or 0)
            if "exclude_words" in data:
                task.exclude_words = data.get("exclude_words", "")
            if "condition_requirement" in data:
                task.condition_requirement = data.get("condition_requirement", "")
            if "min_condition_score" in data:
                task.min_condition_score = int(data.get("min_condition_score") or 0)
            if "interval_minutes" in data:
                task.interval_minutes = int(data.get("interval_minutes") or 20)
            if "fetch_detail" in data:
                task.fetch_detail = bool(data.get("fetch_detail"))
            if "enabled" in data:
                task.enabled = bool(data.get("enabled"))
            if old_requirement != (task.condition_requirement or "").strip():
                for listing in session.query(Listing).filter_by(task_id=task_id):
                    listing.requirement_match = None
                    listing.requirement_input_hash = None
                    listing.requirement_reason = "需求已修改，等待重新核验"
                    listing.condition_score = None
                    listing.condition_detail = None
                    listing.value_score = None
                    listing.value_batch_at = None
                    listing.best_of_batch = False
                    listing.satisfaction = 0
                    listing.needs_verification = True
                    listing.verification_reasons = ["需求已修改，等待重新核验"]
                session.query(Notification).filter_by(task_id=task_id, status="failed").update(
                    {"status": "superseded"}, synchronize_session=False
                )
            session.commit()
            session.refresh(task)
            return task

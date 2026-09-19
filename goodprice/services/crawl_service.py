import hashlib
import logging
import random
import threading
import time
from datetime import datetime
from typing import Any, Optional

from goodprice.analysis.capacity import check_capacity_requirements, requirement_input_hash
from goodprice.crawler.base import ListingData
from goodprice.crawler.parser import is_product_image
from goodprice.models import Listing, Notification, PriceSnapshot, WatchTask
from goodprice.notify.base import NotificationMessage
from goodprice.security import redact_secrets
from goodprice.services.satisfaction import (
    compute_satisfaction,
    drop_pct_from_snapshots,
)

logger = logging.getLogger(__name__)

NOT_SEEN_THRESHOLD = 3
MAX_BATCH_VALUE_ITEMS = 30
MAX_NOTIFICATION_ATTEMPTS = 3


class TaskDisabled(RuntimeError):
    pass


class TaskRequirementsChanged(RuntimeError):
    pass


class TaskRunGuard:
    """进程内任务防重入守卫。"""

    def __init__(self):
        self._running: set[int] = set()
        self._lock = threading.Lock()

    def try_start(self, task_id: int) -> bool:
        with self._lock:
            if task_id in self._running:
                return False
            self._running.add(task_id)
            return True

    def finish(self, task_id: int) -> None:
        with self._lock:
            self._running.discard(task_id)

    def running_ids(self) -> set[int]:
        with self._lock:
            return set(self._running)


class CrawlService:
    def __init__(
        self,
        session_factory,
        adapter,
        llm,
        vision,
        notifiers,
        settings_service,
        guard=None,
        seller_service=None,
    ):
        self._session_factory = session_factory
        self.adapter = adapter
        self.llm = llm
        self.vision = vision
        self.notifiers = notifiers
        self.settings_service = settings_service
        self.guard = guard or TaskRunGuard()
        self.seller_service = seller_service

    def run_task(self, task_id: int) -> dict[str, Any]:
        if not self.guard.try_start(task_id):
            logger.info("任务 %s 已在运行，忽略重复触发", task_id)
            return {"found": 0, "new": 0, "notified": 0, "skipped": "already_running"}
        try:
            return self._run_impl(task_id)
        except TaskRequirementsChanged:
            logger.info("任务 %s 需求已修改，停止本轮旧需求处理", task_id)
            return {"found": 0, "new": 0, "notified": 0, "skipped": "requirements_changed"}
        except TaskDisabled:
            logger.info("任务 %s 已停用，在下一检查点停止", task_id)
            return {"found": 0, "new": 0, "notified": 0, "skipped": "disabled"}
        finally:
            self.guard.finish(task_id)

    def _run_impl(self, task_id: int) -> dict[str, Any]:
        stats = {
            "found": 0,
            "new": 0,
            "notified": 0,
            "backfilled": 0,
            "reevaluated": 0,
            "not_seen_recently": 0,
        }
        settings = self.settings_service.get()
        self._assert_enabled(task_id)
        jitter = int(settings.default_crawl_jitter_minutes)
        if jitter:
            self._interruptible_wait(task_id, random.uniform(0, jitter * 60))
        with self._session_factory() as session:
            task = session.get(WatchTask, task_id)
            if task is None:
                raise RuntimeError(f"任务 {task_id} 不存在")
            if not task.enabled:
                raise TaskDisabled()
            task.last_run_at = datetime.now()
            task.last_error = None
            task.last_run_count = (task.last_run_count or 0) + 1
            session.commit()
        try:
            self._assert_enabled(task_id)
            items = self.adapter.search(task.keyword)
        except Exception as exc:
            if isinstance(exc, TaskDisabled):
                raise
            self._record_error(task_id, f"抓取失败: {exc}")
            raise
        self._assert_enabled(task_id)
        stats["found"] = len(items)
        logger.info("任务 %s 搜索命中 %s 条", task_id, len(items))
        batch_rows: list[dict] = []
        pending: list[tuple] = []  # (task, listing, old_price, is_renotify)
        seen_ids: set[int] = set()
        with self._session_factory() as session:
            task = session.get(WatchTask, task_id)
            try:
                for data in items:
                    self._assert_enabled(task_id)
                    if task.max_price and data.price > task.max_price:
                        logger.info("任务 %s 跳过（超最高价 %s）: %s ¥%s", task_id, task.max_price, data.title[:30], data.price)
                        continue
                    if task.min_price and data.price < task.min_price:
                        logger.info("任务 %s 跳过（低于价格下限 %s）: %s ¥%s", task_id, task.min_price, data.title[:30], data.price)
                        continue
                    if self._excluded(task, data.title):
                        logger.info("任务 %s 跳过（命中排除词）: %s", task_id, data.title[:40])
                        continue
                    old_price: Optional[float] = None
                    existing = (
                        session.query(Listing)
                        .filter(
                            Listing.platform == task.platform,
                            Listing.external_id == data.external_id,
                            Listing.task_id == task.id,
                        )
                        .first()
                    )
                    if existing is not None and abs(existing.price - data.price) > 0.001:
                        old_price = existing.price
                    was_not_seen = bool(
                        existing is not None and existing.status == "not_seen_recently"
                    )
                    listing, is_new = self._upsert_listing(session, task, data)
                    seen_ids.add(listing.id)
                    if self._is_blocked(session, listing):
                        self._assert_requirements_current(task)
                        session.commit()
                        continue
                    listing.status = "active"
                    listing.missed_count = 0
                    # Do not hold SQLite's write lock across browser/model/network calls;
                    # task toggles must remain able to persist while a run is active.
                    self._assert_requirements_current(task)
                    session.commit()
                    if is_new:
                        stats["new"] += 1
                        logger.info("任务 %s 新品 %s：%s ¥%s", task_id, data.external_id, data.title[:30], data.price)
                        if task.fetch_detail:
                            self._assert_enabled(task_id)
                            self._fetch_detail(session, listing)
                            self._assert_enabled(task_id)
                        if self._is_blocked(session, listing):
                            self._assert_requirements_current(task)
                            session.commit()
                            continue
                        if not self._requirement_pass(session, listing, task):
                            logger.info("任务 %s 需求不匹配，不收录：%s", task_id, listing.title[:30])
                            self._assert_requirements_current(task)
                            session.commit()
                            continue
                        self._condition_analysis(session, listing, task)
                        if self._condition_gate_fails(task, listing):
                            logger.info("任务 %s 品相分低于门槛，不收录：%s", task_id, listing.title[:30])
                            self._assert_requirements_current(task)
                            session.commit()
                            continue
                        self._seller_check(session, listing, task)
                        if self._is_blocked(session, listing):
                            self._assert_requirements_current(task)
                            session.commit()
                            continue
                        batch_rows.append(
                            self._batch_row(listing, task.condition_requirement or "")
                        )
                        pending.append((task, listing, old_price, False))
                    else:
                        changed = old_price is not None or was_not_seen
                        if changed:
                            stats["reevaluated"] += 1
                            logger.info("任务 %s 重评 %s：%s（价格变化或重新上架）", task_id, data.external_id, data.title[:30])
                            if not self._requirement_pass(session, listing, task):
                                self._assert_requirements_current(task)
                                session.commit()
                                continue
                            self._condition_analysis(session, listing, task)
                            if self._condition_gate_fails(task, listing):
                                self._assert_requirements_current(task)
                                session.commit()
                                continue
                            self._seller_check(session, listing, task)
                            if self._is_blocked(session, listing):
                                self._assert_requirements_current(task)
                                session.commit()
                                continue
                            batch_rows.append(
                                self._batch_row(listing, task.condition_requirement or "")
                            )
                            pending.append((task, listing, old_price, True))
                        else:
                            if self._backfill(session, listing, task):
                                stats["backfilled"] += 1
                            if self._is_blocked(session, listing) or listing.requirement_match is False:
                                self._assert_requirements_current(task)
                                session.commit()
                                continue
                            if self._retry_failed_notifications(session, task, listing):
                                stats["notified"] += 1
                    self._assert_requirements_current(task)
                    session.commit()
                # 搜索结果有覆盖上限；这里只记录“近期未检索到”，不推断真实下架。
                for other in (
                    session.query(Listing)
                    .filter(Listing.task_id == task.id)
                    .filter(~Listing.id.in_(seen_ids))
                ):
                    other.missed_count = (other.missed_count or 0) + 1
                    if (
                        other.missed_count >= NOT_SEEN_THRESHOLD
                        and other.status != "not_seen_recently"
                    ):
                        other.status = "not_seen_recently"
                        stats["not_seen_recently"] += 1
                        logger.info("任务 %s 商品 %s 标记为近期未检索到", task_id, other.title[:30])
                self._assert_requirements_current(task)
                session.commit()
                # 批性价比：本批通过筛选的商品统一横向对比
                if batch_rows and self._value_client() is not None:
                    self._assert_enabled(task_id)
                    self._batch_value(session, batch_rows)
                    self._assert_requirements_current(task)
                    session.commit()
                # 通知：新品在批性价比后统一发出；重评仅满意度提高时发出
                for task, listing, old_price, is_renotify in pending:
                    self._assert_enabled(task_id)
                    if self._is_blocked(session, listing):
                        self._assert_requirements_current(task)
                        session.commit()
                        continue
                    self._update_verification_state(listing, task)
                    satisfaction = self._satisfaction(listing)
                    listing.satisfaction = satisfaction
                    if (
                        is_renotify
                        and listing.last_notified_satisfaction is not None
                        and satisfaction <= listing.last_notified_satisfaction
                    ):
                        continue
                    accepted = self._notify(
                        session,
                        task,
                        listing,
                        satisfaction,
                        old_price=old_price,
                        is_renotify=is_renotify,
                    )
                    if accepted:
                        stats["notified"] += 1
                self._assert_requirements_current(task)
                session.commit()
            except TaskRequirementsChanged:
                session.rollback()
                raise
            except TaskDisabled:
                # A paused batch never reaches the notification stage, where
                # verification flags are normally finalized. Persist them now.
                for interrupted in session.query(Listing).filter(Listing.id.in_(seen_ids)):
                    self._update_verification_state(interrupted, task)
                session.commit()
                raise
            except Exception as exc:
                task.last_error = f"处理商品时出错: {redact_secrets(exc)}"[:1000]
                session.commit()
                raise
            session.commit()
        return stats

    def _assert_enabled(self, task_id: Optional[int]) -> None:
        if task_id is None:
            return
        with self._session_factory() as session:
            task = session.get(WatchTask, task_id)
            if task is None or not task.enabled:
                raise TaskDisabled()

    def _interruptible_wait(self, task_id: int, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._assert_enabled(task_id)
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    def _upsert_listing(self, session, task: WatchTask, data: ListingData):
        listing = (
            session.query(Listing)
            .filter(
                Listing.platform == task.platform,
                Listing.external_id == data.external_id,
                Listing.task_id == task.id,
            )
            .first()
        )
        if listing is None:
            listing = Listing(
                platform=task.platform,
                external_id=data.external_id,
                title=data.title,
                price=data.price,
                url=data.url,
                image_urls=data.image_urls,
                seller=data.seller,
                location=data.location,
                published_at=data.published_at,
                task_id=task.id,
            )
            session.add(listing)
            session.flush()
            session.add(PriceSnapshot(listing_id=listing.id, price=data.price))
            return listing, True
        if abs(listing.price - data.price) > 0.001:
            listing.price = data.price
            session.add(PriceSnapshot(listing_id=listing.id, price=data.price))
        listing.last_seen_at = datetime.now()
        return listing, False

    def _is_blocked(self, session, listing: Listing) -> bool:
        if listing.blocked:
            return True
        if not listing.seller_uid:
            return False
        from goodprice.models import Seller

        seller = (
            session.query(Seller)
            .filter_by(platform=listing.platform, seller_uid=listing.seller_uid)
            .first()
        )
        if seller and seller.blocked:
            listing.blocked = True
            return True
        return False

    def _fetch_detail(self, session, listing: Listing, force: bool = False) -> None:
        if not listing.url:
            return
        if not force and listing.description:
            return
        try:
            detail = self.adapter.fetch_detail(listing.url)
        except Exception as exc:
            logger.warning("详情抓取失败，保留待核验: %s", redact_secrets(exc))
            return
        if detail.description:
            listing.description = detail.description
        merged = [u for u in (listing.image_urls or []) if is_product_image(u)]
        for url in detail.image_urls:
            if url not in merged and is_product_image(url):
                merged.append(url)
        listing.image_urls = merged[:8]
        if detail.variants:
            listing.variants = detail.variants[:6]
        if detail.seller_uid:
            listing.seller_uid = detail.seller_uid
            listing.seller_name = detail.seller_name or listing.seller_name
            listing.seller_risk = {
                "credit_label": detail.credit_label,
                "positive_rate": detail.positive_rate,
                "sold_count": detail.sold_count,
            }

    def reanalyze_listing(self, listing_id: int) -> dict[str, Any]:
        """手动重新分析单个商品：重抓详情并重跑需求/品相/性价比/卖家，不发送通知。"""
        stats = {"updated": 0}
        with self._session_factory() as session:
            listing = session.get(Listing, listing_id)
            if listing is None:
                raise RuntimeError(f"商品 {listing_id} 不存在")
            task = (
                session.get(WatchTask, listing.task_id)
                if listing.task_id is not None
                else WatchTask(keyword="", condition_requirement="", min_condition_score=0)
            )
            if listing.url:
                self._fetch_detail(session, listing, force=True)
            self._requirement_pass(session, listing, task)
            self._condition_analysis(session, listing, task)
            self._seller_check(session, listing, task)
            if self._value_client() is not None:
                self._batch_value(
                    session, [self._batch_row(listing, task.condition_requirement or "")]
                )
            self._assert_requirements_current(task)
            self._update_verification_state(listing, task)
            listing.satisfaction = self._satisfaction(listing)
            session.commit()
            stats["updated"] = 1
        return stats

    def _seller_check(self, session, listing: Listing, task: WatchTask) -> None:
        if not listing.seller_uid or self.seller_service is None:
            return
        self._assert_enabled(task.id)
        raw = dict(listing.seller_risk or {})
        try:
            seller = self.seller_service.ensure_fresh(
                task.platform,
                listing.seller_uid,
                nickname=listing.seller_name,
                credit_label=raw.get("credit_label"),
                session=session,
            )
        except TaskDisabled:
            raise
        except Exception as exc:
            detail = redact_secrets(exc)
            logger.warning("卖家信息获取失败，继续以待核验状态处理: %s", detail)
            raw["risk_level"] = "未知"
            raw["risk_reason"] = f"卖家信息获取失败（{detail}）"[:500]
            raw["nickname"] = listing.seller_name
            listing.seller_risk = raw
            return
        self._assert_enabled(task.id)
        from goodprice.services.seller_service import compute_risk

        level, reason = compute_risk(
            seller,
            credit_label=raw.get("credit_label"),
            detail_rate=raw.get("positive_rate"),
        )
        raw["risk_level"] = level
        raw["risk_reason"] = reason
        if seller is not None:
            raw["positive_count"] = seller.positive_count
            raw["total_count"] = seller.total_count
            raw["tags"] = seller.tags or []
        raw["nickname"] = listing.seller_name or (seller.nickname if seller else None)
        listing.seller_risk = raw

    def _assert_requirements_current(self, task: WatchTask) -> None:
        self._assert_enabled(task.id)
        if task.id is None:
            return
        with self._session_factory() as session:
            current = session.get(WatchTask, task.id)
            if current is None:
                raise TaskDisabled()
            if (current.condition_requirement or "").strip() != (task.condition_requirement or "").strip():
                raise TaskRequirementsChanged()

    def _requirement_pass(self, session, listing: Listing, task: WatchTask) -> bool:
        self._assert_requirements_current(task)
        requirement = (task.condition_requirement or "").strip()
        listing.requirement_match = None
        listing.requirement_reason = "未配置需求或文本模型，待核验"
        listing.requirement_input_hash = requirement_input_hash(
            listing.title, listing.description or "", requirement
        )
        capacity = check_capacity_requirements(listing.title, listing.description or "", requirement)
        # A definite capacity failure blocks even when detail/model is unavailable.
        # Unknown capacity cannot be promoted to a match by the model.
        if capacity is not None and capacity["matched"] is not True:
            listing.requirement_match = capacity["matched"]
            listing.requirement_reason = capacity["reason"]
            return capacity["matched"] is not False
        if task.fetch_detail and not listing.description:
            listing.requirement_reason = "商品详情缺失，需求待核验"
            return True
        if not requirement or not self.llm.enabled:
            return True
        try:
            verdict = self.llm.analyze_requirement(
                title=listing.title,
                description=listing.description or "",
                requirement=requirement,
            )
            self._assert_requirements_current(task)
        except (TaskDisabled, TaskRequirementsChanged):
            raise
        except Exception as exc:
            logger.warning("需求分析失败，不拦截: %s", redact_secrets(exc))
            listing.requirement_reason = f"需求分析失败，未过滤（{redact_secrets(exc)}）"[:500]
            return True
        listing.requirement_match = verdict["matched"]
        listing.requirement_reason = verdict["reason"]
        return verdict["matched"] is not False

    def _condition_analysis(self, session, listing: Listing, task: WatchTask) -> None:
        if not self.vision.enabled:
            return
        listing.condition_score = None
        valid = [u for u in (listing.image_urls or []) if is_product_image(u)]
        if not valid:
            listing.condition_detail = {"error": "无有效商品图"}
            return
        last_exc = None
        for _attempt in range(2):
            try:
                self._assert_enabled(task.id)
                verdict = self.vision.analyze_condition(
                    title=listing.title,
                    price=listing.price,
                    description=listing.description or "",
                    requirement=task.condition_requirement or "",
                    image_urls=valid,
                )
                self._assert_enabled(task.id)
            except TaskDisabled:
                raise
            except Exception as exc:
                last_exc = exc
                continue
            listing.condition_score = verdict["condition_score"]
            listing.condition_detail = verdict
            return
        listing.condition_detail = {"error": redact_secrets(last_exc)[:200]}

    def _condition_gate_fails(self, task: WatchTask, listing: Listing) -> bool:
        return bool(
            task.min_condition_score
            and listing.condition_score is not None
            and listing.condition_score < task.min_condition_score
        )

    def _excluded(self, task: WatchTask, title: str) -> bool:
        """排除词按空白/中英文逗号拆分，标题命中任一即跳过。"""
        words = (
            (task.exclude_words or "")
            .replace("，", " ")
            .replace(",", " ")
            .split()
        )
        return any(word and word in (title or "") for word in words)

    def _batch_row(self, listing: Listing, requirement: str = "") -> dict:
        defects = []
        if isinstance(listing.condition_detail, dict):
            defects = listing.condition_detail.get("defects") or []
        risk = None
        if isinstance(listing.seller_risk, dict):
            risk = listing.seller_risk.get("risk_level")
        return {
            "listing_id": listing.id,
            "external_id": listing.external_id,
            "title": listing.title,
            "price": listing.price,
            "condition_score": listing.condition_score,
            "defects": defects,
            "seller_risk": risk,
            "requirement": requirement,
        }

    def _value_client(self):
        """批量性价比只依赖文字，优先用文本 LLM，未配置时退回视觉模型。"""
        for client in (self.llm, self.vision):
            if getattr(client, "enabled", False):
                return client
        return None

    def _drop_pct(self, listing: Listing) -> float:
        return drop_pct_from_snapshots(listing)

    def _satisfaction(self, listing: Listing) -> float:
        return compute_satisfaction(
            listing,
            vision_enabled=self.vision.enabled,
            price_drop_pct=self._drop_pct(listing),
        )

    def _batch_value(self, session, rows: list[dict]) -> None:
        client = self._value_client()
        if client is None:
            return
        for row in rows:
            listing = session.get(Listing, row["listing_id"])
            if listing is not None:
                listing.value_score = None
                listing.value_batch_at = None
                listing.best_of_batch = False
        try:
            result = client.analyze_batch_value(rows[:MAX_BATCH_VALUE_ITEMS])
        except TaskDisabled:
            raise
        except Exception as exc:
            logger.warning("批量性价比分析失败，跳过: %s", redact_secrets(exc))
            return
        now = datetime.now()
        scores = result.get("scores") or {}
        best = result.get("best")
        logger.info("批量性价比完成：%s 个商品，本批最优 %s", len(rows), best)
        for row in rows:
            listing = session.get(Listing, row["listing_id"])
            if listing is None:
                continue
            score = scores.get(row["external_id"])
            if score is not None:
                listing.value_score = score
                listing.value_batch_at = now
                listing.best_of_batch = best == row["external_id"]

    def _backfill(self, session, listing: Listing, task: WatchTask) -> bool:
        changed = False
        requirement = (task.condition_requirement or "").strip()
        if task.fetch_detail and not listing.description:
            self._assert_enabled(task.id)
            self._fetch_detail(session, listing)
            self._assert_enabled(task.id)
            if self._is_blocked(session, listing):
                return False
        expected_hash = requirement_input_hash(listing.title, listing.description or "", requirement)
        stale = listing.requirement_input_hash != expected_hash
        if stale:
            listing.condition_score = None
            listing.condition_detail = None
            listing.value_score = None
            listing.value_batch_at = None
            listing.best_of_batch = False
        if stale or (requirement and self.llm.enabled and listing.requirement_match is None):
            self._requirement_pass(session, listing, task)
            changed = listing.requirement_match is not None
        if listing.requirement_match is not False and self.vision.enabled and listing.condition_score is None:
            try:
                self._assert_enabled(task.id)
                verdict = self.vision.analyze_condition(
                    title=listing.title,
                    price=listing.price,
                    description=listing.description or "",
                    requirement=requirement,
                    image_urls=listing.image_urls,
                )
                self._assert_enabled(task.id)
                listing.condition_score = verdict["condition_score"]
                listing.condition_detail = verdict
                changed = True
            except TaskDisabled:
                raise
            except Exception as exc:
                logger.warning("回填品相分析失败: %s", redact_secrets(exc))
        self._assert_requirements_current(task)
        self._update_verification_state(listing, task)
        listing.satisfaction = self._satisfaction(listing)
        return changed

    def _update_verification_state(self, listing: Listing, task: WatchTask) -> None:
        """记录筛选依据中缺失的部分；缺失不会阻止方案 A 的提醒。"""
        reasons: list[str] = []
        if task.fetch_detail and not listing.description:
            reasons.append("商品详情缺失，配置及卖家信息待核验")
        requirement = (task.condition_requirement or "").strip()
        if requirement and listing.requirement_match is None:
            reasons.append(
                "需求分析未完成（文本模型未启用）"
                if not self.llm.enabled
                else "需求分析失败或结果不完整"
            )
            if listing.requirement_reason:
                reasons.append(listing.requirement_reason)
        condition_error = ""
        if isinstance(listing.condition_detail, dict):
            condition_error = listing.condition_detail.get("error") or ""
        if not self.vision.enabled:
            reasons.append("品相分析未完成（视觉模型未启用）")
        elif listing.condition_score is None or condition_error:
            reasons.append("品相分析失败或结果不完整")
        if listing.value_score is None:
            reasons.append("性价比分析未完成")
        risk_level = (
            listing.seller_risk.get("risk_level")
            if isinstance(listing.seller_risk, dict)
            else None
        )
        if not listing.seller_uid:
            reasons.append("卖家信息不全（未取得卖家唯一标识）")
        elif not risk_level or risk_level == "未知":
            reasons.append("卖家信息不全（风险未核实）")
        listing.needs_verification = bool(reasons)
        listing.verification_reasons = reasons

    def _notify(
        self,
        session,
        task: WatchTask,
        listing: Listing,
        satisfaction: float,
        old_price: Optional[float] = None,
        is_renotify: bool = False,
    ) -> bool:
        def _fmt(value: float) -> str:
            return format(value, "g")

        lines = []
        if listing.needs_verification:
            reasons = "；".join(listing.verification_reasons or ["分析依据不完整"])
            lines.append(f"状态：待核验（{reasons}）")
        lines.append(f"价格：{_fmt(listing.price)} 元")
        drop_pct = self._drop_pct(listing)
        if drop_pct >= 0.05:
            lines.append(f"较首见降价 {drop_pct:.0%}")
        if is_renotify and old_price is not None:
            lines.append(f"价格更新重推：{_fmt(old_price)} → {_fmt(listing.price)} 元")
        if listing.requirement_match is not None:
            status = "是" if listing.requirement_match else "否"
            reason = listing.requirement_reason or ""
            line = f"需求匹配：{status}"
            if reason:
                line += f"（{reason}）"
            lines.append(line)
        if listing.condition_score is not None:
            lines.append(f"品相分：{listing.condition_score}")
        elif self.vision.enabled:
            err = ""
            if isinstance(listing.condition_detail, dict):
                err = listing.condition_detail.get("error") or ""
            lines.append(f"品相分：未评估（{err or '分析失败'}）")
        else:
            lines.append("品相分：视觉模型未启用，未评估")
        if listing.value_score is not None:
            lines.append(f"性价比：{listing.value_score}/10")
        else:
            lines.append("性价比：未评估")
        if listing.best_of_batch:
            lines.append("本批最优")
        if listing.variants:
            parts = " · ".join(
                f"{v.get('name')} {_fmt(v.get('price'))} 元" for v in listing.variants[:6]
            )
            lines.append(f"规格：{parts}")
        if listing.seller_risk:
            risk = listing.seller_risk
            name = risk.get("nickname") or "卖家"
            level = risk.get("risk_level")
            reason = risk.get("risk_reason") or ""
            rate = risk.get("positive_rate")
            rate_txt = f"好评率 {rate * 100:.0f}%" if isinstance(rate, (int, float)) else ""
            lines.append(f"卖家：{name} {rate_txt} · 风险{level}（{reason}）")
        if isinstance(listing.condition_detail, dict):
            extra = listing.condition_detail.get("reason", "")
            if extra:
                lines.append(extra)
        message = NotificationMessage(
            title=("[待核验] " if listing.needs_verification else "")
            + f"[{task.keyword}] {listing.title}",
            content="\n".join(lines),
            url=listing.url,
        )
        return self._deliver_message(session, task, listing, message, satisfaction)

    def _deliver_message(
        self,
        session,
        task: WatchTask,
        listing: Listing,
        message: NotificationMessage,
        satisfaction: float,
        channels: Optional[set[str]] = None,
        event_key: Optional[str] = None,
    ) -> bool:
        event_key = event_key or hashlib.sha256(
            f"{message.title}\0{message.content}\0{message.url}".encode("utf-8")
        ).hexdigest()
        accepted_any = False
        for channel, notifier in self.notifiers:
            if channels is not None and channel not in channels:
                continue
            self._assert_requirements_current(task)
            accepted = (
                session.query(Notification)
                .filter_by(
                    listing_id=listing.id,
                    channel=channel,
                    event_key=event_key,
                    status="accepted" if channel != "log" else "logged",
                )
                .first()
            )
            if accepted is not None:
                continue
            failed_count = (
                session.query(Notification)
                .filter_by(
                    listing_id=listing.id,
                    channel=channel,
                    event_key=event_key,
                    status="failed",
                )
                .count()
            )
            if channel != "log" and failed_count >= MAX_NOTIFICATION_ATTEMPTS:
                continue
            attempt = failed_count + 1
            # Persist analysis before releasing control to an external notifier.
            # A concurrent task edit can then invalidate it without being overwritten.
            self._assert_requirements_current(task)
            session.commit()
            try:
                notifier.send(message)
                status = "logged" if channel == "log" else "accepted"
                logger.info(
                    "通知[%s] %s：%s",
                    channel,
                    "已写本地日志" if channel == "log" else "服务端已接受",
                    listing.title[:30],
                )
                session.add(
                    Notification(
                        listing_id=listing.id,
                        task_id=task.id,
                        channel=channel,
                        status=status,
                        event_key=event_key,
                        attempt=attempt,
                        title=message.title,
                        content=message.content,
                    )
                )
                if channel != "log":
                    accepted_any = True
            except Exception as exc:
                detail = redact_secrets(exc)
                logger.warning("通知[%s]失败（第 %s/%s 次）: %s", channel, attempt, MAX_NOTIFICATION_ATTEMPTS, detail)
                session.add(
                    Notification(
                        listing_id=listing.id,
                        task_id=task.id,
                        channel=channel,
                        status="failed",
                        event_key=event_key,
                        attempt=attempt,
                        detail=detail[:1000],
                        title=message.title,
                        content=message.content,
                    )
                )
            with self._session_factory() as current_session:
                current = current_session.get(WatchTask, task.id) if task.id is not None else task
                changed = current is None or (current.condition_requirement or "").strip() != (task.condition_requirement or "").strip()
            if changed:
                for row in session.new:
                    if isinstance(row, Notification) and row.status == "failed":
                        row.status = "superseded"
            session.commit()
            if changed:
                if accepted_any:
                    listing.notified_at = datetime.now()
                    listing.last_notified_satisfaction = satisfaction
                    session.commit()
                raise TaskRequirementsChanged()
        if accepted_any:
            listing.notified_at = datetime.now()
            listing.last_notified_satisfaction = satisfaction
            session.commit()
        return accepted_any

    def _retry_failed_notifications(
        self, session, task: WatchTask, listing: Listing
    ) -> bool:
        failed = (
            session.query(Notification)
            .filter_by(listing_id=listing.id, status="failed")
            .filter(Notification.event_key != "")
            .order_by(Notification.id.desc())
            .all()
        )
        if not failed:
            return False
        latest_event = failed[0].event_key
        rows = [row for row in failed if row.event_key == latest_event]
        retry_channels = {
            row.channel
            for row in rows
            if row.channel != "log"
            and not (
                session.query(Notification)
                .filter_by(
                    listing_id=listing.id,
                    channel=row.channel,
                    event_key=latest_event,
                    status="accepted",
                )
                .first()
            )
            and (
                session.query(Notification)
                .filter_by(
                    listing_id=listing.id,
                    channel=row.channel,
                    event_key=latest_event,
                    status="failed",
                )
                .count()
                < MAX_NOTIFICATION_ATTEMPTS
            )
        }
        if not retry_channels:
            return False
        row = rows[0]
        return self._deliver_message(
            session,
            task,
            listing,
            NotificationMessage(title=row.title, content=row.content, url=listing.url),
            listing.satisfaction,
            channels=retry_channels,
            event_key=latest_event,
        )

    def _record_error(self, task_id: int, message: str) -> None:
        with self._session_factory() as session:
            task = session.get(WatchTask, task_id)
            if task:
                task.last_error = redact_secrets(message)[:1000]
                session.commit()

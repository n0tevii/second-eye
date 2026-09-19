from goodprice.services.task_service import TaskService


def _service(session_factory):
    return TaskService(session_factory)


def test_create_and_get(session_factory):
    service = _service(session_factory)
    task = service.create_task(
        {"keyword": "iPhone 13", "max_price": "3000", "min_condition_score": "6"}
    )
    assert task.id is not None
    loaded = service.get_task(task.id)
    assert loaded.keyword == "iPhone 13"
    assert loaded.max_price == 3000.0
    assert loaded.min_condition_score == 6


def test_list_and_enabled(session_factory):
    service = _service(session_factory)
    service.create_task({"keyword": "a"})
    service.create_task({"keyword": "b"})
    assert len(service.list_tasks()) == 2
    assert len(service.enabled_tasks()) == 2


def test_toggle(session_factory):
    service = _service(session_factory)
    task = service.create_task({"keyword": "a"})
    toggled = service.toggle_task(task.id)
    assert toggled.enabled is False
    assert service.get_task(task.id).enabled is False


def test_delete(session_factory):
    service = _service(session_factory)
    task = service.create_task({"keyword": "a"})
    assert service.delete_task(task.id) is True
    assert service.get_task(task.id) is None
    assert service.delete_task(999) is False


def test_update_task(session_factory):
    service = TaskService(session_factory)
    task = service.create_task({"keyword": "a"})
    updated = service.update_task(task.id, {"keyword": "b", "max_price": "500", "condition_requirement": "屏幕完好"})
    assert updated.keyword == "b"
    assert updated.max_price == 500.0
    assert updated.condition_requirement == "屏幕完好"
    assert service.update_task(999, {"keyword": "x"}) is None


def test_requirement_edit_invalidates_analysis_but_preserves_history(session_factory):
    from goodprice.models import Listing, Notification, PriceSnapshot

    service = TaskService(session_factory)
    task = service.create_task({'keyword': 'Mac Studio', 'condition_requirement': 'SSD至少1TB'})
    other = service.create_task({'keyword': 'other'})
    with session_factory() as session:
        item = Listing(platform='xianyu', external_id='1', task_id=task.id, title='128GB+1TB',
                       price=31000, url='https://example.invalid/1', requirement_match=True,
                       requirement_reason='旧结论', condition_score=9, value_score=8,
                       satisfaction=90, best_of_batch=True)
        untouched = Listing(platform='xianyu', external_id='2', task_id=other.id, title='other',
                            price=1, url='', requirement_match=True)
        session.add_all([item, untouched])
        session.flush()
        session.add_all([
            PriceSnapshot(listing_id=item.id, price=item.price),
            Notification(listing_id=item.id, task_id=task.id, channel='test', status='accepted'),
            Notification(listing_id=item.id, task_id=task.id, channel='other', status='failed'),
        ])
        session.commit()
        item_id, other_id = item.id, untouched.id
    service.update_task(task.id, {'condition_requirement': 'SSD至少2TB'})
    with session_factory() as session:
        item = session.get(Listing, item_id)
        assert item.requirement_match is None
        assert item.condition_score is None and item.value_score is None
        assert not item.best_of_batch and item.satisfaction == 0
        assert item.needs_verification and '需求' in item.requirement_reason
        assert session.get(Listing, other_id).requirement_match is True
        assert session.query(PriceSnapshot).count() == 1
        assert [n.status for n in session.query(Notification).order_by(Notification.id)] == ['accepted', 'superseded']


def test_unchanged_requirement_or_interval_edit_preserves_analysis(session_factory):
    from goodprice.models import Listing
    service = TaskService(session_factory)
    task = service.create_task({'keyword': 'k', 'condition_requirement': '屏幕完好'})
    with session_factory() as session:
        session.add(Listing(platform='xianyu', external_id='1', task_id=task.id, title='x',
                            price=1, url='', requirement_match=True, satisfaction=75))
        session.commit()
    service.update_task(task.id, {'condition_requirement': '屏幕完好', 'interval_minutes': 30})
    with session_factory() as session:
        assert session.query(Listing).one().requirement_match is True

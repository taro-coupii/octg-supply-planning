import datetime


def today() -> datetime.date:
    """Overridden by tests via monkeypatch to keep overdue tests time-independent."""
    return datetime.date.today()


def is_overdue(ros_date: datetime.date, as_of: datetime.date) -> bool:
    """True when ros_date's month is strictly before as_of's month."""
    return (ros_date.year, ros_date.month) < (as_of.year, as_of.month)

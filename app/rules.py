"""Бизнес-правила сервиса заявок: без обращения к базе данных и к сети."""
from datetime import datetime

STATUSES = ("new", "in_progress", "closed")
TRANSITIONS = {"new": {"in_progress"}, "in_progress": {"closed"}, "closed": set()}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def reaction_minutes(created_at: datetime, first_staff_reply_at):
    """Срок реакции в минутах: от создания заявки до первого ответа сотрудника."""
    if first_staff_reply_at is None:
        return None
    return (first_staff_reply_at - created_at).total_seconds() / 60.0


def norm_violated(created_at: datetime, first_staff_reply_at, norm_minutes: int, now: datetime) -> bool:
    """Норматив нарушен, если ответ пришёл позже нормы или ответа нет и норма уже истекла."""
    reaction = reaction_minutes(created_at, first_staff_reply_at)
    if reaction is None:
        return (now - created_at).total_seconds() / 60.0 > norm_minutes
    return reaction > norm_minutes

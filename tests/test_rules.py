from datetime import datetime, timedelta, timezone

from app import rules

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


def test_reaction_time_is_minutes_to_first_staff_reply():
    assert rules.reaction_minutes(T0, T0 + timedelta(minutes=45)) == 45


def test_reaction_time_is_none_without_reply():
    assert rules.reaction_minutes(T0, None) is None


def test_norm_not_violated_when_reply_within_norm():
    assert rules.norm_violated(T0, T0 + timedelta(minutes=60), 60, T0 + timedelta(days=5)) is False


def test_norm_violated_when_reply_after_norm():
    assert rules.norm_violated(T0, T0 + timedelta(minutes=61), 60, T0 + timedelta(days=5)) is True


def test_norm_violated_when_no_reply_and_norm_expired():
    assert rules.norm_violated(T0, None, 60, T0 + timedelta(minutes=90)) is True


def test_norm_not_violated_when_no_reply_but_norm_not_expired():
    assert rules.norm_violated(T0, None, 60, T0 + timedelta(minutes=30)) is False


def test_status_flow_new_to_in_progress_to_closed():
    assert rules.can_transition("new", "in_progress")
    assert rules.can_transition("in_progress", "closed")


def test_status_cannot_skip_or_reopen():
    assert not rules.can_transition("new", "closed")
    assert not rules.can_transition("closed", "new")

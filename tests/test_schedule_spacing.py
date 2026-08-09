"""No two scheduled analytics jobs may land on the same hour.

The six batch jobs share one 967MB VPS. They are serialized by the mutex in
collect/runner.py so overlapping can no longer OOM the process, but a job that
has to wait on the mutex is still a job running late, and lateness compounds:
a queued forecast bundle delays everything behind it. Spacing keeps the mutex
an insurance policy rather than the normal path.

This test exists because the spacing was set by hand on 2026-07-28 and, in
that very edit, two jobs were assigned the same hour (map_propose Monday 09:00
and paper_export Sunday 09:00). Those happen to be on mutually exclusive
weekdays, so they could never actually collide -- but relying on a day-of-week
argument to justify a same-hour clash is precisely the kind of reasoning that
stops being true after an unrelated edit.
"""

from __future__ import annotations

import itertools

import pytest
import yaml

from lab.util import PROJECT_ROOT


def _cron_hours(expr: str) -> list[int]:
    """Hours a 5-field crontab expression fires at. Only the literal and
    comma-list forms this config uses are supported -- a `*` or step in the
    hour field would mean an always-on job, which none of these are and which
    this test should loudly reject rather than silently pass."""
    hour_field = expr.split()[1]
    if not all(part.isdigit() for part in hour_field.split(",")):
        raise AssertionError(
            f"unsupported hour field {hour_field!r} in cron {expr!r} -- a "
            "wildcard/step hour cannot be spaced against anything"
        )
    return [int(part) for part in hour_field.split(",")]


def _scheduled_jobs() -> dict[str, str]:
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    return {
        "forecast": cfg["schedule"]["forecast_cron"],
        "shadow": cfg["schedule"]["shadow_cron"],
        "report": cfg["schedule"]["report_cron"],
        # No "learn": it left this process on 2026-08-05 for its own systemd
        # timer, so its schedule is not this file's to space (and not this
        # process's memory budget to share).
        "map_propose": cfg["cross_venue"]["propose_cron"],
        "pmxt_verify": cfg["cross_venue"]["pmxt_verify_cron"],
        "paper_export": cfg["paper_export"]["cron"],
    }


def test_no_two_jobs_share_an_hour():
    slots: list[tuple[int, str]] = []
    for name, expr in _scheduled_jobs().items():
        slots.extend((hour, name) for hour in _cron_hours(expr))

    by_hour: dict[int, list[str]] = {}
    for hour, name in slots:
        by_hour.setdefault(hour, []).append(name)

    clashes = {h: names for h, names in by_hour.items() if len(names) > 1}
    assert not clashes, f"jobs sharing an hour: {clashes}"


def test_every_job_keeps_a_two_hour_margin():
    """Two hours is the working margin: the forecast bundle's report render
    alone has run for minutes-to-hours as the resolved set grows, and the
    margin has to absorb that growth without jobs starting to queue."""
    slots = sorted(
        (hour, name)
        for name, expr in _scheduled_jobs().items()
        for hour in _cron_hours(expr)
    )
    tight = [
        (slots[i], slots[i + 1])
        for i in range(len(slots) - 1)
        if slots[i + 1][0] - slots[i][0] < 2
    ]
    assert not tight, f"jobs less than 2h apart: {tight}"


def test_forecast_bundle_leads_the_day_with_a_wide_margin():
    """The nightly bundle is the heaviest and most duration-variable job
    (forecast + eval + report + publish + ledger push). Nothing should be
    scheduled close behind it."""
    slots = sorted(
        (hour, name)
        for name, expr in _scheduled_jobs().items()
        for hour in _cron_hours(expr)
    )
    forecast_hour = next(h for h, n in slots if n == "forecast")
    following = [(h, n) for h, n in slots if h > forecast_hour]
    assert following, "nothing scheduled after the forecast bundle -- check the fixture"
    next_hour, next_name = following[0]
    assert next_hour - forecast_hour >= 5, (
        f"{next_name} starts {next_hour - forecast_hour}h after the forecast "
        "bundle; it needs at least 5h of room"
    )


@pytest.mark.parametrize("name,expr", sorted(_scheduled_jobs().items()))
def test_cron_expressions_are_well_formed(name, expr):
    fields = expr.split()
    assert len(fields) == 5, f"{name}: expected 5 cron fields, got {len(fields)} in {expr!r}"
    assert _cron_hours(expr), f"{name}: no hours parsed from {expr!r}"


def test_shadow_is_evaluated_daily_as_the_brief_specifies():
    """CLAUDE.md section 8: "Entry rule (evaluated daily on liquid tier, using
    M4)". It ran weekly from inception instead, which cost 7x the entry
    opportunities and left the portfolio with 3 resolved trades after a month --
    and PAP H2 uses that portfolio's realized, fee-netted P&L as its
    net-of-cost proxy, so the drift was load-bearing, not cosmetic.
    """
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    day_of_week = cfg["schedule"]["shadow_cron"].split()[4]
    assert day_of_week == "*", (
        f"shadow_cron restricts the day-of-week to {day_of_week!r}; the brief says daily"
    )
    assert cfg["schedule"]["control"]["shadow_max_age_hours"] <= 72, (
        "the catch-up window still assumes a weekly job"
    )

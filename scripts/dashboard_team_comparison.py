"""Explicit team forecast/full-month and RAW/same-period comparisons."""
from __future__ import annotations

import datetime as dt

from dashboard_kpi_cards import mom, previous_cutoff
from refresh_live_daily_from_duckdb import fmt_won

KEYS = ('ad_gen', 'ad_int', 'live')


def validate_comparisons(raw: dict, forecast: dict) -> None:
    as_of = dt.date.fromisoformat(raw['as_of'])
    for prior, cutoff, total in (
        (forecast.get('previous_actual'), as_of.replace(day=1) - dt.timedelta(days=1), forecast.get('previous_total_won')),
        (raw.get('previous_same_period'), previous_cutoff(as_of), None),
    ):
        if prior is None:
            continue
        if prior.get('as_of') != cutoff.isoformat():
            raise ValueError('team comparison cutoff mismatch')
        fields = [k + '_won' for k in KEYS]
        if not any(k in prior for k in fields):
            continue  # Older total-only payloads cannot establish a team baseline.
        if not all(k in prior for k in fields):
            raise ValueError('team comparison incomplete components')
        values = [prior[k] for k in fields]
        if any(v is not None and (type(v) is not int or v < 0) for v in values):
            raise ValueError('team comparison invalid amount')
        expected = sum(values) if all(v is not None for v in values) else None
        if prior.get('total_won') != expected:
            raise ValueError('team comparison total mismatch')
        if prior is forecast.get('previous_actual'):
            if prior.get('source') != 'revenue.integrated_ssot' or total != expected:
                raise ValueError('team comparison actual provenance/total mismatch')


def baseline_attrs(prior: dict | None) -> str:
    if not prior or not all(k + '_won' in prior for k in KEYS):
        return ''
    return ' data-team-comparison="v1"' + ''.join(
        f' data-prior-{k.replace("_", "-")}-won="{prior[k + "_won"] if prior[k + "_won"] is not None else "unavailable"}"'
        for k in KEYS)


def comparison_row(kind: str, key: str, current: int | None, previous: int | None, cutoff: dt.date) -> str:
    label = '마감예상 전월 대비' if kind == 'forecast' else 'RAW 전월 동일기간'
    basis = '확정치' if kind == 'forecast' else 'RAW'
    period = f'{cutoff.month}/1~{cutoff.month}/{cutoff.day}'
    amount = '비교값 없음' if previous is None else fmt_won(previous)
    reason = ' · 전월 0원으로 증감률 산출 불가' if previous == 0 else ''
    return (f'<div class="r" data-team-mom="{kind}-{key}" data-comparison-as-of="{cutoff.isoformat()}">'
            f'<span>{label}<small style="display:block">{period} {basis} · {amount}{reason}</small></span>'
            f'<span>{mom(current, previous)}</span></div>')


def team_comparisons(raw: dict, forecast: dict, key: str) -> str:
    as_of = dt.date.fromisoformat(raw['as_of'])
    previous_actual = forecast.get('previous_actual') or {}
    previous_raw = raw.get('previous_same_period') or {}
    # Existing card CSS promotes the last row below the headline forecast amount.
    return (comparison_row('raw', key, raw[key + '_won'], previous_raw.get(key + '_won'), previous_cutoff(as_of))
            + comparison_row('forecast', key, forecast[key], previous_actual.get(key + '_won'),
                             as_of.replace(day=1) - dt.timedelta(days=1)))

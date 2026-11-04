import pytest

from app.services.system_runtime_status import _health_rollup_severity


def test_optional_integrity_evidence_does_not_hide_operational_health():
    diagnostic = dict(severity='unknown', status='no_evidence',
                      reason_codes=['no_evidence', 'reconciliation_unknown'],
                      problem_file_count=0, cleanup_candidate_count=0)
    assert _health_rollup_severity('reconciliation', diagnostic) == 'ok'
    assert diagnostic['severity'] == 'unknown'
    assert _health_rollup_severity('live', dict(severity='unknown', items=[])) == 'ok'


@pytest.mark.parametrize('domain', ['recorder', 'storage', 'cameras', 'retention'])
def test_missing_operational_evidence_stays_unknown(domain):
    assert _health_rollup_severity(domain, dict(severity='unknown')) == 'unknown'


@pytest.mark.parametrize('extra', [dict(severity='warning', problem_file_count=1),
                                dict(severity='error'), dict(partial=True),
                                dict(scan_limited=True), dict(cleanup_candidate_count=1),
                                dict(reason_codes=['no_evidence', 'transport_failed'])])
def test_integrity_faults_are_not_treated_as_unrequested(extra):
    diagnostic = dict(severity='unknown', status='no_evidence',
                      reason_codes=['no_evidence', 'reconciliation_unknown'])
    diagnostic.update(extra)
    assert _health_rollup_severity('reconciliation', diagnostic) != 'ok'

from typing import Optional
from unittest.mock import MagicMock

import pytest

from tmt.checks import CheckEvent
from tmt.cli import TmtExitCode
from tmt.result import (
    CheckResult,
    CheckResultInterpret,
    Result,
    ResultInterpret,
    ResultOutcome,
    aggregate_check_results,
    results_to_exit_code,
    )


@pytest.mark.parametrize(
    ('outcomes', 'expected_exit_code'),
    [
        # No test results found.
        (
            [],
            TmtExitCode.NO_RESULTS_FOUND
            ),
        # Errors occurred during test execution.
        (
            [
                ResultOutcome.PASS,
                ResultOutcome.FAIL,
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
                ResultOutcome.WARN,
                ResultOutcome.ERROR
                ],
            TmtExitCode.ERROR
            ),
        # There was a fail or warn identified, but no error.
        (
            [
                ResultOutcome.PASS,
                ResultOutcome.FAIL,
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
                ResultOutcome.WARN,
                ],
            TmtExitCode.FAIL
            ),
        # Tests were executed, and all reported the ``skip`` result.
        (
            [
                ResultOutcome.SKIP,
                ResultOutcome.SKIP,
                ],
            TmtExitCode.ALL_TESTS_SKIPPED
            ),
        # At least one test passed, there was no fail, warn or error.
        (
            [
                ResultOutcome.PASS,
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
                ],
            TmtExitCode.SUCCESS
            ),
        # An info is treated as a pass.
        (
            [
                ResultOutcome.INFO,
                ResultOutcome.SKIP,
                ],
            TmtExitCode.SUCCESS
            )
        ],
    # IDs copied from the specification:
    ids=(
        "No test results found",
        "Errors occurred during test execution",
        "There was a fail or warn identified, but no error",
        "Tests were executed, and all reported the ``skip`` result",
        "At least one test passed, there was no fail, warn or error",
        "An info is treated as a pass"
        )
    )
def test_result_to_exit_code(outcomes: list[ResultOutcome], expected_exit_code: int) -> None:
    assert results_to_exit_code([MagicMock(result=outcome) for outcome in outcomes]) \
        == expected_exit_code


@pytest.mark.parametrize(
    ('check_results', 'interpret', 'expected_outcome'),
    [
        # Empty results default to PASS
        ([], CheckResultInterpret.RESPECT, ResultOutcome.PASS),
        ([], CheckResultInterpret.INFO, ResultOutcome.PASS),
        ([], CheckResultInterpret.XFAIL, ResultOutcome.PASS),

        # Single check with single phase
        (
            [CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST)],
            CheckResultInterpret.RESPECT,
            ResultOutcome.PASS
            ),
        (
            [CheckResult(name="test1", result=ResultOutcome.FAIL, event=CheckEvent.AFTER_TEST)],
            CheckResultInterpret.RESPECT,
            ResultOutcome.FAIL
            ),

        # Single check with both phases
        (
            [
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.AFTER_TEST)
                ],
            CheckResultInterpret.RESPECT,
            ResultOutcome.PASS
            ),
        (
            [
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test1", result=ResultOutcome.FAIL, event=CheckEvent.AFTER_TEST)
                ],
            CheckResultInterpret.RESPECT,
            ResultOutcome.FAIL
            ),

        # Multiple different checks
        (
            [
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test2", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST)
                ],
            CheckResultInterpret.RESPECT,
            ResultOutcome.PASS
            ),
        (
            [
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test2", result=ResultOutcome.FAIL, event=CheckEvent.BEFORE_TEST)
                ],
            CheckResultInterpret.RESPECT,
            ResultOutcome.FAIL
            ),

        # Check with XFAIL interpretation
        (
            [
                CheckResult(name="test1", result=ResultOutcome.FAIL, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test1", result=ResultOutcome.FAIL, event=CheckEvent.AFTER_TEST)
                ],
            CheckResultInterpret.XFAIL,
            ResultOutcome.PASS
            ),
        (
            [
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.AFTER_TEST)
                ],
            CheckResultInterpret.XFAIL,
            ResultOutcome.FAIL
            ),

        # Check with INFO interpretation
        (
            [
                CheckResult(name="test1", result=ResultOutcome.FAIL, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.AFTER_TEST)
                ],
            CheckResultInterpret.INFO,
            ResultOutcome.INFO
            ),
        (
            [
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
                CheckResult(name="test1", result=ResultOutcome.PASS, event=CheckEvent.AFTER_TEST)
                ],
            CheckResultInterpret.INFO,
            ResultOutcome.INFO
            ),
        ],
    ids=[
        "empty-respect", "empty-info", "empty-xfail",
        "single-phase-pass", "single-phase-fail",
        "both-phases-pass", "both-phases-mixed",
        "different-checks-pass", "different-checks-mixed",
        "xfail-both-fail", "xfail-pass",
        "info-mixed", "info-pass"
        ]
    )
def test_aggregate_check_results(
        check_results: list[CheckResult],
        interpret: CheckResultInterpret,
        expected_outcome: ResultOutcome
        ) -> None:
    assert aggregate_check_results(check_results, interpret) == expected_outcome


@pytest.mark.parametrize(
    ('result_outcome',
     'interpret',
     'interpret_checks',
     'expected_outcome',
     'expected_note_contains'),
    [
        # Test RESPECT interpretation
        (
            ResultOutcome.PASS,
            ResultInterpret.RESPECT,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.PASS,
            None
            ),
        (
            ResultOutcome.FAIL,
            ResultInterpret.RESPECT,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.FAIL,
            "check 'check1' failed"  # Note is set when check fails
            ),

        # Test XFAIL interpretation
        (
            ResultOutcome.FAIL,
            ResultInterpret.XFAIL,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.PASS,
            "original result: fail"
            ),
        (
            ResultOutcome.PASS,
            ResultInterpret.XFAIL,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.FAIL,
            "original result: pass"
            ),

        # Test INFO interpretation
        (
            ResultOutcome.FAIL,
            ResultInterpret.INFO,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.INFO,
            "original result: fail"
            ),
        (
            ResultOutcome.PASS,
            ResultInterpret.INFO,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.INFO,
            "original result: pass"
            ),

        # Test WARN interpretation
        (
            ResultOutcome.PASS,
            ResultInterpret.WARN,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.WARN,
            "original result: pass"
            ),

        # Test ERROR interpretation
        (
            ResultOutcome.PASS,
            ResultInterpret.ERROR,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.ERROR,
            "original result: pass"
            ),

        # Test CUSTOM interpretation (should not modify result)
        (
            ResultOutcome.FAIL,
            ResultInterpret.CUSTOM,
            {"check1": CheckResultInterpret.RESPECT},
            ResultOutcome.FAIL,
            None
            ),
        ],
    ids=[
        "respect-pass", "respect-fail",
        "xfail-fail", "xfail-pass",
        "info-fail", "info-pass",
        "warn-pass",
        "error-pass",
        "custom-fail"
        ]
    )
def test_result_interpret_all_cases(
        result_outcome: ResultOutcome,
        interpret: ResultInterpret,
        interpret_checks: dict[str, CheckResultInterpret],
        expected_outcome: ResultOutcome,
        expected_note_contains: Optional[str]
        ) -> None:
    """Test all possible combinations of result interpretations"""
    result = Result(
        name="test-case",
        result=result_outcome,
        check=[
            CheckResult(name="check1", result=result_outcome, event=CheckEvent.BEFORE_TEST)
            ]
        )

    interpreted = result.interpret_result(interpret, interpret_checks)
    assert interpreted.result == expected_outcome

    if expected_note_contains:
        assert interpreted.note is not None
        assert expected_note_contains in interpreted.note
    else:
        assert interpreted.note is None


def test_result_interpret_check_phases() -> None:
    """Test the interpretation of check results with different phases"""
    result = Result(
        name="test-case",
        check=[
            CheckResult(name="check1", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST),
            CheckResult(name="check1", result=ResultOutcome.FAIL, event=CheckEvent.AFTER_TEST),
            CheckResult(name="check2", result=ResultOutcome.PASS, event=CheckEvent.BEFORE_TEST)
            ]
        )

    # Test with mixed interpretations
    interpret_checks = {
        "check1": CheckResultInterpret.RESPECT,
        "check2": CheckResultInterpret.INFO
        }

    interpreted = result.interpret_result(ResultInterpret.RESPECT, interpret_checks)
    assert interpreted.note is not None
    assert "check 'check1' failed" in interpreted.note
    assert "check 'check2'" not in interpreted.note  # check2 passed

    # Verify individual check results were interpreted
    assert interpreted.check[0].result == ResultOutcome.PASS  # check1 BEFORE_TEST
    assert interpreted.check[1].result == ResultOutcome.FAIL  # check1 AFTER_TEST
    assert interpreted.check[2].result == ResultOutcome.INFO  # check2 BEFORE_TEST (INFO)


def test_result_interpret_edge_cases() -> None:
    """Test edge cases in result interpretation"""
    # Test with no checks
    result = Result(name="test-case", result=ResultOutcome.FAIL)
    interpreted = result.interpret_result(ResultInterpret.RESPECT, {})
    assert interpreted.result == ResultOutcome.FAIL
    assert interpreted.note is None

    # Test with empty check list
    result = Result(name="test-case", result=ResultOutcome.FAIL, check=[])
    interpreted = result.interpret_result(ResultInterpret.RESPECT, {})
    assert interpreted.result == ResultOutcome.FAIL
    assert interpreted.note is None

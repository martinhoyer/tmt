import tmt.log
import tmt.result
import tmt.steps.execute
import tmt.utils
from tmt.frameworks import TestFramework, provides_framework
from tmt.result import ResultOutcome
from tmt.steps.execute import TEST_OUTPUT_FILENAME, TestInvocation


@provides_framework('pytest')
class Pytest(TestFramework):
    @classmethod
    def get_test_command(
        cls, invocation: 'TestInvocation', logger: tmt.log.Logger
    ) -> tmt.utils.ShellScript:
        # Construct the pytest command
        # This might need adjustments based on how pytest is typically invoked
        # and how options/arguments should be passed.
        script = invocation.test.test
        if invocation.test.path:
            script = f'{invocation.test.path.unrooted()}'
        return tmt.utils.ShellScript(f"pytest {script}")

    @classmethod
    def extract_results(
        cls,
        invocation: 'TestInvocation',
        results: list[
            tmt.result.Result
        ],  # This is for tmt-report-result, might not be directly used by pytest output
        logger: tmt.log.Logger,
    ) -> list[tmt.result.Result]:
        """
        Check result of a pytest test.
        Parse pytest output to determine the result.
        This will likely involve parsing JUnit XML output if pytest is configured to produce it,
        or parsing stdout for specific patterns.
        """
        assert invocation.return_code is not None
        note: list[str] = []

        # For now, a simple success/fail based on exit code.
        # This needs to be enhanced to parse pytest's rich output (e.g., JUnit XML).
        if invocation.return_code == 0:
            result_outcome = ResultOutcome.PASS
        elif invocation.return_code == 5:  # Exit code 5 means no tests were collected
            result_outcome = ResultOutcome.INFO
            note.append("No tests found by pytest.")
        else:
            result_outcome = ResultOutcome.FAIL

        log_path = invocation.relative_path / TEST_OUTPUT_FILENAME

        # If pytest is configured to produce a JUnit XML report, we should parse that.
        # For example, if 'results.xml' is the report:
        # junit_xml_path = invocation.path / 'results.xml'
        # if junit_xml_path.exists():
        #     # Parse junit_xml_path to get detailed results, sub-results, etc.
        #     # This is a placeholder for actual XML parsing logic.
        #     pass

        return [
            tmt.Result.from_test_invocation(
                invocation=invocation,
                result=result_outcome,
                log=[log_path],  # Potentially add JUnit XML path here
                note=note,
            )
        ]

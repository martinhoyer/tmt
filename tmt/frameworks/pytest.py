import shlex
from typing import TYPE_CHECKING

import tmt.log
import tmt.result
import tmt.utils
from tmt.base import DependencySimple
from tmt.frameworks import TestFramework, provides_framework
from tmt.result import ResultOutcome
from tmt.steps.execute import TEST_OUTPUT_FILENAME, TestInvocation

if TYPE_CHECKING:
    from tmt.base import Test


@provides_framework('pytest')
class Pytest(TestFramework):
    @classmethod
    def get_requirements(cls, test: 'Test', logger: tmt.log.Logger) -> list[DependencySimple]:
        return [DependencySimple('uv')]

    @classmethod
    def get_test_command(
        cls, invocation: 'TestInvocation', logger: tmt.log.Logger
    ) -> tmt.utils.ShellScript:
        script_path_str = invocation.test.test
        # The 'invocation.test.test' should be the path to the test script or directory
        # relative to the test's FMF file location. Pytest will be invoked from
        # within the test's specific work directory after 'discover'.

        command_parts = ["uvx"]

        # Access pytest_plugins from the invocation's phase data.
        # invocation.phase is an instance of ExecuteInternal plugin,
        # its 'data' attribute holds ExecuteInternalData.
        # Need to ensure that 'pytest_plugins' attribute exists and is populated.
        if (
            hasattr(invocation.phase.data, 'pytest_plugins')
            and invocation.phase.data.pytest_plugins
        ):
            logger.debug(f"Pytest plugins found: {invocation.phase.data.pytest_plugins}")
            for plugin in invocation.phase.data.pytest_plugins:
                command_parts.append("--with")
                command_parts.append(shlex.quote(plugin))
        else:
            logger.debug("No pytest plugins specified in the plan.")

        command_parts.append("pytest")
        # Ensure the script path is correctly quoted, especially if it might contain spaces
        # or special characters, though typically it's a relative path like 'test_file.py'.
        command_parts.append(shlex.quote(script_path_str))

        logger.debug(f"Constructed pytest command: {' '.join(command_parts)}")
        return tmt.utils.ShellScript(command_parts)

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

import shlex
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import tmt.log
import tmt.result
import tmt.utils
from tmt.base import DependencySimple
from tmt.frameworks import TestFramework, provides_framework
from tmt.result import ResultOutcome
from tmt.steps.execute import TEST_OUTPUT_FILENAME, TestInvocation

# Define a constant for the JUnit XML filename
JUNIT_XML_FILENAME = "junit-report.xml"


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
        command_parts = ["uvx"]

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
        command_parts.append(f"--junitxml={JUNIT_XML_FILENAME}")
        command_parts.append(shlex.quote(script_path_str))

        logger.debug(f"Constructed pytest command: {' '.join(command_parts)}")
        return tmt.utils.ShellScript(command_parts)

    @classmethod
    def extract_results(
        cls,
        invocation: 'TestInvocation',
        results: list[tmt.result.Result],  # This is for tmt-report-result, not used here
        logger: tmt.log.Logger,
    ) -> list[tmt.result.Result]:
        assert invocation.return_code is not None
        junit_xml_filepath = invocation.path / JUNIT_XML_FILENAME
        main_log_path = invocation.relative_path / TEST_OUTPUT_FILENAME
        junit_xml_relative_path = invocation.relative_path / JUNIT_XML_FILENAME

        if junit_xml_filepath.exists():
            try:
                tree = ET.parse(junit_xml_filepath)  # noqa: S314
                root = tree.getroot()

                sub_results: list[tmt.result.SubResult] = []
                # Determine overall outcome from the testsuite summary if available
                # or by aggregating individual testcase outcomes.
                # Pytest's top-level <testsuite> errors/failures attributes are useful.
                testsuite_summary = root.find(
                    '.'
                )  # The root element is <testsuites> or <testsuite>
                if testsuite_summary is None:  # Should not happen for valid JUnit
                    raise ET.ParseError("Missing root testsuite element")

                # Initialize overall_outcome based on the worst possible outcome
                # If the <testsuite> (or <testsuites>) node has 'failures' or 'errors' > 0,
                # it's a fail/error.
                # Pytest behavior: if all tests pass, exit code 0. If any fail, exit code 1.
                # If collection error, other codes. No tests collected, exit code 5.
                # We will refine overall_outcome based on test cases.
                overall_outcome = ResultOutcome.PASS

                for testcase_node in root.findall('.//testcase'):
                    name = testcase_node.get('name', 'unknown_test_case')
                    classname = testcase_node.get('classname', '')
                    # Combine classname and name for a more unique tmt result name
                    result_name = f"{classname}.{name}" if classname else name
                    time_str = testcase_node.get('time')

                    current_outcome = ResultOutcome.PASS
                    message = None

                    skipped_node = testcase_node.find('skipped')
                    if skipped_node is not None:
                        current_outcome = ResultOutcome.SKIP
                        message = skipped_node.get('message')

                    failure_node = testcase_node.find('failure')
                    if failure_node is not None:
                        current_outcome = ResultOutcome.FAIL
                        message = failure_node.get('message')
                        # If failure_node.text is substantial, it might be more detailed.
                        if failure_node.text and (
                            not message or len(failure_node.text) > len(message)
                        ):
                            message = (
                                f"{message}: {failure_node.text.strip()}"
                                if message
                                else failure_node.text.strip()
                            )

                    error_node = testcase_node.find('error')
                    if error_node is not None:
                        # In tmt, ERROR is usually treated as a more severe FAIL.
                        current_outcome = ResultOutcome.ERROR
                        message = error_node.get('message')
                        if error_node.text and (
                            not message or len(error_node.text) > len(message)
                        ):
                            message = (
                                f"{message}: {error_node.text.strip()}"
                                if message
                                else error_node.text.strip()
                            )

                    # Update overall_outcome (worst of current vs new)
                    if current_outcome == ResultOutcome.ERROR:
                        overall_outcome = ResultOutcome.ERROR
                    elif (
                        current_outcome == ResultOutcome.FAIL
                        and overall_outcome != ResultOutcome.ERROR
                    ):
                        overall_outcome = ResultOutcome.FAIL
                    elif (
                        current_outcome == ResultOutcome.SKIP
                        and overall_outcome == ResultOutcome.PASS
                    ):
                        # If overall is PASS, and current is SKIP, overall becomes SKIP.
                        # If overall is already FAIL/ERROR, it remains FAIL/ERROR.
                        overall_outcome = ResultOutcome.SKIP

                    sub_results.append(
                        tmt.result.SubResult(
                            name=result_name,
                            result=current_outcome,
                            message=message,
                            duration=time_str,
                            # log_path=None # Not linking individual logs for subresults for now
                        )
                    )

                # Handle case where <testsuite> might be empty (e.g. pytest --collect-only with no tests)  # noqa: E501
                # or if all tests were skipped resulting in non-zero exit code (e.g. pytest.exit("all skipped", 5))  # noqa: E501
                # For pytest, exit code 5 means "no tests collected".
                if not sub_results and invocation.return_code == 5:
                    overall_outcome = ResultOutcome.INFO
                    # Add a note to the main result if possible, or handle as specific case
                    return [
                        tmt.Result.from_test_invocation(
                            invocation=invocation,
                            result=ResultOutcome.INFO,
                            log=[main_log_path, junit_xml_relative_path],
                            note=["No tests found by pytest (exit code 5, empty JUnit XML)."],
                        )
                    ]

                if sub_results:  # If we parsed any testcases
                    return [
                        tmt.Result.from_test_invocation(
                            invocation=invocation,
                            result=overall_outcome,
                            log=[main_log_path, junit_xml_relative_path],
                            subresult=sub_results,
                        )
                    ]
            # If sub_results is empty but not due to exit code 5, it might be an anomaly.
            # Fall through to exit code logic for such cases or if XML was empty for other reasons.

            except ET.ParseError as e:
                logger.warning(f"Failed to parse JUnit XML report '{junit_xml_filepath}': {e}")
            except Exception as e:  # Broad catch for other XML issues
                logger.warning(f"Error processing JUnit XML report '{junit_xml_filepath}': {e}")

        # Fallback to exit code logic
        logger.debug("Falling back to exit code based result extraction.")
        exit_code_outcome: ResultOutcome
        note_list: list[str] = []

        if invocation.return_code == 0:
            exit_code_outcome = ResultOutcome.PASS
        elif invocation.return_code == 5:  # No tests collected
            exit_code_outcome = ResultOutcome.INFO
            note_list.append("No tests found by pytest.")
        else:  # Includes exit code 1 (test failures) and others (errors)
            exit_code_outcome = ResultOutcome.FAIL
            # Add a note if it's not a standard pytest failure exit code (1)
            # or no-tests-found code (5)
            if invocation.return_code not in (1, 5):
                note_list.append(
                    f"Pytest exited with an unexpected code: {invocation.return_code}"
                )
            elif invocation.return_code == 1 and not junit_xml_filepath.exists():
                note_list.append(
                    "Pytest failed (exit code 1) and JUnit XML report was not found."
                )

        return [
            tmt.Result.from_test_invocation(
                invocation=invocation,
                result=exit_code_outcome,
                log=[main_log_path],  # Only main log if XML wasn't processed
                note=note_list,
            )
        ]

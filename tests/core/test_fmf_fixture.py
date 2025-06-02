from typing import Any, Callable

from click.testing import CliRunner as PytestClickCliRunner

import tmt.cli._root
from tmt._compat.pathlib import Path
from tmt.log import Logger  # For type hinting the root_logger fixture

# The fmf_tree fixture is expected to be in tests/conftest.py
# The root_logger fixture is also expected to be in tests/conftest.py


def test_tmt_plan_ls_with_custom_fmf_tree(
    fmf_tree: Callable[[dict[str, Any]], Path],
    cli_runner: PytestClickCliRunner,
    # Though not directly used in this test, it's part of the fmf_tree signature
    root_logger: Logger,
):
    """Tests 'tmt plan ls' with a dynamically created FMF tree using the fmf_tree fixture."""
    plan_name_fmf_path = "my/example/plan"  # This will become /my/example/plan in tmt
    plan_file_path = f"plans/{plan_name_fmf_path}.fmf"
    expected_plan_name_in_output = f"/{plan_name_fmf_path}"

    fmf_content = {
        plan_file_path: {
            "summary": "An example plan created by fmf_tree fixture",
            "discover": {"how": "shell", "tests": ["echo 'test1'"]},  # Simple discover
            "execute": {"how": "shell", "script": "echo 'executing'"},  # Simple execute
        }
        # .fmf/version is handled by the fixture by default
    }

    # Create the FMF tree using the fixture
    fmf_root: Path = fmf_tree(fmf_content)
    root_logger.debug(f"Test using FMF root at: {fmf_root}")

    # Run 'tmt plan ls' against the created FMF root
    result = cli_runner.invoke(tmt.cli._root.main, ["--root", str(fmf_root), "plan", "ls"])

    # Assert that the command was successful
    assert result.exit_code == 0, (
        f"Command 'tmt plan ls' failed with exit code {result.returncode}.\n"
        f"Stdout: {result.stdout}\nStderr: {result.stderr}"
    )

    # Assert that the plan name is in the output
    assert expected_plan_name_in_output in result.stdout, (
        f"Expected plan name '{expected_plan_name_in_output}' not found in 'tmt plan ls' output.\n"
        f"FMF Root: {fmf_root}\n"
        f"Plan file created at: {fmf_root / plan_file_path}\n"
        f"Stdout: {result.stdout}"
    )

    # For more robustness, one could also check the structure of the plan file
    assert (fmf_root / plan_file_path).is_file(), (
        f"Plan file {plan_file_path} was not created in {fmf_root}"
    )

    # Verify .fmf/version was created by the fixture
    assert (fmf_root / ".fmf" / "version").is_file(), ".fmf/version was not created by the fixture"

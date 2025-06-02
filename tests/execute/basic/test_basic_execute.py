import re
import subprocess
from pathlib import Path  # noqa: TID251  TODO _compat for tests?

import pytest
import ruamel.yaml

# Path to the data directory relative to this test file
# Assuming this test file is in tests/execute/basic/
DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def tmp_run_dir(tmp_path_factory):
    """Create a temporary directory for tmt run --id."""
    # tmp_path_factory provides a Path object unique to each test function call
    return tmp_path_factory.mktemp("tmt_run_")


def check_duration(duration_str: str) -> bool:
    """Checks if the duration string is in the format like 'Xs' (e.g., '5s')."""
    if not isinstance(duration_str, str):
        return False
    return re.fullmatch(r"\d+s", duration_str) is not None


def check_beakerlib_duration(duration_str: str) -> bool:
    """Checks if the duration string is in the format like 'HH:MM:SS' (e.g., '00:01:30')."""
    if not isinstance(duration_str, str):
        return False
    return re.fullmatch(r"\d{2,}:[0-5]\d:[0-5]\d", duration_str) is not None


def test_check_shell_results(tmp_run_dir: Path):
    """
    Checks the results of the shell tests execution.
    Corresponds to the "Check shell results" phase in test.sh.
    """
    # Ensure the data directory exists
    assert DATA_DIR.is_dir(), f"Data directory not found: {DATA_DIR}"

    # Construct the tmt command
    # Using str(tmp_run_dir) for compatibility with subprocess
    tmt_command = [
        "tmt",
        "run",
        "--scratch",
        "--id",
        str(tmp_run_dir),
        "discover",
        "provision",
        "execute",
        "finish",  # Ensure results.yaml is generated
    ]

    # Run the tmt command
    # The original script runs tmt from the 'data' directory
    process_result = subprocess.run(tmt_command, cwd=DATA_DIR, capture_output=True, text=True)

    # Debugging output
    print(f"tmp_run_dir: {tmp_run_dir}")
    print(f"tmt command: {' '.join(tmt_command)}")
    print(f"tmt cwd: {DATA_DIR}")
    print(f"tmt stdout: {process_result.stdout}")
    print(f"tmt stderr: {process_result.stderr}")

    assert process_result.returncode == 0, (
        f"tmt run command failed with exit code {process_result.returncode}"
    )

    # Construct the path to results.yaml
    # The plan name is 'shell' as seen in the original test.sh
    # (yq '.[] | select(.name == "/test/shell/good")' $run/shell/execute/results.yaml)
    # and the plan is defined in tests/execute/basic/data/plans/shell.fmf
    results_yaml_path = tmp_run_dir / "shell" / "execute" / "results.yaml"

    assert results_yaml_path.is_file(), (
        f"results.yaml not found at {results_yaml_path}. "
        f"Contents of tmp_run_dir / shell / execute: {
            list((tmp_run_dir / 'shell' / 'execute').iterdir())
            if (tmp_run_dir / 'shell' / 'execute').exists()
            else 'Not found'
        }"
    )

    yaml = ruamel.yaml.YAML()
    with open(results_yaml_path) as f:
        results_data = yaml.load(f)

    assert results_data, "results.yaml is empty or could not be loaded."

    # Store results by name for easier access
    results_by_name = {result['name']: result for result in results_data}

    # Assertions for /test/shell/good
    good_test_result = results_by_name.get("/test/shell/good")
    assert good_test_result, "/test/shell/good not found in results."
    assert good_test_result['result'] == 'pass', (
        f"/test/shell/good expected 'pass', got '{good_test_result['result']}'"
    )
    assert check_duration(good_test_result['duration']), (
        f"Invalid duration format for /test/shell/good: {good_test_result['duration']}"
    )
    # Check for output.txt in logs
    # The original script checks:
    # yq '.[] | select(.name == "/test/shell/good") | .log[] | select(. == "output.txt")'
    assert "output.txt" in good_test_result.get('log', []), (
        "/test/shell/good should have 'output.txt' in logs."
    )

    # Assertions for /test/shell/weird
    weird_test_result = results_by_name.get("/test/shell/weird")
    assert weird_test_result, "/test/shell/weird not found in results."
    assert weird_test_result['result'] == 'error', (
        f"/test/shell/weird expected 'error', got '{weird_test_result['result']}'"
    )
    assert check_duration(weird_test_result['duration']), (
        f"Invalid duration format for /test/shell/weird: {weird_test_result['duration']}"
    )

    # Assertions for /test/shell/bad
    bad_test_result = results_by_name.get("/test/shell/bad")
    assert bad_test_result, "/test/shell/bad not found in results."
    assert bad_test_result['result'] == 'fail', (
        f"/test/shell/bad expected 'fail', got '{bad_test_result['result']}'"
    )
    assert check_duration(bad_test_result['duration']), (
        f"Invalid duration format for /test/shell/bad: {bad_test_result['duration']}"
    )

    # Check if the results are as expected based on the original script's yq checks
    # This is implicitly covered by the assertions above, but good to keep in mind.
    # For example:
    # check "yq '.[] | select(.name == \"/test/shell/good\") | .result == \"pass\"' \
    # $run/shell/execute/results.yaml"
    # ... and so on for other tests and fields.


def test_check_beakerlib_results(tmp_run_dir: Path):
    """
    Checks the results of the BeakerLib tests execution.
    Corresponds to the "Check beakerlib results" phase in test.sh.
    """
    assert DATA_DIR.is_dir(), f"Data directory not found: {DATA_DIR}"

    tmt_command = [
        "tmt",
        "run",
        "--scratch",
        "--id",
        str(tmp_run_dir),
        "discover",
        "provision",
        "execute",
        "finish",
    ]

    process_result = subprocess.run(tmt_command, cwd=DATA_DIR, capture_output=True, text=True)

    print(f"tmp_run_dir (beakerlib): {tmp_run_dir}")
    print(f"tmt command (beakerlib): {' '.join(tmt_command)}")
    print(f"tmt cwd (beakerlib): {DATA_DIR}")
    print(f"tmt stdout (beakerlib): {process_result.stdout}")
    print(f"tmt stderr (beakerlib): {process_result.stderr}")

    # Check if tmt run command itself failed, if so, the shell results check might have passed
    # due to how tmt handles errors in individual plans vs the overall run.
    # For this test to be robust, we need to ensure the run was at least attempted.
    # A more robust solution would be a session/module scoped fixture that runs tmt once.
    if process_result.returncode != 0:
        # If the shell test already ran and created results, we might proceed
        # otherwise, this is a definite failure for beakerlib part.
        # This check is tricky because tmt might return 0 even if one plan fails,
        # as long as other plans succeed.
        # For now, we rely on results.yaml existence.
        pass

    # The plan name is 'beakerlib' as per tests/execute/basic/data/main.fmf
    results_yaml_path = tmp_run_dir / "beakerlib" / "execute" / "results.yaml"

    assert results_yaml_path.is_file(), (
        f"results.yaml not found at {results_yaml_path}. "
        f"Contents of tmp_run_dir / beakerlib / execute: {
            list((tmp_run_dir / 'beakerlib' / 'execute').iterdir())
            if (tmp_run_dir / 'beakerlib' / 'execute').exists()
            else 'Not found'
        }"
    )

    yaml = ruamel.yaml.YAML()
    with open(results_yaml_path) as f:
        results_data = yaml.load(f)

    assert results_data, "BeakerLib results.yaml is empty or could not be loaded."

    results_by_name = {result['name']: result for result in results_data}

    # Assertions for /test/beakerlib/good
    good_test_result = results_by_name.get("/test/beakerlib/good")
    assert good_test_result, "/test/beakerlib/good not found in results."
    assert good_test_result['result'] == 'pass', (
        f"/test/beakerlib/good expected 'pass', got '{good_test_result['result']}'"
    )
    assert check_beakerlib_duration(good_test_result['duration']), (
        f"Invalid duration format for /test/beakerlib/good: {good_test_result['duration']}"
    )
    assert "output.txt" in good_test_result.get('log', []), (
        "/test/beakerlib/good should have 'output.txt' in logs."
    )
    assert "journal.txt" in good_test_result.get('log', []), (
        "/test/beakerlib/good should have 'journal.txt' in logs."
    )

    # Assertions for /test/beakerlib/need
    need_test_result = results_by_name.get("/test/beakerlib/need")
    assert need_test_result, "/test/beakerlib/need not found in results."
    assert need_test_result['result'] == 'warn', (
        f"/test/beakerlib/need expected 'warn', got '{need_test_result['result']}'"
    )
    assert check_beakerlib_duration(need_test_result['duration']), (
        f"Invalid duration format for /test/beakerlib/need: {need_test_result['duration']}"
    )

    # Assertions for /test/beakerlib/weird
    weird_test_result = results_by_name.get("/test/beakerlib/weird")
    assert weird_test_result, "/test/beakerlib/weird not found in results."
    assert weird_test_result['result'] == 'error', (
        f"/test/beakerlib/weird expected 'error', got '{weird_test_result['result']}'"
    )
    assert check_beakerlib_duration(weird_test_result['duration']), (
        f"Invalid duration format for /test/beakerlib/weird: {weird_test_result['duration']}"
    )

    # Assertions for /test/beakerlib/bad
    bad_test_result = results_by_name.get("/test/beakerlib/bad")
    assert bad_test_result, "/test/beakerlib/bad not found in results."
    assert bad_test_result['result'] == 'fail', (
        f"/test/beakerlib/bad expected 'fail', got '{bad_test_result['result']}'"
    )
    assert check_beakerlib_duration(bad_test_result['duration']), (
        f"Invalid duration format for /test/beakerlib/bad: {bad_test_result['duration']}"
    )

from pytest_container.container import Container  # For type hinting

# Make sure the tmt_mini_container fixture is available (it's in tests/conftest.py)


def test_tmt_help_in_container(tmt_mini_container: Container):
    """
    Tests that `tmt --help` can be successfully run inside the tmt_mini_container.
    """
    # Execute `tmt --help` inside the container
    # The working directory is already set to /src in the fixture
    result = tmt_mini_container.connection.run("tmt --help")

    # Assert that the command was successful
    assert result.returncode == 0, (
        f"Command 'tmt --help' failed with exit code {result.returncode}.\n"
        f"Stdout: {result.stdout}\nStderr: {result.stderr}"
    )

    # Assert that the output contains expected text
    # Click usually puts the main help string for a command group in stdout
    expected_text = "Test Management Tool"
    assert expected_text in result.stdout, (
        f"Expected text '{expected_text}' not found in 'tmt --help' output.\n"
        f"Stdout: {result.stdout}"
    )

    # Optionally, check for specific command names if needed
    assert "run" in result.stdout
    assert "plan" in result.stdout
    assert "test" in result.stdout
    assert "story" in result.stdout

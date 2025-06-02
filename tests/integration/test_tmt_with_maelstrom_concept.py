# This test is intended to be run with Maelstrom.
# Example:
# 1. Generate the config:
#    pytest --collect-only -q tests/conftest.py::basic_maelstrom_config_for_tmt_tests
#    (This is a bit of a hack to get the path, ideally a script would generate this)
# 2. Then run:
#    maelstrom-pytest --config-dir <path_to_generated_config_dir> tests/integration/test_tmt_with_maelstrom_concept.py  # noqa: E501

import os
import sys

# Import pytest for potential use of its features, though not strictly needed for this simple test
import pytest

from tmt._compat.pathlib import Path


def test_tmt_import_and_basic_presence_in_maelstrom_env():
    """A conceptual test to verify that tmt can be imported and its source code
    is present in the Maelstrom container environment as configured by the
    basic_maelstrom_config_for_tmt_tests fixture.
    """
    # The maelstrom-pytest.toml from the fixture mounts the repo root to /src
    # We need to add /src to sys.path to allow importing tmt directly from source.
    # Alternatively, tmt could be "installed" in the Maelstrom environment
    # by adding '../' to test-requirements.txt and ensuring Maelstrom handles it.
    if "/src" not in sys.path:
        sys.path.insert(0, "/src")

    print(f"Python sys.path: {sys.path}")
    print(f"Current working directory: {os.getcwd()}")

    try:
        import tmt
        import tmt.cli  # Try importing a submodule as well

        print(f"TMT version {tmt.__version__} imported successfully from {tmt.__file__}.")
    except ImportError as e:
        # For debugging in the Maelstrom environment, list contents of /src
        # to understand what's actually there.
        src_contents = "N/A (could not list /src or /src does not exist)"
        if Path("/src").exists():
            try:
                src_contents = str(os.listdir("/src"))
            except Exception as list_e:
                src_contents = f"Error listing /src: {list_e}"

        pytest.fail(
            f"Failed to import tmt in Maelstrom environment. Error: {e}\n"
            f"Contents of /src: {src_contents}\n"
            f"Is /src in sys.path and does it contain the tmt source correctly?"
        )
    except Exception as e:  # Catch any other unexpected error during import
        pytest.fail(f"An unexpected error occurred during tmt import: {e}")

    # A very basic check, e.g., list contents of /src to see if tmt code is there
    # This verifies the `added_layers = [{ local_path = ".", remote_path = "/src" }]`
    # in maelstrom-pytest.toml worked as expected.
    try:
        src_listing = os.listdir("/src")
        print(f"Contents of /src: {src_listing}")
        # Check for a key file or directory that should exist at the root of the tmt repo
        assert "tmt" in src_listing, (
            "'tmt' directory (source code) not found directly in /src. "
            "Check added_layers in maelstrom-pytest.toml and Maelstrom execution context."
        )
        assert "pyproject.toml" in src_listing, (
            "'pyproject.toml' not found in /src. "
            "Indicates /src might not be the tmt repository root."
        )

    except FileNotFoundError:
        pytest.fail("Error listing /src directory. It seems the mount or path is incorrect.")
    except Exception as e:  # Catch any other unexpected error during listing
        pytest.fail(f"An unexpected error occurred while checking /src contents: {e}")

    # A simple assertion to make it a valid test
    assert True, "TMT imported and basic source presence confirmed."

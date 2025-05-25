import pytest
from click.testing import CliRunner as PytestClickCliRunner
from ruamel.yaml import YAML

import tmt.cli._root

# Expected plugin list from the original test.sh
EXPECTED_PLUGIN_LIST_STR = """\
export.plan: dict json template yaml
export.story: dict json rst template yaml
export.test: dict json nitrate polarion template yaml
package_managers: apk apt bootc dnf dnf5 rpm-ostree yum
plan_shapers: max-tests repeat
prepare.feature: crb epel fips profile
step.discover: fmf shell
step.execute: tmt upgrade
step.finish: ansible shell
step.prepare: ansible feature install shell
step.provision: artemis beaker bootc connect container local virtual.testcloud
step.report: display html junit polarion reportportal
test.check: avc coredump dmesg watchdog
test.framework: beakerlib shell
"""

EXPECTED_PLUGIN_HEADERS = [
    "Export plugins for story",
    "Export plugins for plan",
    "Export plugins for test",
    "Test check plugins",
    "Test framework plugins",
    "Plan shapers",
    "Package manager plugins",
    "Discover step plugins",
    "Provision step plugins",
    "Prepare step plugins",
    "Execute step plugins",
    "Finish step plugins",
    "Report step plugins",
]

def test_about_plugins_ls_human_readable(cli_runner: PytestClickCliRunner):
    """ Test 'tmt about plugin ls' human-readable output """
    result = cli_runner.invoke(tmt.cli._root.main, ['about', 'plugin', 'ls'])
    assert result.exit_code == 0
    for header in EXPECTED_PLUGIN_HEADERS:
        assert header in result.stdout

def test_about_plugins_ls_yaml(cli_runner: PytestClickCliRunner):
    """ Test 'tmt about plugins ls --how yaml' output """
    result = cli_runner.invoke(tmt.cli._root.main, ['about', 'plugins', 'ls', '--how', 'yaml'])
    assert result.exit_code == 0

    yaml = YAML()
    try:
        data = yaml.load(result.stdout)
    except Exception as e:
        pytest.fail(f"Failed to parse YAML output: {e}\nOutput:\n{result.stdout}")

    actual_plugin_list_parts = []
    for key, values in sorted(data.items()):
        sorted_values = " ".join(sorted(values))
        actual_plugin_list_parts.append(f"{key}: {sorted_values}")
    actual_plugin_list_str = "\n".join(actual_plugin_list_parts)

    # Normalize the expected string (sort lines and handle potential trailing newline)
    expected_lines = sorted([line.strip() for line in EXPECTED_PLUGIN_LIST_STR.strip().split('\n')])
    actual_lines = sorted([line.strip() for line in actual_plugin_list_str.strip().split('\n')])

    assert actual_lines == expected_lines, (
        f"Actual plugin list does not match expected.\n"
        f"Expected:\n{chr(10).join(expected_lines)}\n"
        f"Actual:\n{chr(10).join(actual_lines)}"
    )

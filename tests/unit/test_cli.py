import contextlib
import os
import shutil
import sys
import tempfile

import _pytest.monkeypatch
import pytest
from click.testing import CliRunner as PytestClickCliRunner
from hypothesis import given
from hypothesis import strategies as st

import tmt.cli._root
import tmt.log
from tests import CliRunner
from tmt.container import container
from tmt.utils import Path

# Prepare path to examples
PATH = Path(__file__).resolve().parent


def example(name):
    """Return path to given example."""
    return PATH / "../../examples/" / name


runner = CliRunner()


@contextlib.contextmanager
def patch_environment_and_tty(env_vars=None, simulate_tty=False):
    """Context manager for patching environment variables and TTY simulation."""
    env_vars = env_vars or {}

    # Store original environment values
    original_env = {}
    for var in ['NO_COLOR', 'TMT_NO_COLOR', 'TMT_FORCE_COLOR']:
        original_env[var] = os.environ.get(var)
        if var in os.environ:
            del os.environ[var]

    # Store original isatty methods
    original_stdout_isatty = sys.stdout.isatty
    original_stderr_isatty = sys.stderr.isatty

    try:
        # Set new environment variables
        for var, value in env_vars.items():
            if value is not None:
                os.environ[var] = value

        # Mock isatty methods
        sys.stdout.isatty = lambda: simulate_tty
        sys.stderr.isatty = lambda: simulate_tty

        yield

    finally:
        # Restore original environment
        for var in ['NO_COLOR', 'TMT_NO_COLOR', 'TMT_FORCE_COLOR']:
            if original_env[var] is not None:
                os.environ[var] = original_env[var]
            elif var in os.environ:
                del os.environ[var]

        # Restore original isatty methods
        sys.stdout.isatty = original_stdout_isatty
        sys.stderr.isatty = original_stderr_isatty


def test_mini(cli_runner: PytestClickCliRunner):
    """Minimal smoke test."""
    tmp = tempfile.mkdtemp()
    result = cli_runner.invoke(
        tmt.cli._root.main, ['--root', example('mini'), 'run', '-i', tmp, '-dv', 'discover']
    )
    assert result.exit_code == 0
    assert 'Found 1 plan.' in result.output
    assert '1 test selected' in result.output
    assert '/ci' in result.output
    shutil.rmtree(tmp)


def test_init():
    """Tree initialization."""
    tmp = tempfile.mkdtemp()
    original_directory = os.getcwd()
    os.chdir(tmp)
    result = runner.invoke(tmt.cli._root.main, ['init'])
    assert 'Initialized the fmf tree root' in result.output
    result = runner.invoke(tmt.cli._root.main, ['init'])
    assert 'already exists' in result.output
    result = runner.invoke(tmt.cli._root.main, ['init', '--template', 'mini'])
    assert 'plans/example' in result.output
    result = runner.invoke(tmt.cli._root.main, ['init', '--template', 'mini'])
    assert result.exception
    result = runner.invoke(tmt.cli._root.main, ['init', '--template', 'full', '--force'])
    assert 'overwritten' in result.output
    # tmt init --template mini in a clean directory
    os.system('rm -rf .fmf *')
    result = runner.invoke(tmt.cli._root.main, ['init', '--template', 'mini'])
    assert 'plans/example' in result.output
    # tmt init --template full in a clean directory
    os.system('rm -rf .fmf *')
    result = runner.invoke(tmt.cli._root.main, ['init', '--template', 'full'])
    assert 'tests/example' in result.output
    os.chdir(original_directory)
    shutil.rmtree(tmp)


def test_create():
    """Test, plan and story creation."""
    # Create a test directory
    tmp = tempfile.mkdtemp()
    original_directory = os.getcwd()
    os.chdir(tmp)
    # Commands to test
    commands = [
        'init',
        'test create -t beakerlib test',
        'test create -t shell test',
        'plan create -t mini test',
        'plan create -t full test',
        'story create -t mini test',
        'story create -t full test',
    ]
    for command in commands:
        result = runner.invoke(tmt.cli._root.main, command.split())
        assert result.exit_code == 0
        os.system('rm -rf *')
    # Test directory cleanup
    os.chdir(original_directory)
    shutil.rmtree(tmp)


def test_step():
    """Select desired step."""
    for step in ['discover', 'provision', 'prepare']:
        tmp = tempfile.mkdtemp()
        result = runner.invoke(
            tmt.cli._root.main,
            ['--feeling-safe', '--root', example('local'), 'run', '-i', tmp, step],
        )
        assert result.exit_code == 0
        assert step in result.output
        assert 'finish' not in result.output
        shutil.rmtree(tmp)


def test_step_execute():
    """Test execute step."""
    tmp = tempfile.mkdtemp()
    step = 'execute'

    result = runner.invoke(tmt.cli._root.main, ['--root', example('local'), 'run', '-i', tmp, step])

    # Test execute empty with discover output missing
    assert result.exit_code != 0
    assert isinstance(result.exception, tmt.utils.GeneralError)
    assert len(result.exception.causes) == 1
    assert isinstance(result.exception.causes[0], tmt.utils.ExecuteError)
    assert step in result.output
    assert 'provision' not in result.output
    shutil.rmtree(tmp)


def test_systemd():
    """Check systemd example."""
    result = runner.invoke(tmt.cli._root.main, ['--root', example('systemd'), 'plan'])
    assert result.exit_code == 0
    assert 'Found 2 plans' in result.output
    result = runner.invoke(tmt.cli._root.main, ['--root', example('systemd'), 'plan', 'show'])
    assert result.exit_code == 0
    assert 'Tier two functional tests' in result.output


@container
class DecideColorizationTestcase:
    """A single test case for :py:func:`tmt.log.decide_colorization`."""

    # Name of the testcase and expected outcome of decide_colorization()
    name: str
    expected: tuple[bool, bool]

    # Testcase environment setup to perform before calling decide_colorization()
    set_no_color_option: bool = False
    set_force_color_option: bool = False
    set_no_color_envvar: bool = False
    set_tmt_no_color_envvar: bool = False
    set_tmt_force_color_envvar: bool = False
    simulate_tty: bool = False


_DECIDE_COLORIZATION_TESTCASES = [
    # With TTY simulated
    DecideColorizationTestcase('tty, autodetection', (True, True), simulate_tty=True),
    DecideColorizationTestcase(
        'tty, disable with option', (False, False), set_no_color_option=True, simulate_tty=True
    ),
    DecideColorizationTestcase(
        'tty, disable with NO_COLOR', (False, False), set_no_color_envvar=True, simulate_tty=True
    ),
    DecideColorizationTestcase(
        'tty, disable with TMT_NO_COLOR',
        (False, False),
        set_tmt_no_color_envvar=True,
        simulate_tty=True,
    ),
    DecideColorizationTestcase(
        'tty, force with option', (True, True), set_force_color_option=True, simulate_tty=True
    ),
    DecideColorizationTestcase(
        'tty, force with TMT_FORCE_COLOR',
        (True, True),
        set_tmt_force_color_envvar=True,
        simulate_tty=True,
    ),
    DecideColorizationTestcase(
        'tty, force with TMT_FORCE_COLOR over NO_COLOR',
        (True, True),
        set_tmt_force_color_envvar=True,
        set_no_color_envvar=True,
    ),
    DecideColorizationTestcase(
        'tty, force with TMT_FORCE_COLOR over --no-color',
        (True, True),
        set_tmt_force_color_envvar=True,
        set_no_color_option=True,
    ),
    # With TTY not simulated, streams are captured
    DecideColorizationTestcase('not tty, autodetection', (False, False)),
    DecideColorizationTestcase(
        'not tty, disable with option', (False, False), set_no_color_option=True
    ),
    DecideColorizationTestcase(
        'not tty, disable with NO_COLOR', (False, False), set_no_color_envvar=True
    ),
    DecideColorizationTestcase(
        'not tty, disable with TMT_NO_COLOR', (False, False), set_tmt_no_color_envvar=True
    ),
    DecideColorizationTestcase(
        'not tty, force with option', (True, True), set_force_color_option=True
    ),
    DecideColorizationTestcase(
        'not tty, force with TMT_FORCE_COLOR', (True, True), set_tmt_force_color_envvar=True
    ),
    DecideColorizationTestcase(
        'not tty, force with TMT_FORCE_COLOR over NO_COLOR',
        (True, True),
        set_tmt_force_color_envvar=True,
        set_tmt_no_color_envvar=True,
    ),
    DecideColorizationTestcase(
        'not tty, force with TMT_FORCE_COLOR over --no-color',
        (True, True),
        set_tmt_force_color_envvar=True,
        set_no_color_option=True,
    ),
]


@pytest.mark.parametrize(
    'testcase',
    list(_DECIDE_COLORIZATION_TESTCASES),
    ids=[testcase.name for testcase in _DECIDE_COLORIZATION_TESTCASES],
)
def test_decide_colorization(
    testcase: DecideColorizationTestcase, monkeypatch: _pytest.monkeypatch.MonkeyPatch
) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.delenv('TMT_NO_COLOR', raising=False)
    monkeypatch.delenv('TMT_FORCE_COLOR', raising=False)

    no_color = bool(testcase.set_no_color_option)
    force_color = bool(testcase.set_force_color_option)

    if testcase.set_no_color_envvar:
        monkeypatch.setenv('NO_COLOR', '')

    if testcase.set_tmt_no_color_envvar:
        monkeypatch.setenv('TMT_NO_COLOR', '')

    if testcase.set_tmt_force_color_envvar:
        monkeypatch.setenv('TMT_FORCE_COLOR', '')

    monkeypatch.setattr(sys.stdout, 'isatty', lambda: testcase.simulate_tty)
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: testcase.simulate_tty)

    assert tmt.log.decide_colorization(no_color, force_color) == testcase.expected


@given(
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.booleans(),
)
def test_decide_colorization_hypothesis(
    no_color_option: bool,
    force_color_option: bool,
    no_color_envvar: bool,
    tmt_no_color_envvar: bool,
    tmt_force_color_envvar: bool,
    simulate_tty: bool,
) -> None:
    # Prepare environment variables
    env_vars = {}
    if no_color_envvar:
        env_vars['NO_COLOR'] = ''
    if tmt_no_color_envvar:
        env_vars['TMT_NO_COLOR'] = ''
    if tmt_force_color_envvar:
        env_vars['TMT_FORCE_COLOR'] = ''

    with patch_environment_and_tty(env_vars=env_vars, simulate_tty=simulate_tty):
        # Determine expected output based on the logic in tmt.log.decide_colorization
        # (Copied and adapted from tmt.log.decide_colorization)
        # Enforce colors if `--force-color` was used, or `TMT_FORCE_COLOR` envvar is set.
        if force_color_option or 'TMT_FORCE_COLOR' in os.environ:
            expected_output = True
            expected_logging = True
        # Disable coloring if `--no-color` was used, or `NO_COLOR` or `TMT_NO_COLOR` envvar is set.
        elif no_color_option or 'NO_COLOR' in os.environ or 'TMT_NO_COLOR' in os.environ:
            expected_output = False
            expected_logging = False
        # Autodetection, disable colors when not talking to a terminal.
        else:
            expected_output = simulate_tty
            expected_logging = simulate_tty

        assert tmt.log.decide_colorization(no_color_option, force_color_option) == (
            expected_output,
            expected_logging,
        )

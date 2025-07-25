"""
Test Metadata Utilities
"""

import contextlib
import copy
import dataclasses
import datetime
import enum
import functools
import importlib.resources
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import unicodedata
import urllib.parse
import warnings
from collections import Counter
from collections.abc import Iterable, Iterator
from math import ceil
from re import Pattern
from threading import RLock, Thread
from types import ModuleType
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    Callable,
    Generic,
    Literal,
    Optional,
    TextIO,
    TypeVar,
    Union,
    cast,
    overload,
)

import click
import fmf
import fmf.utils
import jsonschema
import requests
import requests.adapters
import ruamel.yaml.reader
import ruamel.yaml.scalarstring
import urllib3
import urllib3._collections
import urllib3.exceptions
import urllib3.util.retry
from click import echo, wrap_text
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.parser import ParserError
from ruamel.yaml.representer import Representer
from urllib3.response import HTTPResponse

import tmt.log
from tmt._compat.pathlib import Path
from tmt._compat.typing import ParamSpec
from tmt.container import container
from tmt.log import LoggableValue
from tmt.utils.themes import style

if TYPE_CHECKING:
    import tmt.base
    import tmt.cli
    import tmt.steps
    import tmt.utils.themes
    from tmt._compat.typing import Self, TypeAlias
    from tmt.hardware import Size


def sanitize_string(text: str) -> str:
    """Remove invalid Unicode characters from a string"""
    try:
        text.encode('utf-8', errors='strict')
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="ignore").decode("utf-8")


def configure_optional_constant(default: Optional[int], envvar: str) -> Optional[int]:
    """
    Deduce the actual value of a global constant which may be left unset.

    :param default: the default value of the constant.
    :param envvar: name of the optional environment variable which would
        override the default value.
    :returns: value extracted from the environment variable, or the
        given default value if the variable did not exist.
    """

    if envvar not in os.environ:
        return default

    try:
        return int(os.environ[envvar])

    except ValueError as exc:
        raise tmt.utils.GeneralError(
            f"Could not parse '{envvar}={os.environ[envvar]}' as integer."
        ) from exc


def configure_constant(default: int, envvar: str) -> int:
    """
    Deduce the actual value of global constant.

    :param default: the default value of the constant.
    :param envvar: name of the optional environment variable which would
        override the default value.
    :returns: value extracted from the environment variable, or the
        given default value if the variable did not exist.
    """

    try:
        return int(os.environ.get(envvar, default))

    except ValueError as exc:
        raise tmt.utils.GeneralError(
            f"Could not parse '{envvar}={os.environ[envvar]}' as integer."
        ) from exc


def configure_bool_constant(default: bool, envvar: str) -> bool:
    """
    Deduce the bool value of global constant.

    Value '1' means True, all other values mean False.

    :param default: the default value of the constant.
    :param envvar: name of the optional environment variable which would
        override the default value.
    :returns: value extracted from the environment variable, or the
        given default value if the variable did not exist.
    """
    value = os.environ.get(envvar)
    if value is None:
        return default
    return value == "1"


#: How many leading characters to display in tracebacks with
#: ``TMT_SHOW_TRACEBACK=2``.
TRACEBACK_LOCALS_TRIM = 1024

# Default workdir root and max
WORKDIR_ROOT = Path('/var/tmp/tmt')  # noqa: S108 insecure usage of temporary dir
WORKDIR_MAX = 1000

# Maximum number of lines of stdout/stderr to show upon errors
OUTPUT_LINES = 100

#: How wide should the output be at maximum.
#: This is the default value tmt would use unless told otherwise.
DEFAULT_OUTPUT_WIDTH: int = 79

#: How wide should the output be at maximum.
#: This is the effective value, combining the default and optional envvar,
#: ``TMT_OUTPUT_WIDTH``.
OUTPUT_WIDTH: int = configure_constant(DEFAULT_OUTPUT_WIDTH, 'TMT_OUTPUT_WIDTH')

# Hierarchy indent
INDENT = 4

# Default name for step plugins
DEFAULT_NAME = 'default'


# Special process return codes


class ProcessExitCodes(enum.IntEnum):
    #: Successful run.
    SUCCESS = 0
    #: Unsuccessful run.
    FAILURE = 1

    #: tmt pidfile lock operation failed.
    TEST_PIDFILE_LOCK_FAILED = 122
    #: tmt pidfile unlock operation failed.
    TEST_PIDFILE_UNLOCK_FAILED = 123

    #: Command was terminated because of a timeout.
    TIMEOUT = 124

    #: Permission denied (or) unable to execute.
    PERMISSION_DENIED = 126
    #: Command not found, or PATH error.
    NOT_FOUND = 127

    # (128 + N) where N is a signal send to the process
    #: Terminated by either ``Ctrl+C`` combo or ``SIGINT`` signal.
    SIGINT = 130
    #: Terminated by a ``SIGTERM`` signal.
    SIGTERM = 143

    @classmethod
    def is_pidfile(cls, exit_code: Optional[int]) -> bool:
        return exit_code in (
            ProcessExitCodes.TEST_PIDFILE_LOCK_FAILED,
            ProcessExitCodes.TEST_PIDFILE_UNLOCK_FAILED,
        )

    @classmethod
    def format(cls, exit_code: int) -> Optional[str]:
        """
        Format a given exit code for nicer logging
        """

        member = cls._value2member_map_.get(exit_code)

        if member is None:
            return 'unrecognized'

        if member.name.startswith('SIG'):
            return member.name

        return member.name.lower().replace('_', ' ')


# Default select.select(timeout) in seconds
DEFAULT_SELECT_TIMEOUT = 5

# Default shell and options to be set for all shell scripts
DEFAULT_SHELL = "/bin/bash"
SHELL_OPTIONS = 'set -eo pipefail'

# Defaults for HTTP/HTTPS retries and timeouts (see `retry_session()`).
DEFAULT_RETRY_SESSION_RETRIES: int = 3
DEFAULT_RETRY_SESSION_BACKOFF_FACTOR: float = 0.1

# Defaults for HTTP/HTTPS retries for getting environment file
# Retry with exponential backoff, maximum duration ~511 seconds
ENVFILE_RETRY_SESSION_RETRIES: int = 10
ENVFILE_RETRY_SESSION_BACKOFF_FACTOR: float = 1

# Defaults for HTTP/HTTPS codes that are considered retriable
DEFAULT_RETRIABLE_HTTP_CODES: Optional[tuple[int, ...]] = (
    403,  # Forbidden (but Github uses it for rate limiting)
    429,  # Too Many Requests
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
)

# Defaults for GIT attempts and interval
DEFAULT_GIT_CLONE_TIMEOUT: Optional[int] = None
GIT_CLONE_TIMEOUT: Optional[int] = configure_optional_constant(
    DEFAULT_GIT_CLONE_TIMEOUT, 'TMT_GIT_CLONE_TIMEOUT'
)

DEFAULT_GIT_CLONE_ATTEMPTS: int = 3
GIT_CLONE_ATTEMPTS: int = configure_constant(DEFAULT_GIT_CLONE_ATTEMPTS, 'TMT_GIT_CLONE_ATTEMPTS')

DEFAULT_GIT_CLONE_INTERVAL: int = 10
GIT_CLONE_INTERVAL: int = configure_constant(DEFAULT_GIT_CLONE_INTERVAL, 'TMT_GIT_CLONE_INTERVAL')

# A stand-in variable for generic use.
T = TypeVar('T')


WriteMode = Literal['w', 'a']


def effective_workdir_root(workdir_root_option: Optional[Path] = None) -> Path:
    """
    Find out what the actual workdir root is.

    If the ``workdir-root`` cli option is set, it is used as the workdir root.
    Otherwise, the ``TMT_WORKDIR_ROOT`` environment variable is used if set.
    If neither is specified, the default value of :py:data:``WORKDIR_ROOT`` is used.
    """

    if workdir_root_option:
        return workdir_root_option

    if 'TMT_WORKDIR_ROOT' in os.environ:
        return Path(os.environ['TMT_WORKDIR_ROOT'])

    return WORKDIR_ROOT


class FmfContext(dict[str, list[str]]):
    """
    Represents an fmf context.

    See https://tmt.readthedocs.io/en/latest/spec/context.html
    and https://fmf.readthedocs.io/en/latest/context.html.
    """

    def __init__(self, data: Optional[dict[str, list[str]]] = None) -> None:
        super().__init__(data or {})

    @classmethod
    def _normalize_command_line(cls, spec: list[str], logger: tmt.log.Logger) -> 'FmfContext':
        """
        Normalize command line fmf context specification.

        .. code-block:: ini

            -c distro=fedora-33 -> {'distro': ['fedora']}
            -c arch=x86_64,ppc64 -> {'arch': ['x86_64', 'ppc64']}
        """

        return FmfContext(
            {
                key: value.split(',')
                for key, value in Environment.from_sequence(spec, logger).items()
            }
        )

    @classmethod
    def _normalize_fmf(
        cls, spec: dict[str, Union[str, list[str]]], logger: tmt.log.Logger
    ) -> 'FmfContext':
        """
        Normalize fmf context specification from fmf node.

        .. code-block:: yaml

            context:
              distro: fedora-33
              arch:
                - x86_64
                - ppc64
        """

        normalized: FmfContext = FmfContext()

        for dimension, values in spec.items():
            if isinstance(values, list):
                normalized[str(dimension)] = [str(v) for v in values]
            else:
                normalized[str(dimension)] = [str(values)]

        return normalized

    @classmethod
    def from_spec(cls, key_address: str, spec: Any, logger: tmt.log.Logger) -> 'FmfContext':
        """
        Convert from a specification file or from a CLI option.

        See https://tmt.readthedocs.io/en/stable/spec/context.html for details on context.
        """

        if spec is None:
            return FmfContext()

        if isinstance(spec, tuple):
            return cls._normalize_command_line(list(spec), logger)

        if isinstance(spec, list):
            return cls._normalize_command_line(spec, logger)

        if isinstance(spec, dict):
            return cls._normalize_fmf(spec, logger)

        raise NormalizationError(key_address, spec, 'a list of strings or a dictionary')

    def to_spec(self) -> dict[str, Any]:
        """
        Convert to a form suitable for saving in a specification file
        """

        return dict(self)


#: A type of environment variable name.
EnvVarName: 'TypeAlias' = str

# This one is not an alias: a full-fledged class makes type linters
# enforce strict instantiation of objects rather than accepting
# strings where `EnvVarValue` is expected.


class EnvVarValue(str):
    """
    A type of environment variable value
    """

    def __new__(cls, raw_value: Any) -> 'EnvVarValue':
        if isinstance(raw_value, str):
            return str.__new__(cls, raw_value)

        if isinstance(raw_value, Path):
            return str.__new__(cls, str(raw_value))

        raise GeneralError(
            f"Only strings and paths can be environment variables, '{type(raw_value)}' found."
        )


class Environment(dict[str, EnvVarValue]):
    """
    Represents a set of environment variables.

    See https://tmt.readthedocs.io/en/latest/spec/tests.html#environment,
    https://tmt.readthedocs.io/en/latest/spec/plans.html#environment and
    https://tmt.readthedocs.io/en/latest/spec/plans.html#environment-file.
    """

    def __init__(self, data: Optional[dict[EnvVarName, EnvVarValue]] = None) -> None:
        super().__init__(data or {})

    @classmethod
    def from_dotenv(cls, content: str) -> 'Environment':
        """
        Construct environment from a ``.env`` format.

        :param content: string containing variables defined in the "dotenv"
            format, https://hexdocs.pm/dotenvy/dotenv-file-format.html.
        """

        environment = Environment()

        try:
            for line in shlex.split(content, comments=True):
                key, value = line.split("=", maxsplit=1)

                environment[key] = EnvVarValue(value)

        except Exception as exc:
            raise GeneralError("Failed to extract variables from 'dotenv' format.") from exc

        return environment

    @classmethod
    def from_yaml(cls, content: str) -> 'Environment':
        """
        Construct environment from a YAML format.

        :param content: string containing variables defined in a YAML
            dictionary, i.e. ``key: value`` entries.
        """

        try:
            yaml = YAML(typ="safe").load(content)

        except Exception as exc:
            raise GeneralError('Failed to extract variables from YAML format.') from exc

        # Handle empty file as an empty environment
        if yaml is None:
            return Environment()

        if not isinstance(yaml, dict):
            raise GeneralError(
                'Failed to extract variables from YAML format, '
                'YAML defining variables must be a dictionary.'
            )

        if any(isinstance(v, (dict, list)) for v in yaml.values()):
            raise GeneralError(
                'Failed to extract variables from YAML format, '
                'only primitive types are accepted as values.'
            )

        return Environment({key: EnvVarValue(str(value)) for key, value in yaml.items()})

    @classmethod
    def from_yaml_file(
        cls,
        filepath: Path,
        logger: tmt.log.Logger,
    ) -> 'Environment':
        """
        Construct environment from a YAML file.

        File is expected to contain variables in a YAML dictionary, i.e.
        ``key: value`` entries. Only primitive types - strings, numbers,
        booleans - are allowed as values.

        :param path: path to the file with variables.
        :param logger: used for logging.
        """

        try:
            content = filepath.read_text()

        except Exception as exc:
            raise GeneralError(
                f"Failed to extract variables from YAML file '{filepath}'."
            ) from exc

        return cls.from_yaml(content)

    @classmethod
    def from_sequence(
        cls,
        variables: Union[str, list[str]],
        logger: tmt.log.Logger,
    ) -> 'Environment':
        """
        Construct environment from a sequence of variables.

        Variables may be specified in two ways:

        * ``NAME=VALUE`` pairs, or
        * ``@foo.yaml`` signaling variables to be read from a file.

        If a "variable" starts with ``@``, it is treated as a path to
        a YAML or DOTENV file that contains key/value pairs which are then
        transparently loaded and added to the final environment.

        :param variables: string or a sequence of strings containing
            variables. The acceptable formats are:

            * ``'X=1'``
            * ``'X=1 Y=2 Z=3'``
            * ``['X=1', 'Y=2', 'Z=3']``
            * ``['X=1 Y=2 Z=3', 'A=1 B=2 C=3']``
            * ``'TXT="Some text with spaces in it"'``
            * ``@foo.yaml``
            * ``@../../bar.yaml``
            * ``@foo.env``
        """

        if not isinstance(variables, (list, tuple)):
            variables = [variables]

        result = Environment()

        for variable in variables:
            if variable is None:
                continue
            for var in shlex.split(variable):
                if var.startswith('@'):
                    if not var[1:]:
                        raise GeneralError(f"Invalid variable file specification '{var}'.")

                    filename = var[1:]
                    environment = cls.from_file(filename=filename, logger=logger)

                    if not environment:
                        logger.warning(f"Empty environment file '{filename}'.")

                    result.update(environment)

                else:
                    matched = re.match("([^=]+)=(.*)", var)
                    if not matched:
                        raise GeneralError(f"Invalid variable specification '{var}'.")
                    name, value = matched.groups()
                    result[name] = EnvVarValue(value)

        return result

    @classmethod
    def from_file(
        cls,
        *,
        filename: str,
        root: Optional[Path] = None,
        logger: tmt.log.Logger,
    ) -> 'Environment':
        """
        Construct environment from a file.

        YAML files - recognized by ``.yaml`` or ``.yml`` suffixes - or
        ``.env``-like files are supported.

        .. code-block:: bash

           A=B
           C=D

        .. code-block:: yaml

           A: B
           C: D

        .. note::

            For loading environment variables from multiple files, see
            :py:meth:`Environment.from_files`.
        """

        root = root or Path.cwd()
        filename = filename.strip()
        environment_filepath: Optional[Path] = None

        # Fetch a remote file
        if filename.startswith("http"):
            # Create retry session for longer retries, see #1229
            session = retry_session.create(
                retries=ENVFILE_RETRY_SESSION_RETRIES,
                backoff_factor=ENVFILE_RETRY_SESSION_BACKOFF_FACTOR,
                allowed_methods=('GET',),
                status_forcelist=DEFAULT_RETRIABLE_HTTP_CODES,
                logger=logger,
            )
            try:
                response = session.get(filename)
                response.raise_for_status()
                content = response.text
            except requests.RequestException as error:
                raise GeneralError(
                    f"Failed to extract variables from URL '{filename}'."
                ) from error

        # Read a local file
        else:
            # Ensure we don't escape from the metadata tree root

            root = root.resolve()
            environment_filepath = root.joinpath(filename).resolve()

            if not environment_filepath.is_relative_to(root):
                raise GeneralError(
                    f"Failed to extract variables from file '{environment_filepath}' as it "
                    f"lies outside the metadata tree root '{root}'."
                )
            if not environment_filepath.is_file():
                raise GeneralError(f"File '{environment_filepath}' doesn't exist.")

            content = environment_filepath.read_text()

        # Parse yaml file
        if Path(filename).suffix.lower() in ('.yaml', '.yml'):
            environment = cls.from_yaml(content)

        else:
            environment = cls.from_dotenv(content)

        if not environment:
            logger.warning(f"Empty environment file '{filename}'.")

            return Environment()

        return environment

    @classmethod
    def from_files(
        cls,
        *,
        filenames: Iterable[str],
        root: Optional[Path] = None,
        logger: tmt.log.Logger,
    ) -> 'Environment':
        """
        Read environment variables from the given list of files.

        Files should be in YAML format (``.yaml`` or ``.yml`` suffixes), or in dotenv format.

        .. code-block:: bash

           A=B
           C=D

        .. code-block:: yaml

           A: B
           C: D

        Path to each file should be relative to the metadata tree root.

        .. note::

            For loading environment variables from a single file, see
            :py:meth:`Environment.from_file`, which is a method called
            for each file, accumulating data from all input files.
        """

        root = root or Path.cwd()

        result = Environment()

        for filename in filenames:
            result.update(cls.from_file(filename=filename, root=root, logger=logger))

        return result

    @classmethod
    def from_inputs(
        cls,
        *,
        raw_fmf_environment: Any = None,
        raw_fmf_environment_files: Any = None,
        raw_cli_environment: Any = None,
        raw_cli_environment_files: Any = None,
        file_root: Optional[Path] = None,
        key_address: Optional[str] = None,
        logger: tmt.log.Logger,
    ) -> 'Environment':
        """
        Extract environment variables from various sources.

        Combines various raw sources into a set of environment variables. Calls
        necessary functions to process environment files, dictionaries and CLI
        inputs.

        All inputs are optional, and there is a clear order of preference, which is,
        from the most preferred:

        * ``--environment`` CLI option (``raw_cli_environment``)
        * ``--environment-file`` CLI option (``raw_cli_environment_files``)
        * ``environment`` fmf key (``raw_fmf_environment``)
        * ``environment-file`` fmf key (``raw_fmf_environment_files``)

        :param raw_fmf_environment: content of ``environment`` fmf key. ``None``
            and a dictionary are accepted.
        :param raw_fmf_environment_files: content of ``environment-file`` fmf key.
            ``None`` and a list of paths are accepted.
        :param raw_cli_environment: content of ``--environment`` CLI option.
            ``None``, a tuple or a list are accepted.
        :param raw_cli_environment_files: content of `--environment-file`` CLI
            option. ``None``, a tuple or a list are accepted.
        :raises NormalizationError: when an input is of a type which is not allowed
            for that particular source.
        """

        key_address_prefix = f'{key_address}:' if key_address else ''

        from_fmf_files = Environment()
        from_fmf_dict = Environment()
        from_cli_files = Environment()
        from_cli = Environment()

        if raw_fmf_environment_files is None:
            pass
        elif isinstance(raw_fmf_environment_files, list):
            from_fmf_files = cls.from_files(
                filenames=raw_fmf_environment_files,
                root=file_root,
                logger=logger,
            )
        else:
            raise NormalizationError(
                f'{key_address_prefix}environment-file',
                raw_fmf_environment_files,
                'unset or a list of paths',
            )

        if raw_fmf_environment is None:
            pass
        elif isinstance(raw_fmf_environment, dict):
            from_fmf_dict = Environment.from_dict(raw_fmf_environment)
        else:
            raise NormalizationError(
                f'{key_address_prefix}environment', raw_fmf_environment, 'unset or a dictionary'
            )

        if raw_cli_environment_files is None:
            pass
        elif isinstance(raw_cli_environment_files, (list, tuple)):
            from_cli_files = Environment.from_files(
                filenames=raw_cli_environment_files,
                root=file_root,
                logger=logger,
            )
        else:
            raise NormalizationError(
                'environment-file', raw_cli_environment_files, 'unset or a list of paths'
            )

        if raw_cli_environment is None:
            pass
        elif isinstance(raw_cli_environment, (list, tuple)):
            from_cli = Environment.from_sequence(
                variables=list(raw_cli_environment),
                logger=logger,
            )
        else:
            raise NormalizationError(
                'environment', raw_cli_environment, 'unset or a list of key/value pairs'
            )

        # Combine all sources into one mapping, honor the order in which they override
        # other sources.
        return Environment(
            {
                **from_fmf_files,
                **from_fmf_dict,
                **from_cli_files,
                **from_cli,
            }
        )

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]] = None) -> 'Environment':
        """
        Create environment variables from a dictionary
        """

        if not data:
            return Environment()

        return Environment({str(key): EnvVarValue(str(value)) for key, value in data.items()})

    @classmethod
    def from_environ(cls) -> 'Environment':
        """
        Extract environment variables from the live environment
        """

        return Environment({key: EnvVarValue(value) for key, value in os.environ.items()})

    @classmethod
    def from_fmf_context(cls, fmf_context: FmfContext) -> 'Environment':
        """
        Create environment variables from an fmf context
        """

        return Environment(
            {key: EnvVarValue(','.join(value)) for key, value in fmf_context.items()}
        )

    @classmethod
    def from_fmf_spec(cls, data: Optional[dict[str, Any]] = None) -> 'Environment':
        """
        Create environment from an fmf specification
        """

        if not data:
            return Environment()

        return Environment({key: EnvVarValue(str(value)) for key, value in data.items()})

    def to_fmf_spec(self) -> dict[str, str]:
        """
        Convert to an fmf specification
        """

        return {key: str(value) for key, value in self.items()}

    def to_popen(self) -> dict[str, str]:
        """
        Convert to a form accepted by :py:class:`subprocess.Popen`
        """

        return self.to_environ()

    def to_environ(self) -> dict[str, str]:
        """
        Convert to a form compatible with :py:attr:`os.environ`
        """

        return {key: str(value) for key, value in self.items()}

    def copy(self) -> 'Environment':
        return Environment(self)

    @classmethod
    def normalize(
        cls,
        key_address: str,
        value: Any,
        logger: tmt.log.Logger,
    ) -> 'Environment':
        """
        Normalize value of ``environment`` key
        """

        # Note: this normalization callback is an exception, it does not
        # bother with CLI input. Environment handling is complex, and CLI
        # options have their special handling. The `environment` as an
        # fmf key does not really have a 1:1 CLI option, the corresponding
        # options are always "special".
        if value is None:
            return cls()

        if isinstance(value, dict):
            return cls({k: EnvVarValue(str(v)) for k, v in value.items()})

        raise NormalizationError(key_address, value, 'unset or a dictionary')

    @contextlib.contextmanager
    def as_environ(self) -> Iterator[None]:
        """
        A context manager replacing :py:attr:`os.environ` with this environment.

        When left, the original content of ``os.environ`` is restored.

        .. warning::

            This method is not thread safe! Beware of using it in code
            that runs in multiple threads, e.g. from
            provision/prepare/execute/finish phases.
        """

        environ_backup = os.environ.copy()
        os.environ.clear()
        os.environ.update(self)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(environ_backup)


# Workdir argument type, can be True, a string, a path or None
WorkdirArgumentType = Union[Literal[True], Path, None]

# Workdir type, can be None or a path
WorkdirType = Optional[Path]

# Option to skip to initialize work tree in plan
PLAN_SKIP_WORKTREE_INIT = 'plan_skip_worktree_init'

# List of schemas that need to be ignored in a plan
PLAN_SCHEMA_IGNORED_IDS: list[str] = [
    '/schemas/provision/hardware',
    '/schemas/provision/kickstart',
]


# TODO: `StreamLogger` is a dedicated thread following given stream, passing their content to
# tmt's logging methods. Thread is needed because of some amount of blocking involved in the
# process, but it has a side effect of `NO_COLOR` envvar being ignored. When tmt spots `NO_COLOR`
# envvar, it flips a `color` flag in its Click context. But since contexts are thread-local,
# thread powering `StreamLogger` is not aware of this change, and all Click methods it calls
# - namely `echo` and `style` in depths of logging code - would still apply colors depending on
# tty setup.
#
# Passing Click context from the main thread to `StreamLogger` instances to replace their context
# is one way to solve it, another might be logging being more explicit and transparent, e.g. with
# https://github.com/teemtee/tmt/issues/1565.
class StreamLogger(Thread):
    """
    Reading pipes of running process in threads.

    Code based on:
    https://github.com/packit/packit/blob/main/packit/utils/logging.py#L10
    """

    def __init__(
        self,
        log_header: str,
        *,
        stream: Optional[IO[bytes]] = None,
        logger: Optional[tmt.log.LoggingFunction] = None,
        click_context: Optional[click.Context] = None,
        stream_output: bool = True,
    ) -> None:
        super().__init__(daemon=True)

        self.stream = stream
        self.output: list[str] = []
        self.log_header = log_header
        self.logger = logger
        self.click_context = click_context
        self.stream_output = stream_output

    def run(self) -> None:
        if self.stream is None:
            return

        if self.logger is None:
            return

        if self.click_context is not None:
            click.globals.push_context(self.click_context)

        for _line in self.stream:
            line = _line.decode('utf-8', errors='replace')
            if self.stream_output and line != '':
                self.logger(self.log_header, line.rstrip('\n'), 'yellow', level=3)
            self.output.append(line)

    def get_output(self) -> Optional[str]:
        return "".join(self.output)


class UnusedStreamLogger(StreamLogger):
    """
    Special variant of :py:class:`StreamLogger` that records no data.

    It is designed to make the implementation of merged streams easier in
    :py:meth:`Command.run`. Instance of this class is created to log ``stderr``
    when, in fact, ``stderr`` is merged into ``stdout``. This class returns
    values compatible with :py:class:`CommandOutput` notion of "no output".
    """

    def __init__(self, log_header: str) -> None:
        super().__init__(log_header)

    def run(self) -> None:
        pass

    def get_output(self) -> Optional[str]:
        return None


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Common
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

CommonDerivedType = TypeVar('CommonDerivedType', bound='Common')

#: A single element of command-line.
_CommandElement = str
#: A single element of raw command line in its ``list`` form.
RawCommandElement = Union[str, Path]
#: A raw command line form, a list of elements.
RawCommand = list[RawCommandElement]

#: Type of a callable to be called by :py:meth:`Command.run` after starting the
#: child process.
OnProcessStartCallback = Callable[
    ['Command', subprocess.Popen[bytes], tmt.log.Logger],
    None,
]

#: Type of a callable to be called by :py:meth:`Command.run` after the
#: child process finishes.
OnProcessEndCallback = Callable[
    ['Command', subprocess.Popen[bytes], 'CommandOutput', tmt.log.Logger],
    None,
]


@container(frozen=True)
class CommandOutput:
    stdout: Optional[str]
    stderr: Optional[str]


class ShellScript:
    """
    A shell script, a free-form blob of text understood by a shell.
    """

    def __init__(self, script: str) -> None:
        """
        A shell script, a free-form blob of text understood by a shell.

        :param script: the actual script to be encapsulated by ``ShellScript``
            wrapper.
        """

        self._script = textwrap.dedent(script)

    def __str__(self) -> str:
        return self._script

    def __add__(self, other: 'ShellScript') -> 'ShellScript':
        if not other:
            return self

        return ShellScript.from_scripts([self, other])

    def __and__(self, other: 'ShellScript') -> 'ShellScript':
        if not other:
            return self

        return ShellScript(f'{self} && {other}')

    def __or__(self, other: 'ShellScript') -> 'ShellScript':
        if not other:
            return self

        return ShellScript(f'{self} || {other}')

    def __bool__(self) -> bool:
        return bool(self._script)

    @classmethod
    def from_scripts(cls, scripts: list['ShellScript']) -> 'ShellScript':
        """
        Create a single script from many shorter ones.

        Scripts are merged into a single ``ShellScript`` instance, joined
        together with ``;`` character.

        :param scripts: scripts to merge into one.
        """

        return ShellScript('; '.join(script._script for script in scripts if bool(script)))

    def to_element(self) -> _CommandElement:
        """
        Convert a shell script to a command element
        """

        return self._script

    def to_shell_command(self) -> 'Command':
        """
        Convert a shell script into a shell-driven command.

        Turns a shell script into a full-fledged command one might pass to the OS.
        Basically what would ``run(script, shell=True)`` do.
        """

        return Command(DEFAULT_SHELL, '-c', self.to_element())


class Command:
    """
    A command with its arguments.
    """

    def __init__(self, *elements: RawCommandElement) -> None:
        self._command = [str(element) for element in elements]

    def __str__(self) -> str:
        return self.to_element()

    def __add__(self, other: Union['Command', RawCommand, list[str]]) -> 'Command':
        if isinstance(other, Command):
            return Command(*self._command, *other._command)

        return Command(*self._command, *other)

    def to_element(self) -> _CommandElement:
        """
        Convert a command to a shell command line element.

        Use when a command or just a list of command options should become a part
        of another command. Common examples of such "higher level" commands
        would be would be ``rsync -e`` or ``ansible-playbook --ssh-common-args``.
        """

        return ' '.join(shlex.quote(s) for s in self._command)

    def to_script(self) -> ShellScript:
        """
        Convert a command to a shell script.

        Use when a command is supposed to become a part of a shell script.
        """

        return ShellScript(' '.join(shlex.quote(s) for s in self._command))

    def to_popen(self) -> list[str]:
        """
        Convert a command to form accepted by :py:mod:`subprocess.Popen`
        """

        return list(self._command)

    def run(
        self,
        *,
        cwd: Optional[Path],
        shell: bool = False,
        env: Optional[Environment] = None,
        dry: bool = False,
        join: bool = False,
        interactive: bool = False,
        timeout: Optional[int] = None,
        on_process_start: Optional[OnProcessStartCallback] = None,
        on_process_end: Optional[OnProcessEndCallback] = None,
        # Logging
        message: Optional[str] = None,
        friendly_command: Optional[str] = None,
        log: Optional[tmt.log.LoggingFunction] = None,
        silent: bool = False,
        stream_output: bool = True,
        caller: Optional['Common'] = None,
        logger: tmt.log.Logger,
    ) -> CommandOutput:
        """
        Run command, give message, handle errors.

        :param cwd: if set, command would be executed in the given directory,
            otherwise the current working directory is used.
        :param shell: if set, the command would be executed in a shell.
        :param env: environment variables to combine with the current environment
            before running the command.
        :param dry: if set, the command would not be actually executed.
        :param join: if set, stdout and stderr of the command would be merged into
            a single output text.
        :param interactive: if set, the command would be executed in an interactive
            manner, i.e. with stdout and stdout connected to terminal for live
            interaction with user.
        :param timeout: if set, command would be interrupted, if still running,
            after this many seconds.
        :param on_process_start: if set, this callable would be called after the
            command process started.
        :param on_process_end: if set, this callable would be called after the
            command process finishes.
        :param message: if set, it would be logged for more friendly logging.
        :param friendly_command: if set, it would be logged instead of the
            command itself, to improve visibility of the command in logging output.
        :param log: a logging function to use for logging of command output. By
            default, ``logger.debug`` is used.
        :param silent: if set, logging of steps taken by this function would be
            reduced.
        :param stream_output: if set, command output would be streamed
            live into the log. When unset, the output would be logged
            only when the command fails.
        :param caller: optional "parent" of the command execution, used for better
            linked exceptions.
        :param logger: logger to use for logging.
        :returns: command output, bundled in a :py:class:`CommandOutput` tuple.
        """

        # A bit of logging - command, default message, error message for later...

        # First, if we were given a message, emit it.
        if message:
            logger.verbose(message, level=2)

        # For debugging, we want to save somewhere the actual command rather
        # than the provided "friendly". Emit the actual command to the debug
        # log, and the friendly one to the verbose/custom log
        logger.debug(f'Run command: {self!s}', level=2)

        # The friendly command version would be emitted only when we were not
        # asked to be quiet.
        if not silent and friendly_command:
            (log or logger.verbose)("cmd", friendly_command, color="yellow", level=2)

        # Nothing more to do in dry mode
        if dry:
            return CommandOutput(None, None)

        # Fail nicely if the working directory does not exist
        if cwd and not cwd.exists():
            raise GeneralError(f"The working directory '{cwd}' does not exist.")

        # For command output logging, use either the given logging callback, or
        # use the given logger & emit to debug log.
        output_logger = (log or logger.debug) if not silent else logger.debug

        # Prepare the environment: use the current process environment, but do
        # not modify it if caller wants something extra, make a copy.
        actual_env: Optional[Environment] = None

        # Do not modify current process environment
        if env is not None:
            actual_env = Environment.from_environ()
            actual_env.update(env)

        logger.debug('environment', actual_env, level=4)

        # Set special executable only when shell was requested
        executable = DEFAULT_SHELL if shell else None

        if interactive:

            def _spawn_process() -> subprocess.Popen[bytes]:
                return subprocess.Popen(
                    self.to_popen(),
                    cwd=cwd,
                    shell=shell,
                    env=actual_env.to_popen() if actual_env is not None else None,
                    # Disabling for now: When used together with the
                    # local provision this results into errors such as:
                    # 'cannot set terminal process group: Inappropriate
                    # ioctl for device' and 'no job control in this
                    # shell'. Let's investigate later why this happens.
                    # start_new_session=True,
                    stdin=None,
                    stdout=None,
                    stderr=None,
                    executable=executable,
                )

        else:

            def _spawn_process() -> subprocess.Popen[bytes]:
                return subprocess.Popen(
                    self.to_popen(),
                    cwd=cwd,
                    shell=shell,
                    env=actual_env.to_popen() if actual_env is not None else None,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if join else subprocess.PIPE,
                    executable=executable,
                )

        # Spawn the child process
        try:
            process = _spawn_process()

        except FileNotFoundError as exc:
            raise RunError(f"File '{exc.filename}' not found.", self, 127, caller=caller) from exc

        if on_process_start:
            on_process_start(self, process, logger)

        if not interactive:
            # Create and start stream loggers
            stdout_logger = StreamLogger(
                'out',
                stream=process.stdout,
                logger=output_logger,
                click_context=click.get_current_context(silent=True),
                stream_output=stream_output,
            )

            if join:
                stderr_logger: StreamLogger = UnusedStreamLogger('err')

            else:
                stderr_logger = StreamLogger(
                    'err',
                    stream=process.stderr,
                    logger=output_logger,
                    click_context=click.get_current_context(silent=True),
                    stream_output=stream_output,
                )

            stdout_logger.start()
            stderr_logger.start()

        # A bit of logging helpers for debugging duration behavior
        start_timestamp = time.monotonic()

        def _event_timestamp() -> str:
            return f'{time.monotonic() - start_timestamp:.4}'

        def log_event(msg: str) -> None:
            logger.debug(
                'Command event',
                f'{_event_timestamp()} {msg}',
                level=4,
                topic=tmt.log.Topic.COMMAND_EVENTS,
            )

        log_event('waiting for process to finish')

        try:
            process.wait(timeout=timeout)

        except subprocess.TimeoutExpired:
            log_event(f'duration "{timeout}" exceeded')

            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            log_event('sent SIGKILL signal')

            process.wait()
            log_event('kill confirmed')

            process.returncode = ProcessExitCodes.TIMEOUT

        else:
            log_event('waiting for process completed')

        stdout: Optional[str]
        stderr: Optional[str]

        if interactive:
            log_event('stream readers not active')

            stdout, stderr = None, None

        else:
            log_event('waiting for stream readers')

            stdout_logger.join()
            log_event('stdout reader done')

            stderr_logger.join()
            log_event('stderr reader done')

            stdout, stderr = stdout_logger.get_output(), stderr_logger.get_output()

        logger.debug(
            f"Command returned '{process.returncode}' "
            f"({ProcessExitCodes.format(process.returncode)}).",
            level=3,
        )

        output = CommandOutput(stdout, stderr)

        if on_process_end is not None:
            try:
                on_process_end(self, process, output, logger)

            except Exception as exc:
                tmt.utils.show_exception_as_warning(
                    exception=exc,
                    message=f'On-process-end callback {on_process_end.__name__} failed.',
                    logger=logger,
                )

        # Handle the exit code, return output
        if process.returncode != ProcessExitCodes.SUCCESS:
            if not stream_output:
                if stdout is not None:
                    for line in stdout.splitlines():
                        output_logger('out', value=line, color='yellow', level=3)

                if stderr is not None:
                    for line in stderr.splitlines():
                        output_logger('err', value=line, color='yellow', level=3)

            raise RunError(
                f"Command '{friendly_command or str(self)}' returned {process.returncode}.",
                self,
                process.returncode,
                stdout=stdout,
                stderr=stderr,
                caller=caller,
            )

        return output


_SANITIZE_NAME_PATTERN: Pattern[str] = re.compile(r'[^\w/-]+')
_SANITIZE_NAME_PATTERN_NO_SLASH: Pattern[str] = re.compile(r'[^\w-]+')


def sanitize_name(name: str, allow_slash: bool = True) -> str:
    """
    Create a safe variant of a name that does not contain special characters.

    Spaces and other special characters are removed to prevent problems with
    tools which do not expect them (e.g. in directory names).

    :param name: a name to sanitize.
    :param allow_slash: if set, even a slash character, ``/``, would be replaced.
    """

    pattern = _SANITIZE_NAME_PATTERN if allow_slash else _SANITIZE_NAME_PATTERN_NO_SLASH

    return pattern.sub('-', name).strip('-')


class _CommonBase:
    """
    A base class for **all** classes contributing to "common" tree of classes.

    All classes derived from :py:class:`Common` or mixin classes used to enhance
    classes derived from :py:class:`Common` need to have this class as one of
    its most distant ancestors. They should not descend directly from ``object``
    class, ``_CommonBase`` needs to be used instead.

    Our classes and mixins use keyword-only arguments, and with mixins in play,
    we do not have a trivial single-inheritance tree, therefore it's not simple
    to realize when a ``super().__init__`` belongs to ``object``. To deliver
    arguments to all classes, our ``__init__()`` methods must accept all
    parameters, even those they have no immediate use for, and propagate them
    via ``**kwargs``. Sooner or later, one of the classes would try to call
    ``object.__init__(**kwargs)``, but this particular ``__init__()`` accepts
    no keyword arguments, which would lead to an exception.

    ``_CommonBase`` sits at the root of the inheritance tree, and is responsible
    for calling ``object.__init__()`` *with no arguments*. Thanks to method
    resolution order, all "branches" of our tree of common classes should lead
    to ``_CommonBase``, making sure the call to ``object`` is correct. To behave
    correctly, ``_CommonBase`` needs to check which class is the next in the MRO
    sequence, and stop propagating arguments.
    """

    def __init__(self, **kwargs: Any) -> None:
        mro = type(self).__mro__
        # ignore[name-defined]: mypy does not recognize __class__, but it
        # exists and it's documented.
        # https://peps.python.org/pep-3135/
        # https://github.com/python/mypy/issues/4177
        parent = mro[mro.index(__class__) + 1]  # type: ignore[name-defined]

        if parent in (object, Generic):
            super().__init__()

        else:
            super().__init__(**kwargs)


class _CommonMeta(type):
    """
    A meta class for all :py:class:`Common` classes.

    Takes care of properly resetting :py:attr:`Common.cli_invocation` attribute
    that cannot be shared among classes.
    """

    def __init__(cls, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # TODO: repeat type annotation from `Common` - IIUIC, `cls` should be
        # the class being created, in our case that would be a subclass of
        # `Common`. For some reason, mypy is uncapable of detecting annotation
        # of this attribute in `Common`, and infers its type is `None` because
        # of the assignment below. That's incomplete, and leads to mypy warning
        # about assignments of `CliInvocation` instances to this attribute.
        # Repeating the annotation silences mypy, giving it better picture.
        cls.cli_invocation: Optional[tmt.cli.CliInvocation] = None


class Common(_CommonBase, metaclass=_CommonMeta):
    """
    Common shared stuff

    Takes care of command line context, options and workdir handling.
    Provides logging functions info(), verbose() and debug().
    Implements read() and write() for comfortable file access.
    Provides the run() method for easy command execution.
    """

    # When set to true, _opt will be ignored (default will be returned)
    ignore_class_options: bool = False
    _workdir: WorkdirType = None
    _clone_dirpath: Optional[Path] = None

    # TODO: must be declared outside of __init__(), because it must exist before
    # __init__() gets called to allow logging helpers work correctly when used
    # from mixins. But that's not very clean, is it? :( Maybe decoupling logging
    # from Common class would help, such a class would be able to initialize
    # itself without involving the rest of Common code. On the other hand,
    # Common owns workdir, for example, whose value affects logging too, so no
    # clear solution so far.
    #
    # Note: cannot use CommonDerivedType - it's a TypeVar filled in by the type
    # given to __init__() and therefore the type it's representing *now* is
    # unknown. but we know `parent` will be derived from `Common` class, so it's
    # mostly fine.
    parent: Optional['Common'] = None

    # Store actual name and safe name. When `name` changes, we need to update
    # `safe_name` accordingly. Direct access not encouraged, use `name` and
    # `safe_name` attributes.
    _name: str

    def inject_logger(self, logger: tmt.log.Logger) -> None:
        self._logger = logger

    def __init__(
        self,
        *,
        parent: Optional[CommonDerivedType] = None,
        name: Optional[str] = None,
        workdir: WorkdirArgumentType = None,
        workdir_root: Optional[Path] = None,
        relative_indent: int = 1,
        cli_invocation: Optional['tmt.cli.CliInvocation'] = None,
        logger: tmt.log.Logger,
        **kwargs: Any,
    ) -> None:
        """
        Initialize name and relation with the parent object

        Prepare the workdir for provided id / directory path
        or generate a new workdir name if workdir=True given.
        Store command line context and options for future use
        if context is provided.
        """

        super().__init__(
            parent=parent,
            name=name,
            workdir=workdir,
            relative_indent=relative_indent,
            logger=logger,
            **kwargs,
        )

        # Use lowercase class name as the default name
        self.name = name or self.__class__.__name__.lower()
        self.parent = parent

        self._workdir_root = workdir_root
        self.cli_invocation = cli_invocation

        self.inject_logger(logger)

        # Relative log indent level shift against the parent
        self._relative_indent = relative_indent

        # Initialize the workdir if requested
        self._workdir_load(workdir)

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, name: str) -> None:
        self._name = name

        # Reset safe name - when accessed next time, it'd be recomputed from
        # the name we just set.
        if 'safe_name' in self.__dict__:
            delattr(self, 'safe_name')

    @functools.cached_property
    def safe_name(self) -> str:
        """
        A safe variant of the name which does not contain special characters.

        Spaces and other special characters are removed to prevent problems with
        tools which do not expect them (e.g. in directory names).

        Unlike :py:meth:`pathless_safe_name`, this property preserves
        slashes, ``/``.
        """

        return sanitize_name(self.name)

    @functools.cached_property
    def pathless_safe_name(self) -> str:
        """
        A safe variant of the name which does not contain any special characters.

        Unlike :py:attr:`safe_name`, this property removes even slashes, ``/``.
        """

        return sanitize_name(self.name, allow_slash=False)

    def __str__(self) -> str:
        """
        Name is the default string representation
        """

        return self.name

    #
    # Invokability via CLI
    #

    # CLI invocation of (sub)command represented by the class or instance.
    # When Click subcommand (or "group" command) runs, saves the Click context
    # in a class corresponding to the subcommand/group. For example, in command
    # like `tmt run report -h foo --bar=baz`, `report` subcommand would save
    # its context inside `tmt.steps.report.Report` class.
    #
    # The context can be also saved on the instance level, for more fine-grained
    # context tracking.
    #
    # The "later use" means the context is often used when looking for options
    # like --how or --dry, may affect step data from fmf or even spawn new phases.
    cli_invocation: Optional['tmt.cli.CliInvocation'] = None

    @classmethod
    def store_cli_invocation(
        cls,
        context: Optional['tmt.cli.Context'],
        options: Optional[dict[str, Any]] = None,
    ) -> 'tmt.cli.CliInvocation':
        """
        Record a CLI invocation and options it carries for later use.

        .. warning::

           The given context is saved into a class variable, therefore it will
           function as a "default" context for instances on which
           :py:meth:`store_cli_invocation` has not been called.

        :param context: CLI context representing the invocation.
        :param options: Optional dictionary with custom options.
            If provided, context is ignored.
        :raises GeneralError: when there was a previously saved invocation
            already. Multiple invocations are not allowed.
        """

        if cls.cli_invocation is not None:
            raise GeneralError(
                f"{cls.__name__} attempted to save a second CLI context: {cls.cli_invocation}"
            )

        if options is not None:
            cls.cli_invocation = tmt.cli.CliInvocation.from_options(options)
        elif context is not None:
            cls.cli_invocation = tmt.cli.CliInvocation.from_context(context)
        else:
            raise GeneralError(
                "Either context or options have to be provided to store_cli_invocation()."
            )

        return cls.cli_invocation

    @property
    def _purely_inherited_cli_invocation(self) -> Optional['tmt.cli.CliInvocation']:
        """
        CLI invocation attached to a parent of this instance.

        :returns: a class-level CLI invocation, the first one attached to
            parent class or its parent classes.
        """

        for klass in self.__class__.__mro__:
            if not issubclass(klass, Common):
                continue

            if klass.cli_invocation:
                return klass.cli_invocation

        return None

    @property
    def _inherited_cli_invocation(self) -> Optional['tmt.cli.CliInvocation']:
        """
        CLI invocation attached to this instance or its parents.

        :returns: instance-level CLI invocation, or, if there is none,
            current class and its parent classes are inspected for their
            class-level invocations.
        """

        if self.cli_invocation is not None:
            return self.cli_invocation

        return self._purely_inherited_cli_invocation

    @property
    def _cli_context_object(self) -> Optional['tmt.cli.ContextObject']:
        """
        A CLI context object attached to the CLI invocation.

        :returns: a CLI context object, or ``None`` if there is no
            CLI invocation attached to this instance or any of its
            parent classes.
        """

        invocation = self._inherited_cli_invocation

        if invocation is None:
            return None

        if invocation.context is None:
            return None

        return invocation.context.obj

    @property
    def _cli_options(self) -> dict[str, Any]:
        """
        CLI options attached to the CLI invocation.

        :returns: CLI options, or an empty dictionary if there is no
            CLI invocation attached to this instance or any of its
            parent classes.
        """

        invocation = self._inherited_cli_invocation

        if invocation is None:
            return {}

        return invocation.options

    @property
    def _cli_fmf_context(self) -> FmfContext:
        """
        An fmf context attached to the CLI invocation.

        :returns: an fmf context, or an empty fmf context if there
            is no CLI invocation attached to this instance or any of
            its parent classes.
        """

        if self._cli_context_object is None:
            return FmfContext()

        return self._cli_context_object.fmf_context

    @property
    def _fmf_context(self) -> FmfContext:
        """
        An fmf context set for this object.
        """

        # By default, the only fmf context available is one provided via CLI.
        # But some derived classes can and will override this, because fmf
        # context can exist in fmf nodes, too.
        return self._cli_fmf_context

    @overload
    @classmethod
    def _opt(cls, option: str) -> Any:
        pass

    @overload
    @classmethod
    def _opt(cls, option: str, default: T) -> T:
        pass

    @classmethod
    def _opt(cls, option: str, default: Any = None) -> Any:
        """
        Get an option from the command line context (class version)
        """

        if cls.ignore_class_options:
            return default

        if cls.cli_invocation is None:
            return default

        return cls.cli_invocation.options.get(option, default)

    def opt(self, option: str, default: Optional[Any] = None) -> Any:
        """
        Get an option from the command line options

        Checks also parent options. For flags (boolean values) parent's
        True wins over child's False (e.g. run --quiet enables quiet
        mode for all included plans and steps).

        For options that can be used multiple times, the child overrides
        the parent if it was defined (e.g. run -av provision -vvv runs
        all steps except for provision in mildly verbose mode, provision
        is run with the most verbosity).

        Environment variables override command line options.
        """

        # Translate dashes to underscores to match click's conversion
        option = option.replace('-', '_')

        # Get local option
        local = (
            self._inherited_cli_invocation.options.get(option, default)
            if self._inherited_cli_invocation
            else None
        )

        # Check parent option
        parent = None
        if self.parent:
            parent = self.parent.opt(option)
        return parent if parent is not None else local

    @property
    def debug_level(self) -> int:
        """
        The current debug level applied to this object
        """

        return self._logger.debug_level

    @debug_level.setter
    def debug_level(self, level: int) -> None:
        """
        Update the debug level attached to this object
        """

        self._logger.debug_level = level

    @property
    def verbosity_level(self) -> int:
        """
        The current verbosity level applied to this object
        """

        return self._logger.verbosity_level

    @verbosity_level.setter
    def verbosity_level(self, level: int) -> None:
        """
        Update the verbosity level attached to this object
        """

        self._logger.verbosity_level = level

    @property
    def quietness(self) -> bool:
        """
        The current quietness level applied to this object
        """

        return self._logger.quiet

    # TODO: interestingly, the option has its own default, right? So why do we
    # need a default of our own? Because sometimes commands have not been
    # invoked, and there's no CLI invocation to ask for the default value.
    # Maybe we should add some kind of "default invocation"...
    def _get_cli_flag(self, key: str, option: str, default: bool) -> bool:
        """
        Find the eventual value of a CLI-provided flag option.

        :param key: in the tree of :py:class:`Common` instance, the
            flag is represented by this attribute.
        :param option: a CLI option name of the flag.
        :param default: default value if the option has not been specified.
        """

        if self.parent:
            parent = cast(bool, getattr(self.parent, key))

            if parent:
                return parent

        invocation = self._inherited_cli_invocation

        if invocation and option in invocation.options:
            return cast(bool, invocation.options[option])

        invocation = self._purely_inherited_cli_invocation

        if invocation and option in invocation.options:
            return cast(bool, invocation.options[option])

        return default

    @property
    def is_dry_run(self) -> bool:
        """
        Whether the current run is a dry-run
        """

        return self._get_cli_flag('is_dry_run', 'dry', False)

    @property
    def is_forced_run(self) -> bool:
        """
        Whether the current run is allowed to overwrite files and data
        """

        return self._get_cli_flag('is_forced_run', 'force', False)

    @property
    def should_run_again(self) -> bool:
        """
        Whether selected step or the whole run should be run again
        """

        return self._get_cli_flag('should_run_again', 'again', False)

    @property
    def is_feeling_safe(self) -> bool:
        """
        Whether the current run is allowed to run unsafe actions
        """

        return self._get_cli_flag('is_feeling_safe', 'feeling_safe', False)

    def _level(self) -> int:
        """
        Hierarchy level
        """

        if self.parent is None:
            return -1
        return self.parent._level() + self._relative_indent

    def _indent(
        self,
        key: str,
        value: Optional[str] = None,
        color: 'tmt.utils.themes.Style' = None,
        shift: int = 0,
    ) -> str:
        """
        Indent message according to the object hierarchy
        """

        return tmt.log.indent(key, value=value, color=color, level=self._level() + shift)

    def print(
        self,
        text: str,
        color: 'tmt.utils.themes.Style' = None,
    ) -> None:
        """
        Print out an output.

        This method is supposed to be used for emitting a command output. Not
        to be mistaken with logging - errors, warnings, general command progress,
        and so on.

        ``print()`` emits even when ``--quiet`` is used, as the option suppresses
        **logging** but not the actual command output.
        """

        self._logger.print(text, color=color)

    def info(
        self,
        key: str,
        value: Optional[LoggableValue] = None,
        color: 'tmt.utils.themes.Style' = None,
        shift: int = 0,
        topic: Optional[tmt.log.Topic] = None,
    ) -> None:
        """
        Show a message unless in quiet mode
        """

        self._logger.info(key, value=value, color=color, shift=shift, topic=topic)

    def verbose(
        self,
        key: str,
        value: Optional[LoggableValue] = None,
        color: 'tmt.utils.themes.Style' = None,
        shift: int = 0,
        level: int = 1,
        topic: Optional[tmt.log.Topic] = None,
    ) -> None:
        """
        Show message if in requested verbose mode level

        In quiet mode verbose messages are not displayed.
        """

        self._logger.verbose(key, value=value, color=color, shift=shift, level=level, topic=topic)

    def debug(
        self,
        key: str,
        value: Optional[LoggableValue] = None,
        color: 'tmt.utils.themes.Style' = None,
        shift: int = 0,
        level: int = 1,
        topic: Optional[tmt.log.Topic] = None,
    ) -> None:
        """
        Show message if in requested debug mode level

        In quiet mode debug messages are not displayed.
        """

        self._logger.debug(key, value=value, color=color, shift=shift, level=level, topic=topic)

    def warn(self, message: str, shift: int = 0) -> None:
        """
        Show a yellow warning message on info level, send to stderr
        """

        self._logger.warning(message, shift=shift)

    def fail(self, message: str, shift: int = 0) -> None:
        """
        Show a red failure message on info level, send to stderr
        """

        self._logger.fail(message, shift=shift)

    def _command_verbose_logger(
        self,
        key: str,
        value: Optional[str] = None,
        color: 'tmt.utils.themes.Style' = None,
        shift: int = 1,
        level: int = 3,
        topic: Optional[tmt.log.Topic] = None,
    ) -> None:
        """
        Reports the executed command in verbose mode.

        This is a tailored verbose() function used for command logging where
        default parameters are adjusted (to preserve the function type).
        """

        self.verbose(key=key, value=value, color=color, shift=shift, level=level, topic=topic)

    def run(
        self,
        command: Command,
        friendly_command: Optional[str] = None,
        silent: bool = False,
        message: Optional[str] = None,
        cwd: Optional[Path] = None,
        ignore_dry: bool = False,
        shell: bool = False,
        env: Optional[Environment] = None,
        interactive: bool = False,
        join: bool = False,
        log: Optional[tmt.log.LoggingFunction] = None,
        timeout: Optional[int] = None,
        on_process_start: Optional[OnProcessStartCallback] = None,
        on_process_end: Optional[OnProcessEndCallback] = None,
    ) -> CommandOutput:
        """
        Run command, give message, handle errors

        Command is run in the workdir be default.
        In dry mode commands are not executed unless ignore_dry=True.
        Environment is updated with variables from the 'env' dictionary.

        Output is logged using self.debug() or custom 'log' function.
        A user friendly command string 'friendly_command' will be shown,
        if provided, at the beginning of the command output.

        Returns named tuple CommandOutput.
        """

        dryrun_actual = self.is_dry_run

        if ignore_dry:
            dryrun_actual = False

        return command.run(
            friendly_command=friendly_command,
            silent=silent,
            message=message,
            cwd=cwd or self.workdir,
            dry=dryrun_actual,
            shell=shell,
            env=env,
            interactive=interactive,
            on_process_start=on_process_start,
            on_process_end=on_process_end,
            join=join,
            log=log,
            timeout=timeout,
            caller=self,
            logger=self._logger,
        )

    def read(self, path: Path, level: int = 2) -> str:
        """
        Read a file from the workdir
        """

        if self.workdir:
            path = self.workdir / path
        self.debug(f"Read file '{path}'.", level=level)
        try:
            return path.read_text(encoding='utf-8', errors='replace')

        except OSError as error:
            raise FileError(f"Failed to read from '{path}'.") from error

    def write(
        self,
        path: Path,
        data: str,
        mode: WriteMode = 'w',
        level: int = 2,
    ) -> None:
        """
        Write a file to the workdir
        """

        if self.workdir:
            path = self.workdir / path
        action = 'Append to' if mode == 'a' else 'Write'
        self.debug(f"{action} file '{path}'.", level=level)
        # Dry mode
        if self.is_dry_run:
            return
        try:
            if mode == 'a':
                path.append_text(data, encoding='utf-8', errors='replace')

            else:
                path.write_text(data, encoding='utf-8', errors='replace')

        except OSError as error:
            raise FileError(f"Failed to write into '{path}' file.") from error

    def _workdir_init(self, id_: WorkdirArgumentType = None) -> None:
        """
        Initialize the work directory

        If 'id' is a path, that directory is used instead. Otherwise a
        new workdir is created under the workdir root directory.
        """

        # Prepare the workdir name from given id or path
        if isinstance(id_, Path):
            # Use provided directory if full path given
            workdir = id_ if '/' in str(id_) else self.workdir_root / id_
            # Resolve any relative paths
            workdir = workdir.resolve()
        # Weird workdir id
        elif id_ is not None:
            raise GeneralError(f"Invalid workdir '{id_}', expected a path or None.")

        def _check_or_create_workdir_root_with_perms() -> None:
            """
            If created workdir_root has to be 1777 for multi-user
            """

            if not self.workdir_root.is_dir():
                try:
                    self.workdir_root.mkdir(exist_ok=True, parents=True)
                    self.workdir_root.chmod(0o1777)
                except OSError as error:
                    raise FileError(f"Failed to prepare workdir '{self.workdir_root}': {error}")

        if id_ is None:
            # Prepare workdir_root first
            _check_or_create_workdir_root_with_perms()

            # Generated unique id or fail, has to be atomic call
            for id_bit in range(1, WORKDIR_MAX + 1):
                directory = f"run-{str(id_bit).rjust(3, '0')}"
                workdir = self.workdir_root / directory
                try:
                    # Call is atomic, no race possible
                    workdir.mkdir(parents=True)
                    break
                except FileExistsError:
                    pass
            else:
                raise GeneralError(f"Workdir full. Cleanup the '{self.workdir_root}' directory.")
        else:
            # Cleanup possible old workdir if called with --scratch
            if self.opt('scratch'):
                self._workdir_cleanup(workdir)

            if workdir.is_relative_to(self.workdir_root):
                _check_or_create_workdir_root_with_perms()

            # Create the workdir
            create_directory(path=workdir, name='workdir', quiet=True, logger=self._logger)

        self._workdir = workdir

        # TODO: chicken and egg problem: when `Common` is instantiated, the workdir
        # path might be already known, but it's often not created yet. Therefore
        # a logfile handler cannot be attached to the given logger.
        # This is a problem, as we modify a given logger, and we may modify the
        # incorrect logger, and we may modify 3rd party app logger. The solution
        # to our little logging problem would probably be related to refactoring
        # of workdir creation some day in the future.
        self._logger.add_logfile_handler(workdir / tmt.log.LOG_FILENAME)

        # Do the same for the bootstrap logger - this logger should not
        # be used by regular code, and by now we should have everything
        # up and running, but some exceptions exist.
        #
        # Do *not* do the same for the *exception* logger - that one is
        # owned by `tmt.utils.show_exception()` which takes care of emitting
        # lines into files as necessary. And while the bootstrap logger is
        # the go-to logger for async code, like signal handlers, the
        # exception logger is not to be used from anywhere but exception
        # logging.
        from tmt._bootstrap import _BOOTSTRAP_LOGGER

        _BOOTSTRAP_LOGGER.add_logfile_handler(workdir / tmt.log.LOG_FILENAME)

    def _workdir_name(self) -> Optional[Path]:
        """
        Construct work directory name from parent workdir
        """

        # Need the parent workdir
        if self.parent is None or self.parent.workdir is None:
            return None
        # Join parent name with self
        return self.parent.workdir / self.safe_name.lstrip("/")

    def _workdir_load(self, workdir: WorkdirArgumentType) -> None:
        """
        Create the given workdir if it is not None

        If workdir=True, the directory name is automatically generated.
        """

        if workdir is True:
            self._workdir_init()
        elif workdir is not None:
            self._workdir_init(workdir)

    def _workdir_cleanup(self, path: Optional[Path] = None) -> None:
        """
        Clean up the work directory
        """

        directory = path or self._workdir_name()
        if directory is not None and directory.is_dir():
            self.debug(f"Clean up workdir '{directory}'.", level=2)
            shutil.rmtree(directory)
        self._workdir = None

    @property
    def workdir(self) -> Optional[Path]:
        """
        Get the workdir, create if does not exist
        """

        if self._workdir is None:
            self._workdir = self._workdir_name()
            # Workdir not enabled, even parent does not have one
            if self._workdir is None:
                return None
            # Create a child workdir under the parent workdir
            create_directory(path=self._workdir, name='workdir', quiet=True, logger=self._logger)

        return self._workdir

    @property
    def clone_dirpath(self) -> Path:
        """
        Path for cloning into

        Used internally for picking specific libraries (or anything
        else) from cloned repos for filtering purposes, it is removed at
        the end of relevant step.
        """

        if not self._clone_dirpath:
            self._clone_dirpath = Path(tempfile.TemporaryDirectory(dir=self.workdir).name)

        return self._clone_dirpath

    @property
    def workdir_root(self) -> Path:
        if self._workdir_root:
            return self._workdir_root
        if self.parent:
            return self.parent.workdir_root
        return effective_workdir_root()

    @workdir_root.setter
    def workdir_root(self, workdir_root: Path) -> None:
        self._workdir_root = workdir_root


class _MultiInvokableCommonMeta(_CommonMeta):
    """
    A meta class for all :py:class:`Common` classes.

    Takes care of properly resetting :py:attr:`Common.cli_invocation` attribute
    that cannot be shared among classes.
    """

    def __init__(cls, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        cls.cli_invocations: list[tmt.cli.CliInvocation] = []


class MultiInvokableCommon(Common, metaclass=_MultiInvokableCommonMeta):
    cli_invocations: list['tmt.cli.CliInvocation']

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    @classmethod
    def store_cli_invocation(
        cls,
        context: Optional['tmt.cli.Context'],
        options: Optional[dict[str, Any]] = None,
    ) -> 'tmt.cli.CliInvocation':
        """
        Save a CLI context and options it carries for later use.

        .. warning::

           The given context is saved into a class variable, therefore it will
           function as a "default" context for instances on which
           :py:meth:`_save_cli_context_to_instance` has not been called.

        .. warning::

           The given context will overwrite any previously saved context.

        :param context: CLI context to save.
        :param options: Optional dictionary with custom options.
            If provided, context is ignored.
        """

        if options is not None:
            invocation = tmt.cli.CliInvocation.from_options(options)
        elif context is not None:
            invocation = tmt.cli.CliInvocation.from_context(context)
        else:
            raise GeneralError(
                "Either context or options have to be provided to store_cli_invocation()."
            )

        cls.cli_invocations.append(invocation)

        cls.cli_invocation = invocation

        return invocation


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Exceptions
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


class GeneralError(Exception):
    """
    General error
    """

    def __init__(
        self,
        message: str,
        causes: Optional[list[Exception]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """
        General error.

        :param message: error message.
        :param causes: optional list of exceptions that caused this one. Since
            ``raise ... from ...`` allows only for a single cause, and some of
            our workflows may raise exceptions triggered by more than one
            exception, we need a mechanism for storing them. Our reporting will
            honor this field, and report causes the same way as ``__cause__``.
        """

        super().__init__(message, *args, **kwargs)

        self.message = message
        self.causes = causes or []


class GitUrlError(GeneralError):
    """
    Remote git url is not reachable
    """


class FileError(GeneralError):
    """
    File operation error
    """


class RunError(GeneralError):
    """
    Command execution error
    """

    def __init__(
        self,
        message: str,
        command: Command,
        returncode: int,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        caller: Optional[Common] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, *args, **kwargs)
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        # Store instance of caller to get additional details
        # in post processing (e.g. verbose level)
        self.caller = caller
        # Since logger may get swapped, to better reflect context (guests start
        # with logger inherited from `provision` but may run under `prepare` or
        # `finish`), save a logger for later.
        self.logger = caller._logger if isinstance(caller, Common) else None

    @functools.cached_property
    def output(self) -> CommandOutput:
        """
        Captured output of the command.

        .. note::

           This field contains basically the same info as :py:attr:`stdout`
           and :py:attr:`stderr`, but it's bundled into a single object.
           This is how command output is passed between functions, therefore
           the exception should offer it as well.
        """

        return CommandOutput(self.stdout, self.stderr)


class MetadataError(GeneralError):
    """
    General metadata error
    """


class SpecificationError(MetadataError):
    """
    Metadata specification error
    """

    def __init__(
        self,
        message: str,
        validation_errors: Optional[list[tuple[jsonschema.ValidationError, str]]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, *args, **kwargs)
        self.validation_errors = validation_errors


class NormalizationError(SpecificationError):
    """
    Raised when a key normalization fails
    """

    def __init__(
        self,
        key_address: str,
        raw_value: Any,
        expected_type: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """
        Raised when a key normalization fails.

        A subclass of :py:class:`SpecificationError`, but describing errors
        that appear in a very specific point of key loading in a unified manner.

        :param key_address: the key in question, preferably with detailed location,
            e.g. ``/plans/foo:discover[0].tests``.
        :param raw_value: input value, the one that failed the normalization.
        :param expected_type: string description of expected, allowed types, as
            a hint in the error message.
        """

        super().__init__(
            f"Field '{key_address}' must be {expected_type}, '{type(raw_value).__name__}' found.",
            *args,
            **kwargs,
        )

        self.key_address = key_address
        self.raw_value = raw_value
        self.expected_type = expected_type


class ConvertError(MetadataError):
    """
    Metadata conversion error
    """


class StructuredFieldError(GeneralError):
    """
    StructuredField parsing error
    """


class RetryError(GeneralError):
    """
    Retries unsuccessful
    """

    def __init__(self, label: str, causes: list[Exception]) -> None:
        super().__init__(f"Retries of '{label}' unsuccessful.", causes)


class BackwardIncompatibleDataError(GeneralError):
    """
    A backward incompatible data cannot be processed
    """


# Step exceptions


class DiscoverError(GeneralError):
    """
    Discover step error
    """


class ProvisionError(GeneralError):
    """
    Provision step error
    """


class PrepareError(GeneralError):
    """
    Prepare step error
    """


class ExecuteError(GeneralError):
    """
    Execute step error
    """


class RebootTimeoutError(ExecuteError):
    """
    Reboot failed due to a timeout
    """


class ReconnectTimeoutError(ExecuteError):
    """
    Failed to reconnect to the guest due to a timeout.
    """


class RestartMaxAttemptsError(ExecuteError):
    """
    Test restart failed due to maximum attempts reached.
    """


class ReportError(GeneralError):
    """
    Report step error
    """


class FinishError(GeneralError):
    """
    Finish step error
    """


class TracebackVerbosity(enum.Enum):
    """
    Levels of logged traveback verbosity
    """

    #: Render only exception and its causes.
    DEFAULT = '0'
    #: Render also call stack for exception and each of its causes.
    VERBOSE = '1'
    #: Render also call stack for exception and each of its causes,
    #: plus all local variables in each frame, trimmed to first 1024
    #: characters of their values.
    LOCALS = '2'
    #: Render everything that can be shown: all causes, their call
    #: stacks, all frames and all locals in their completeness.
    FULL = 'full'

    @classmethod
    def from_spec(cls, spec: str) -> 'TracebackVerbosity':
        try:
            return TracebackVerbosity(spec)

        except ValueError:
            raise SpecificationError(f"Invalid traceback verbosity '{spec}'.")

    @classmethod
    def from_env(cls) -> 'TracebackVerbosity':
        return TracebackVerbosity.from_spec(os.getenv('TMT_SHOW_TRACEBACK', '0').lower())


def render_run_exception_streams(
    output: CommandOutput,
    verbose: int = 0,
    comment_sign: str = '#',
) -> Iterator[str]:
    """
    Render run exception output streams for printing
    """

    for name, content in (('stdout', output.stdout), ('stderr', output.stderr)):
        if not content:
            continue
        content_lines = content.strip().split('\n')
        # Show all lines in verbose mode, limit to maximum otherwise
        if verbose > 0:
            line_summary = f"{len(content_lines)}"
        else:
            line_summary = f"{min(len(content_lines), OUTPUT_LINES)}/{len(content_lines)}"
            content_lines = content_lines[-OUTPUT_LINES:]

        line_intro = f'{comment_sign} '

        yield line_intro + f'{name} ({line_summary} lines)'
        yield line_intro + (OUTPUT_WIDTH - 2) * '~'
        yield from content_lines
        yield line_intro + (OUTPUT_WIDTH - 2) * '~'
        yield ''


@overload
def render_command_report(
    *,
    label: str,
    command: Optional[Union[ShellScript, Command]] = None,
    output: CommandOutput,
    exc: None = None,
) -> Iterator[str]:
    pass


@overload
def render_command_report(
    *,
    label: str,
    command: Optional[Union[ShellScript, Command]] = None,
    output: None = None,
    exc: RunError,
) -> Iterator[str]:
    pass


def render_command_report(
    *,
    label: str,
    command: Optional[Union[ShellScript, Command]] = None,
    output: Optional[CommandOutput] = None,
    exc: Optional[RunError] = None,
    comment_sign: str = '#',
) -> Iterator[str]:
    """
    Format a command output for a report file.

    To provide unified look of various files reporting command outputs,
    this helper would combine its arguments and emit lines the caller
    may then write to a file. The following template is used:

    .. code-block::

        ## ${label}

        # ${command}

        # exit code ${exit_code}

        # stdout (N lines)
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        ...
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

        # stderr (N lines)
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        ...
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    :param label: a string describing the intent of the command. It is
        useful for user who reads the report file eventually.
    :param command: command that was executed.
    :param output: if set, it contains output of the command. It has
        higher priority than ``exc``.
    :param exc: if set, it represents a failed command, and input stored
        in it is rendered.
    :param comment_sign: a character to mark lines with comments that
        document the report.
    """

    yield f'{comment_sign}{comment_sign} {label}'
    yield ''

    if command:
        yield f'{comment_sign} {command.to_element()}'
        yield ''

    if output is not None:
        yield f'{comment_sign} exit code: finished successfully'
        yield ''
        yield from render_run_exception_streams(output, verbose=1)

    elif exc is not None:
        yield f'{comment_sign} exit code: {exc.returncode}'
        yield ''
        yield from render_run_exception_streams(exc.output, verbose=1)


def render_run_exception(exception: RunError) -> Iterator[str]:
    """
    Render detailed output upon command execution errors for printing
    """

    # Check verbosity level used during raising exception,
    if exception.logger:
        verbose = exception.logger.verbosity_level
    elif isinstance(exception.caller, Common):
        verbose = exception.caller.verbosity_level
    else:
        verbose = 0

    yield from render_run_exception_streams(exception.output, verbose=verbose)


def render_exception_stack(
    exception: BaseException,
    traceback_verbosity: TracebackVerbosity = TracebackVerbosity.DEFAULT,
) -> Iterator[str]:
    """
    Render traceback of the given exception
    """

    exception_traceback = traceback.TracebackException(
        type(exception),
        exception,
        exception.__traceback__,
        capture_locals=True,
    )

    # N806: allow upper-case names to make them look like formatting
    # tags in strings below.
    R = functools.partial(style, fg='red')  # noqa: N806
    Y = functools.partial(style, fg='yellow')  # noqa: N806
    B = functools.partial(style, fg='blue')  # noqa: N806

    yield R('Traceback (most recent call last):')
    yield ''

    for frame in exception_traceback.stack:
        yield f'File {Y(frame.filename)}, line {Y(str(frame.lineno))}, in {Y(frame.name)}'
        if frame.line:
            yield f'  {B(frame.line)}'

        if frame.locals:
            yield ''

            if traceback_verbosity is TracebackVerbosity.LOCALS:
                for k, v in frame.locals.items():
                    v_formatted = (
                        (v[:TRACEBACK_LOCALS_TRIM] + '...')
                        if len(v) > TRACEBACK_LOCALS_TRIM
                        else v
                    )

                    yield f'  {B(k)} = {Y(v_formatted)}'

            elif traceback_verbosity is TracebackVerbosity.FULL:
                for k, v in frame.locals.items():
                    yield f'  {B(k)} = {Y(v)}'

            yield ''


def render_exception(
    exception: BaseException,
    traceback_verbosity: TracebackVerbosity = TracebackVerbosity.DEFAULT,
) -> Iterator[str]:
    """
    Render the exception and its causes for printing
    """

    def _indent(iterable: Iterable[str]) -> Iterator[str]:
        for item in iterable:
            if not item:
                yield item

            else:
                for line in item.splitlines():
                    yield f'{INDENT * " "}{line}'

    yield style(str(exception), fg='red')

    if isinstance(exception, RunError):
        yield ''
        yield from render_run_exception(exception)

    if traceback_verbosity is not TracebackVerbosity.DEFAULT:
        yield ''
        yield from _indent(
            render_exception_stack(exception, traceback_verbosity=traceback_verbosity)
        )

    # Follow the chain and render all causes
    def _render_cause(number: int, cause: BaseException) -> Iterator[str]:
        yield ''
        yield f'Cause number {number}:'
        yield ''
        yield from _indent(render_exception(cause, traceback_verbosity=traceback_verbosity))

    def _render_causes(causes: list[BaseException]) -> Iterator[str]:
        yield ''
        yield f'The exception was caused by {len(causes)} earlier exceptions'

        for number, cause in enumerate(causes, start=1):
            yield from _render_cause(number, cause)

    causes: list[BaseException] = []

    if isinstance(exception, GeneralError) and exception.causes:
        causes += exception.causes

    if exception.__cause__:
        causes += [exception.__cause__]

    if causes:
        yield from _render_causes(causes)


def _render_base_exception(
    exception: BaseException, traceback_verbosity: TracebackVerbosity
) -> Iterator[str]:
    """
    A small helper for functions showing exceptions.

    On top of :py:func:`render_exception`, it requires verbosity and
    adds one leading empty line to simplify formatting.

    :param exception: exception to log.
    :param traceback_verbosity: with what verbosity tracebacks should
        be rendered.
    """

    yield ''
    yield from render_exception(exception, traceback_verbosity=traceback_verbosity)


def _render_exception_into_files(exception: BaseException, logger: tmt.log.Logger) -> None:
    """
    Render an exception into known log files.

    :param exception: exception to log.
    :param logger: logger to use for logging.
    """

    logger = logger.clone()
    logger.apply_colors_output = False

    logfile_streams: list[TextIO] = []

    with contextlib.ExitStack() as stack:
        for path in tmt.log.LogfileHandler.emitting_to:
            try:
                # SIM115: all opened files are added on exit stack, and they
                # will get collected and closed properly.
                stream: TextIO = open(path, 'a')  # noqa: SIM115

                logfile_streams.append(stream)
                stack.enter_context(stream)

            except Exception as exc:
                show_exception(
                    GeneralError(f"Cannot log error into logfile '{path}'.", causes=[exc]),
                    include_logfiles=False,
                )

        for line in _render_base_exception(exception, TracebackVerbosity.LOCALS):
            for stream in logfile_streams:
                logger.print(line, file=stream)


def render_exception_as_notes(exception: BaseException) -> list[str]:
    """
    Render an exception as a list of :py:class:`Result` notes.

    Each exception message is recorded, and prefixed with an index
    corresponding to its position among causes of the error state.

    :param exception: exception to render.
    """

    def _render_exception(exc: BaseException, index: str) -> Iterator[str]:
        causes: list[BaseException] = []

        if isinstance(exc, GeneralError) and exc.causes:
            causes += exc.causes

        if exc.__cause__:
            causes += [exc.__cause__]

        yield f'Exception #{index}: {exc}'

        if causes:
            for cause_index, cause_exc in enumerate(causes, 1):
                yield from _render_exception(cause_exc, f'{index}.{cause_index}')

    return list(_render_exception(exception, '1'))


def show_exception(
    exception: BaseException,
    traceback_verbosity: Optional[TracebackVerbosity] = None,
    include_logfiles: bool = True,
) -> None:
    """
    Display the exception and its causes.

    :param exception: exception to log.
    :param include_logfiles: if set, exception will be logged into known
        logfiles as well as to standard error output.
    """

    from tmt._bootstrap import EXCEPTION_LOGGER

    traceback_verbosity = traceback_verbosity or TracebackVerbosity.from_env()

    for line in _render_base_exception(exception, traceback_verbosity):
        EXCEPTION_LOGGER.print(line, file=sys.stderr)

    if include_logfiles:
        _render_exception_into_files(exception, EXCEPTION_LOGGER)


def show_exception_as_warning(
    *,
    exception: BaseException,
    message: str,
    include_logfiles: bool = True,
    logger: tmt.log.Logger,
) -> None:
    """
    Display the exception and its causes as a warning.

    :param exception: exception to log.
    :param message: message to emit as a warning to introduce the
        exception.
    :param include_logfiles: if set, exception will be logged into known
        logfiles as well as to standard error output.
    :param logger: logger to use for logging.
    """

    logger.warning(message)

    for line in _render_base_exception(exception, TracebackVerbosity.DEFAULT):
        logger.warning(line)

    if include_logfiles:
        _render_exception_into_files(exception, logger)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Utilities
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


def uniq(values: list[T]) -> list[T]:
    """
    Return a list of all unique items from ``values``
    """

    return list(set(values))


def duplicates(values: Iterable[Optional[T]]) -> Iterator[T]:
    """
    Iterate over all duplicate values in ``values``
    """

    seen = Counter(values)
    for value, count in seen.items():
        if value is None or count == 1:
            continue
        yield value


def flatten(lists: Iterable[list[T]], unique: bool = False) -> list[T]:
    """
    "Flatten" a list of lists into a single-level list.

    :param lists: an iterable of lists to flatten.
    :param unique: if set, duplicate items would be removed, leaving only
        a single instance in the final list.
    :returns: list of items from all given lists.
    """

    flattened: list[T] = [item for sublist in lists for item in sublist]

    return uniq(flattened) if unique else flattened


def quote(string: str) -> str:
    """
    Surround a string with double quotes
    """

    return f'"{string}"'


def pure_ascii(text: Any) -> bytes:
    """
    Transliterate special unicode characters into pure ascii
    """

    if not isinstance(text, str):
        text = str(text)
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore')


def get_full_metadata(fmf_tree_path: Path, node_path: str) -> Any:
    """
    Get full metadata for a node in any fmf tree

    Go through fmf tree nodes using given relative node path
    and return full data as dictionary.
    """

    try:
        return fmf.Tree(fmf_tree_path).find(node_path).data
    except AttributeError:
        raise MetadataError(f"'{node_path}' not found in the '{fmf_tree_path}' Tree.")


def filter_paths(directory: Path, searching: list[str], files_only: bool = False) -> list[Path]:
    """
    Filter files for specific paths we are searching for inside a directory

    Returns list of matching paths.
    """

    all_paths = list(directory.rglob('*'))  # get all filepaths for given dir recursively
    alldirs = [str(d) for d in all_paths if d.is_dir()]
    allfiles = [str(file) for file in all_paths if not file.is_dir()]
    found_paths: list[str] = []

    for search_string in searching:
        if search_string == '/':
            return all_paths
        regex = re.compile(search_string)

        if not files_only:
            # Search in directories first to reduce amount of copying later
            matches = list(filter(regex.search, alldirs))
            if matches:
                found_paths += matches
                continue

        # Search through all files
        found_paths += list(filter(regex.search, allfiles))
    return [Path(path) for path in set(found_paths)]  # return all matching unique paths as Path's


def dict_to_yaml(
    data: Union[dict[str, Any], list[Any], 'tmt.base._RawFmfId'],
    width: Optional[int] = None,
    sort: bool = False,
    start: bool = False,
) -> str:
    """
    Convert dictionary into yaml
    """

    output = io.StringIO()
    yaml = YAML()
    yaml.indent(mapping=4, sequence=4, offset=2)
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.encoding = 'utf-8'
    # ignore[assignment]: ruamel bug workaround, see stackoverflow.com/questions/58083562,
    # sourceforge.net/p/ruamel-yaml/tickets/322/
    #
    # Yeah, but sometimes the ignore is not needed, at least mypy in a Github
    # check tells us it's unused... When disabled, the local pre-commit fails.
    # It seems we cannot win until ruamel.yaml gets its things fixed, therefore,
    # giving up, and using `cast()` to enforce matching types to silence mypy,
    # being fully aware the enforce types are wrong.
    yaml.width = cast(None, width)  # # type: ignore[assignment]
    yaml.explicit_start = cast(None, start)  # # type: ignore[assignment]

    # For simpler dumping of well-known classes
    def _represent_path(representer: Representer, data: Path) -> Any:
        return representer.represent_scalar('tag:yaml.org,2002:str', str(data))

    yaml.representer.add_representer(pathlib.Path, _represent_path)  # noqa: TID251
    yaml.representer.add_representer(pathlib.PosixPath, _represent_path)  # noqa: TID251
    yaml.representer.add_representer(Path, _represent_path)

    def _represent_environment(representer: Representer, data: Environment) -> Any:
        return representer.represent_mapping('tag:yaml.org,2002:map', data.to_fmf_spec())

    yaml.representer.add_representer(Environment, _represent_environment)

    # Convert multiline strings, sanitize invalid characters. Based on
    # `scalarstring.walk_tree()` which does not support any other test
    # than "is this character in that string?"
    # Prevents saving non-printable characters a YAML parser might later
    # reject - see https://github.com/teemtee/tmt/issues/3805
    def _sanitize_yaml_string(s: str) -> str:
        pattern = ruamel.yaml.reader.Reader.NON_PRINTABLE

        if '\n' in s:
            s = ruamel.yaml.scalarstring.preserve_literal(s)

        return ''.join(rf'#{{{ord(c):x}}}' if pattern.match(c) else c for c in s)

    def walk_tree(value: Any) -> Any:
        from collections.abc import MutableMapping, MutableSequence

        if isinstance(value, MutableMapping):
            for k, v in value.items():
                if isinstance(v, str):
                    value[k] = _sanitize_yaml_string(v)

                else:
                    value[k] = walk_tree(v)

            return value

        if isinstance(value, MutableSequence):
            for k, v in enumerate(value):
                if isinstance(v, str):
                    value[k] = _sanitize_yaml_string(v)

                else:
                    value[k] = walk_tree(v)

            return value

        if isinstance(value, str):
            return _sanitize_yaml_string(value)

        return value

    data = walk_tree(data)

    if sort:
        # Sort the data https://stackoverflow.com/a/40227545
        sorted_data = CommentedMap()
        for key in sorted(data):
            # ignore[literal-required]: `data` may be either a generic
            # dictionary, or _RawFmfId which allows only a limited set
            # of keys. That spooks mypy, but we do not add any keys,
            # therefore we will not escape TypedDict constraints.
            sorted_data[key] = data[key]  # type: ignore[literal-required]
        data = sorted_data
    yaml.dump(data, output)
    return output.getvalue()


YamlTypType = Literal['rt', 'safe', 'unsafe', 'base']


def yaml_to_python(data: Any, yaml_type: Optional[YamlTypType] = None) -> Any:
    """
    Convert YAML into Python data types.
    """

    return YAML(typ=yaml_type).load(data)


def yaml_to_dict(data: Any, yaml_type: Optional[YamlTypType] = None) -> dict[Any, Any]:
    """
    Convert yaml into dictionary
    """

    yaml = YAML(typ=yaml_type)
    loaded_data = yaml.load(data)
    if loaded_data is None:
        return {}
    if not isinstance(loaded_data, dict):
        raise GeneralError(
            f"Expected dictionary in yaml data, got '{type(loaded_data).__name__}'."
        )
    return loaded_data


def yaml_to_list(data: Any, yaml_type: Optional[YamlTypType] = 'safe') -> list[Any]:
    """
    Convert yaml into list
    """

    yaml = YAML(typ=yaml_type)
    try:
        loaded_data = yaml.load(data)
    except ParserError as error:
        raise GeneralError(f"Invalid yaml syntax: {error}")

    if loaded_data is None:
        return []
    if not isinstance(loaded_data, list):
        raise GeneralError(f"Expected list in yaml data, got '{type(loaded_data).__name__}'.")
    return loaded_data


def json_to_list(data: Any) -> list[Any]:
    """
    Convert json into list
    """

    try:
        loaded_data = json.loads(data)
    except json.decoder.JSONDecodeError as error:
        raise GeneralError(f"Invalid json syntax: {error}")

    if not isinstance(loaded_data, list):
        raise GeneralError(f"Expected list in json data, got '{type(loaded_data).__name__}'.")
    return loaded_data


def markdown_to_html(filename: Path) -> str:
    """
    Convert markdown to html

    Expects: Markdown document as a file.
    Returns: An HTML document as a string.
    """

    try:
        import markdown
    except ImportError:
        raise ConvertError("Install tmt+test-convert to export tests.")

    try:
        try:
            return markdown.markdown(filename.read_text())
        except UnicodeError:
            raise MetadataError(f"Unable to read '{filename}'.")
    except OSError:
        raise ConvertError(f"Unable to open '{filename}'.")


def shell_variables(data: Union[list[str], tuple[str, ...], dict[str, Any]]) -> list[str]:
    """
    Prepare variables to be consumed by shell

    Convert dictionary or list/tuple of key=value pairs to list of
    key=value pairs where value is quoted with shlex.quote().
    """

    # Convert from list/tuple
    if isinstance(data, (list, tuple)):
        converted_data = []
        for item in data:
            splitted_item = item.split('=')
            key = splitted_item[0]
            value = shlex.quote('='.join(splitted_item[1:]))
            converted_data.append(f'{key}={value}')
        return converted_data

    # Convert from dictionary
    return [f"{key}={shlex.quote(str(value))}" for key, value in data.items()]


def duration_to_seconds(duration: str, injected_default: Optional[str] = None) -> int:
    """
    Convert extended sleep time format into seconds

    Optional 'injected_default' argument to evaluate 'duration' when
    it contains only multiplication.
    """

    units = {
        's': 1,
        'm': 60,
        'h': 60 * 60,
        'd': 60 * 60 * 24,
    }
    # Couldn't create working validation regexp to accept '2 1m 4'
    # thus fixing the string so \b can be used as word boundary
    fixed_duration = re.sub(r'([smhd])(\d)', r'\1 \2', str(duration))
    fixed_duration = re.sub(r'\s\s+', ' ', fixed_duration)
    raw_groups = r'''
            (   # Group all possibilities
                (  # Multiply by float number
                    (?P<asterisk>\*) # "*" character
                                \s*
                    (?P<float>\d+(\.\d+)?(?![smhd])) # float part
                                \s*
                )
                |   # Or
                ( # Time pattern
                    (?P<digit>\d+)  # digits
                    \s*
                    (?P<suffix>[smhd])? # suffix
                    \s*
                )
            )\b # Needs to end with word boundary to avoid splitting
        '''
    re_validate = re.compile(
        r'''
        ^(  # Match beginning, opening of input group
        '''
        + raw_groups
        + r'''
        \s* # Optional spaces in the case of multiple inputs
        )+$ # Inputs can repeat
        ''',
        re.VERBOSE,
    )
    re_split = re.compile(raw_groups, re.VERBOSE)
    if re_validate.match(fixed_duration) is None:
        raise SpecificationError(f"Invalid duration '{duration}'.")
    total_time = 0
    multiply_by = 1.0
    for match in re_split.finditer(fixed_duration):
        if match['asterisk'] == '*':
            multiply_by *= float(match['float'])
        else:
            total_time += int(match['digit']) * units.get(match['suffix'], 1)
    # Inject value so we have something to multiply
    if injected_default and total_time == 0:
        total_time = duration_to_seconds(injected_default)
    # Multiply in the end and round up
    return ceil(total_time * multiply_by)


@overload
def verdict(
    decision: bool,
    comment: Optional[str] = None,
    good: str = 'pass',
    bad: str = 'fail',
    problem: str = 'warn',
    **kwargs: Any,
) -> bool:
    pass


@overload
def verdict(
    decision: None,
    comment: Optional[str] = None,
    good: str = 'pass',
    bad: str = 'fail',
    problem: str = 'warn',
    **kwargs: Any,
) -> None:
    pass


def verdict(
    decision: Optional[bool],
    comment: Optional[str] = None,
    good: str = 'pass',
    bad: str = 'fail',
    problem: str = 'warn',
    **kwargs: Any,
) -> Optional[bool]:
    """
    Print verdict in green, red or yellow based on the decision

    The supported decision values are:

        True .... good (green)
        False ... bad (red)
        None .... problem (yellow)

    Anything else raises an exception. Additional arguments
    are passed to the `echo` function. Returns back the decision.
    """

    if decision is False:
        text = style(bad, fg='red')
    elif decision is True:
        text = style(good, fg='green')
    elif decision is None:
        text = style(problem, fg='yellow')
    else:
        raise GeneralError("Invalid decision value, must be 'True', 'False' or 'None'.")
    if comment:
        text = text + ' ' + comment
    echo(text, **kwargs)
    return decision


#
# Value formatting a.k.a. pretty-print
#
# (And `pprint` is ugly and `dict_to_yaml` too YAML-ish...)
#
# NOTE: there are comments prefixed by "UX": these try to document
# various tweaks and "exceptions" we need to employ to produce nicely
# readable output for common inputs and corner cases.
#

FormatWrap = Literal[True, False, 'auto']


class ListFormat(enum.Enum):
    """
    How to format lists
    """

    #: Use :py:func:`fmf.utils.listed`.
    LISTED = enum.auto()

    #: Produce comma-separated list.
    SHORT = enum.auto()

    #: One list item per line.
    LONG = enum.auto()


#: How dictionary key/value pairs are indented in their container.
_FORMAT_VALUE_DICT_ENTRY_INDENT = ' ' * INDENT
#: How list items are indented below their container.
_FORMAT_VALUE_LIST_ENTRY_INDENT = '  - '


def assert_window_size(window_size: Optional[int]) -> None:
    """
    Raise an exception if window size is zero or a negative integer.

    Protects possible underflows in formatters employed by :py:func:`format_value`.
    """

    if window_size is None or window_size > 0:
        return

    raise GeneralError(
        f"Allowed width of terminal exhausted, output cannot fit into {OUTPUT_WIDTH} columns."
    )


def _format_bool(
    value: bool,
    window_size: Optional[int],
    key_color: 'tmt.utils.themes.Style',
    list_format: ListFormat,
    wrap: FormatWrap,
) -> Iterator[str]:
    """
    Format a ``bool`` value
    """

    assert_window_size(window_size)

    yield 'true' if value else 'false'


def _format_list(
    value: list[Any],
    window_size: Optional[int],
    key_color: 'tmt.utils.themes.Style',
    list_format: ListFormat,
    wrap: FormatWrap,
) -> Iterator[str]:
    """
    Format a list
    """

    assert_window_size(window_size)

    # UX: if the list is empty, don't bother checking `listed()` or counting
    # spaces.
    if not value:
        yield '[]'
        return

    # UX: if there's just a single item, it's also a trivial case.
    if len(value) == 1:
        yield '\n'.join(
            _format_value(value[0], window_size=window_size, key_color=key_color, wrap=wrap)
        )
        return

    # Render each item in the list. We get a list of possibly multiline strings,
    # one for each item in `value`.
    formatted_items = [
        '\n'.join(_format_value(item, window_size=window_size, key_color=key_color, wrap=wrap))
        for item in value
    ]

    # There are nice ways how to format a string, but those can be tried out
    # only when:
    #
    # * there is no multiline item,
    # * there is no item containing a space,
    # * the window size has been set.
    #
    # If one of these conditions is violated, we fall back to one-item-per-line
    # rendering.
    has_multiline = any('\n' in item for item in formatted_items)
    has_space = any(' ' in item for item in formatted_items)

    if not has_multiline and not has_space and window_size:
        if list_format is ListFormat.LISTED:
            listed_value: str = fmf.utils.listed(formatted_items, quote="'")

            # UX: an empty list, as an item, would be rendered as "[]". Thanks
            # to `quote="'"`, it would be wrapped with quotes, but that looks
            # pretty ugly: foo: 'bar', 'baz' and '[]'. Drop the quotes to make
            # the output a bit nicer.
            listed_value = listed_value.replace("'[]'", '[]')

            if len(listed_value) < window_size:
                yield listed_value
                return

        elif list_format is ListFormat.SHORT:
            short_value = ', '.join(formatted_items)

            if len(short_value) < window_size:
                yield short_value
                return

    yield from formatted_items


def _format_str(
    value: str,
    window_size: Optional[int],
    key_color: 'tmt.utils.themes.Style',
    list_format: ListFormat,
    wrap: FormatWrap,
) -> Iterator[str]:
    """
    Format a string
    """

    assert_window_size(window_size)

    # UX: if the window size is known, rewrap lines to fit in. Otherwise, put
    # each line on its own, well, line.
    # Work with *paragraphs* - lines within a paragraph may get reformatted to
    # fit the line, but we should preserve empty lines between paragraps as
    # much as possible.
    is_multiline = bool('\n' in value)

    if window_size:
        for paragraph in value.rstrip().split('\n\n'):
            stripped_paragraph = paragraph.rstrip()

            if not stripped_paragraph:
                yield ''

            elif wrap is False:
                yield stripped_paragraph

                if is_multiline:
                    yield ''

            else:
                if all(len(line) <= window_size for line in stripped_paragraph.splitlines()):
                    yield from stripped_paragraph.splitlines()

                else:
                    yield from textwrap.wrap(stripped_paragraph, width=window_size)

                if is_multiline:
                    yield ''

    elif not value.rstrip():
        yield ''

    else:
        yield from value.rstrip().split('\n')


def _format_dict(
    value: dict[Any, Any],
    window_size: Optional[int],
    key_color: 'tmt.utils.themes.Style',
    list_format: ListFormat,
    wrap: FormatWrap,
) -> Iterator[str]:
    """
    Format a dictionary
    """

    assert_window_size(window_size)

    # UX: if the dictionary is empty, it's trivial to render.
    if not value:
        yield '{}'
        return

    for k, v in value.items():
        # First, render the key.
        k_formatted = style(k, style=key_color)
        k_size = len(k) + 2

        # Then, render the value. If the window size is known, the value must be
        # propagated, but it must be updated to not include the space consumed by
        # key.
        if window_size:
            v_formatted = _format_value(
                v, window_size=window_size - k_size, key_color=key_color, wrap=wrap
            )
        else:
            v_formatted = _format_value(v, key_color=key_color, wrap=wrap)

        # Now attach key and value in a nice and respectful way.
        if len(v_formatted) == 0:
            # This should never happen, even an empty list should be
            # formatted as a list with one item.
            raise AssertionError

        def _emit_list_entries(lines: list[str]) -> Iterator[str]:
            for i, line in enumerate(lines):
                if i == 0:
                    yield f'{_FORMAT_VALUE_LIST_ENTRY_INDENT}{line}'

                else:
                    yield f'{_FORMAT_VALUE_DICT_ENTRY_INDENT}{line}'

        def _emit_dict_entry(lines: list[str]) -> Iterator[str]:
            yield from (f'{_FORMAT_VALUE_DICT_ENTRY_INDENT}{line}' for line in lines)

        # UX: special handling of containers with just a single item, i.e. the
        # key value fits into a single line of text.
        if len(v_formatted) == 1:
            # UX: special tweaks when `v` is a dictionary
            if isinstance(v, dict):
                # UX: put the `v` on its own line. This way, we get `k` followed
                # by a nested and indented key/value pair.
                #
                # foo:
                #     bar: ...
                if v:
                    yield f'{k_formatted}:'
                    yield from _emit_dict_entry(v_formatted)

                # UX: an empty dictionary shall lead to just a key being emitted
                #
                # foo:<nothing>
                else:
                    yield f'{k_formatted}:'

            # UX: special tweaks when `v` is a list
            elif isinstance(v, list):
                # UX: put both key and value on the same line. We have a list
                # with a single item, trivial case.
                if v:
                    lines = v_formatted[0].splitlines()

                    # UX: If there is just a single line, put key and value on the
                    # same line.
                    if len(lines) <= 1:
                        yield f'{k_formatted}: {lines[0]}'

                    # UX: Otherwise, put lines under the key, and mark the first
                    # line with the list-entry prefix to make it clear the key
                    # holds a list. Remaining lines are indented as well.
                    else:
                        yield f'{k_formatted}:'
                        yield from _emit_list_entries(lines)

                # UX: an empty list, just like an empty dictionary, shall lead to
                # just a key being emitted
                #
                # foo:<nothing>
                else:
                    yield f'{k_formatted}:'

            # UX: every other type
            else:
                lines = v_formatted[0].splitlines()

                # UX: If there is just a single line, put key and value on the
                # same line.
                if not lines:
                    yield f'{k_formatted}:'

                elif len(lines) == 1:
                    yield f'{k_formatted}: {lines[0]}'

                # UX: Otherwise, put lines under the key, and indent them.
                else:
                    yield f'{k_formatted}:'
                    yield from _emit_dict_entry(lines)

        # UX: multi-item dictionaries are much less complicated, there is no
        # chance to simplify the output. Each key would land on its own line,
        # with content well-aligned.
        else:
            yield f'{k_formatted}:'

            # UX: when rendering a list, indent the lines properly with the
            # first one
            if isinstance(v, list):
                for item in v_formatted:
                    yield from _emit_list_entries(item.splitlines())

            else:
                yield from _emit_dict_entry(v_formatted)


#: A type describing a per-type formatting helper.
ValueFormatter = Callable[
    [Any, Optional[int], 'tmt.utils.themes.Style', ListFormat, FormatWrap], Iterator[str]
]


#: Available formatters, as ``type``/``formatter`` pairs. If a value is instance
#: of ``type``, the ``formatter`` is called to render it.
_VALUE_FORMATTERS: list[tuple[Any, ValueFormatter]] = [
    (bool, _format_bool),
    (str, _format_str),
    (list, _format_list),
    (dict, _format_dict),
]


def _format_value(
    value: Any,
    window_size: Optional[int] = None,
    key_color: 'tmt.utils.themes.Style' = None,
    list_format: ListFormat = ListFormat.LISTED,
    wrap: FormatWrap = 'auto',
) -> list[str]:
    """
    Render a nicely-formatted string representation of a value.

    A main workhorse for :py:func:`format_value` and value formatters
    defined for various types. This function is responsible for
    picking the right one.

    :param value: an object to format.
    :param window_size: if set, rendering will try to produce
        lines whose length would not exceed ``window_size``. A
        window not wide enough may result into not using
        :py:func:`fmf.utils.listed`, or wrapping lines in a text
        paragraph.
    :param key_color: if set, dictionary keys would be colorized by
        this color.
    :param list_format: preferred list formatting. It may be ignored
        if ``window_size`` is set and not wide enough to hold the
        desired formatting; :py:member:`ListFormat.LONG` would be
        the fallback choice.
    :returns: a list of lines representing the formatted string
        representation of ``value``.
    """

    assert_window_size(window_size)

    for type_, formatter in _VALUE_FORMATTERS:
        if isinstance(value, type_):
            return list(formatter(value, window_size, key_color, list_format, wrap))

    return [str(value)]


def format_value(
    value: Any,
    window_size: Optional[int] = None,
    key_color: 'tmt.utils.themes.Style' = None,
    list_format: ListFormat = ListFormat.LISTED,
    wrap: FormatWrap = 'auto',
) -> str:
    """
    Render a nicely-formatted string representation of a value.

    :param value: an object to format.
    :param window_size: if set, rendering will try to produce
        lines whose length would not exceed ``window_size``. A
        window not wide enough may result into not using
        :py:func:`fmf.utils.listed`, or wrapping lines in a text
        paragraph.
    :param key_color: if set, dictionary keys would be colorized by
        this color.
    :param list_format: preferred list formatting. It may be ignored
        if ``window_size`` is set and not wide enough to hold the
        desired formatting; :py:attr:`ListFormat.LONG` would be
        the fallback choice.
    :returns: a formatted string representation of ``value``.
    """

    assert_window_size(window_size)

    formatted_value = _format_value(
        value, window_size=window_size, key_color=key_color, list_format=list_format, wrap=wrap
    )

    # UX: post-process lists: this top-level is the "container" of the list,
    # and therefore needs to apply indentation and prefixes.
    if isinstance(value, list):
        # UX: an empty list should be represented as an empty string.
        # We get a nice `foo <nothing>` from `format()` under
        # various `show` commands.
        if not value:
            return ''

        # UX: if there is just a single formatted item, prefixing it with `-`
        # would not help readability.
        if len(value) == 1:
            return formatted_value[0]

        # UX: if there are multiple items, we do not add prefixes as long as
        # there are no multi-line items - once there is just a single one item
        # rendered across multiple lines, we need to add `-` prefix & indentation
        # to signal where items start and end visually.
        if len(value) > 1 and any('\n' in formatted_item for formatted_item in formatted_value):
            prefixed: list[str] = []

            for item in formatted_value:
                for i, line in enumerate(item.splitlines()):
                    if i == 0:
                        prefixed.append(f'- {line}')

                    else:
                        prefixed.append(f'  {line}')

            return '\n'.join(prefixed)

    return '\n'.join(formatted_value)


def format(
    key: str,
    value: Union[None, float, bool, str, list[Any], dict[Any, Any]] = None,
    indent: int = 24,
    window_size: int = OUTPUT_WIDTH,
    wrap: FormatWrap = 'auto',
    key_color: 'tmt.utils.themes.Style' = 'green',
    value_color: 'tmt.utils.themes.Style' = 'black',
    list_format: ListFormat = ListFormat.LISTED,
) -> str:
    """
    Nicely format and indent a key-value pair

    :param key: a key introducing the value.
    :param value: an object to format.
    :param indent: the key would be right-justified to this column.
    :param window_size: rendering will try to fit produce lines
        whose length would exceed ``window_size``. A window not wide
        enough may result into not using :py:func:`fmf.utils.listed`
        for lists, or wrapping lines in a text paragraph.
    :param wrap: if set to ``True``, always reformat text and wrap
        long lines; if set to ``False``, preserve text formatting
        and make no changes; the default, ``auto``, tries to rewrap
        lines as needed to obey ``window_size``.
    :param key_color: if set, dictionary keys would be colorized by
        this color.
    :param list_format: preferred list formatting. It may be ignored
        if ``window_size`` is set and not wide enough to hold the
        desired formatting; :py:attr:`ListFormat.LONG` would be
        the fallback choice.
    :returns: a formatted string representation of ``value``.
    """

    assert_window_size(window_size)

    indent_string = (indent + 1) * ' '

    # Format the key first
    output = style(f"{str(key).rjust(indent, ' ')} ", style=key_color)

    # Then the value
    formatted_value = format_value(
        value,
        window_size=window_size - indent,
        key_color=key_color,
        list_format=list_format,
        wrap=wrap,
    )

    # A special care must be taken when joining key and some types of values
    if isinstance(value, list):
        value_as_lines = formatted_value.splitlines()

        if len(value_as_lines) == 1:
            return output + formatted_value

        return output + ('\n' + indent_string).join(value_as_lines)

    if isinstance(value, dict):
        return output + ('\n' + indent_string).join(formatted_value.splitlines())

    # TODO: the whole text wrap should be handled by the `_format_value()`!
    if isinstance(value, str):
        value_as_lines = formatted_value.splitlines()

        # Undo the line rewrapping. This would be resolved once `_format_value`
        # takes over.
        if wrap is False:
            return output + ''.join(value_as_lines)

        # In 'auto' mode enable wrapping when long lines present
        if wrap == 'auto':
            wrap = any(len(line) + indent - 7 > window_size for line in value_as_lines)

        if wrap:
            return (
                output
                + wrap_text(
                    value,
                    width=window_size,
                    preserve_paragraphs=True,
                    initial_indent=indent_string,
                    subsequent_indent=indent_string,
                ).lstrip()
            )

        return output + ('\n' + indent_string).join(value_as_lines)

    return output + formatted_value


P = ParamSpec('P')


# [happz] I was thinking how to slot this under the umbrela of `format()`
# and `format_value()`, but it's 3 values rather than one, and extending
# their API did not look sane enough.
# On the other hand, we don't log function calls too often, it's a rare
# occasion, so it's probably fine if it stands alone.
def format_call(fn: Callable[P, Any], *args: P.args, **kwargs: P.kwargs) -> str:
    """
    Format a function call for logging.
    """

    arguments: list[str] = [repr(arg) for arg in args] + [
        f'{name}={value}' for name, value in kwargs.items()
    ]

    return f'{fn.__name__}({", ".join(arguments)})'


def create_directory(
    *,
    path: Path,
    name: str,
    dry: bool = False,
    quiet: bool = False,
    logger: tmt.log.Logger,
) -> None:
    """
    Create a new directory.

    Before creating the directory, function checks whether it exists
    already - the existing directory is **not** removed and re-created.

    The outcome of the operation will be logged in a debug log, but
    may also be sent to console with ``quiet=False``.

    :param path: a path to be created.
    :param name: a "label" of the path, used for logging.
    :param dry: if set, directory would not be created. Still, the
        existence check will happen.
    :param quiet: if set, an outcome of the operation would not be logged
        to console.
    :param logger: logger to use for logging.
    :raises FileError: when function tried to create the directory,
        but failed.
    """

    # Streamline the logging a bit: wrap the creating with a function returning
    # a message & optional exception. Later we will send the message to debug
    # log, and maybe also to console.
    def _create_directory() -> tuple[str, Optional[Exception]]:
        if path.is_dir():
            return (f"{name.capitalize()} '{path}' already exists.", None)

        if dry:
            return (f"{name.capitalize()} '{path}' would be created.", None)

        try:
            path.mkdir(exist_ok=True, parents=True)

        except OSError as error:
            return (f"Failed to create {name} '{path}'.", error)

        return (f"{name.capitalize()} '{path}' created.", None)

    message, exc = _create_directory()

    if exc:
        raise FileError(message) from exc

    logger.debug(message)

    if quiet:
        return

    echo(message)


def create_file(
    *,
    path: Path,
    content: str,
    name: str,
    dry: bool = False,
    force: bool = False,
    mode: int = 0o664,
    quiet: bool = False,
    logger: tmt.log.Logger,
) -> None:
    """
    Create a new file.

    Before creating the file, function checks whether it exists
    already - the existing file is **not** removed and re-created,
    unless ``force`` is set.

    The outcome of the operation will be logged in a debug log, but
    may also be sent to console with ``quiet=False``.

    :param path: a path to be created.
    :param content: content to save into the file
    :param name: a "label" of the path, used for logging.
    :param dry: if set, the file would not be created or overwritten. Still,
        the existence check will happen.
    :param force: if set, the file would be overwritten if it already exists.
    :param mode: permissions to set for the file.
    :param quiet: if set, an outcome of the operation would not be logged
        to console.
    :param logger: logger to use for logging.
    :raises FileError: when function tried to create the file,
        but failed.
    """

    # Streamline the logging a bit: wrap the creating with a function returning
    # a message & optional exception. Later we will send the message to debug
    # log, and maybe also to console.
    def _create_file() -> tuple[str, Optional[Exception]]:
        # When overwriting an existing path, we need to provide different message.
        # Let's save the action taken for logging.
        action: str = 'created'

        if path.exists():
            if not force:
                message = f"{name.capitalize()} '{path}' already exists."

                # Return a custom exception - it was not raised by any FS-related code,
                # but we need to signal the operation failed to our caller.
                return message, FileExistsError(message)

            action = 'overwritten'

        if dry:
            return f"{name.capitalize()} '{path}' would be {action}.", None

        try:
            path.write_text(content)
            path.chmod(mode)

        except OSError as error:
            return f"Failed to create {name} '{path}'.", error

        return f"{name.capitalize()} '{path}' {action}.", None

    message, exc = _create_file()

    if exc:
        raise FileError(message) from exc

    logger.debug(message)

    if quiet:
        return

    echo(message)


@functools.cache
def fmf_id(
    *,
    name: str,
    fmf_root: Path,
    logger: tmt.log.Logger,
) -> 'tmt.base.FmfId':
    """
    Return full fmf identifier of the node
    """

    from tmt.base import FmfId
    from tmt.utils.git import GitInfo

    fmf_id = FmfId(fmf_root=fmf_root, name=name)
    git_info = GitInfo.from_fmf_root(fmf_root=fmf_root, logger=logger)

    # If we couldn't resolve the git metadata, keep the git metadata empty
    if not git_info:
        return fmf_id

    # Populate the git metadata from GitInfo
    # TODO: Save GitInfo inside FmfId as-is
    fmf_id.git_root = git_info.git_root
    # Construct path (if different from git root)
    if fmf_id.git_root.resolve() != fmf_root.resolve():
        fmf_id.path = Path('/') / fmf_root.relative_to(fmf_id.git_root)
    fmf_id.ref = git_info.ref
    fmf_id.url = git_info.url
    fmf_id.default_branch = git_info.default_branch

    return fmf_id


class TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """
    Spice up request's session with custom timeout
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.timeout = kwargs.pop('timeout', None)

        super().__init__(*args, **kwargs)

    # ignore[override]: signature does not match superclass on purpose.
    # send() does declare plenty of parameters we do not care about.
    def send(  # type: ignore[override]
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        """
        Send request.

        All arguments are passed to superclass after enforcing the timeout.

        :param request: the request to send.
        """

        kwargs.setdefault('timeout', self.timeout)

        return super().send(request, **kwargs)


class RetryStrategy(urllib3.util.retry.Retry):
    def __init__(self, *args: Any, logger: Optional[tmt.log.Logger] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logger = logger

    def new(self, **kw: Any) -> 'Self':
        new_retry = super().new(**kw)
        new_retry.logger = self.logger
        return new_retry

    def _log_rate_limit_info(self, headers: urllib3._collections.HTTPHeaderDict) -> None:
        """
        Log current GitHub rate limit information
        """

        if not self.logger:
            return

        self.logger.debug("Limit", headers.get('X-RateLimit-Limit', 'unknown'))
        self.logger.debug("Remaining", headers.get('X-RateLimit-Remaining', 'unknown'))
        self.logger.debug("Reset", headers.get('X-RateLimit-Reset', 'unknown'))
        self.logger.debug("Resource", headers.get('X-RateLimit-Resource', 'unknown'))

    def _log_response_info(self, response: HTTPResponse) -> None:
        """
        Log detailed response information for debugging
        """

        if not self.logger:
            return

        self.logger.debug("Response status", response.status)
        self.logger.debug("Response headers", dict(response.headers))
        self.logger.debug("Response text", response.data.decode('utf-8'))

    def increment(self, *args: Any, **kwargs: Any) -> urllib3.util.retry.Retry:
        error = cast(Optional[Exception], kwargs.get('error'))

        # Detect a subset of exception we do not want to follow with a retry.
        # SIM102: Use a single `if` statement instead of nested `if` statements. Keeping for
        # readability.
        if error is not None:  # noqa: SIM102
            # Failed certificate verification - this issue will probably not get any better
            # should we try again.
            if isinstance(
                error, urllib3.exceptions.SSLError
            ) and 'certificate verify failed' in str(error):
                # [mpr] I'm not sure how stable this *iternal* API is, but pool seems to be the
                # only place aware of the remote hostname. Try our best to get the hostname for
                # a better error message, but don't crash because of a missing attribute or
                # something as dumb.

                connection_pool = kwargs.get('_pool')

                if connection_pool is not None and hasattr(connection_pool, 'host'):
                    message = f"Certificate verify failed for '{connection_pool.host}'."
                else:
                    message = 'Certificate verify failed.'

                raise GeneralError(message) from error

        # Handle GitHub-specific responses
        # https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28#exceeding-the-rate-limit
        response = cast(Optional[urllib3.response.HTTPResponse], kwargs.get('response'))
        if response is None or 'X-GitHub-Request-Id' not in response.headers:
            return super().increment(*args, **kwargs)

        headers = response.headers

        # Log rate limit information if available
        if any(key.startswith('X-RateLimit-') for key in list(headers.keys())):
            # mypy complains without converting headers keys to list
            self._log_rate_limit_info(headers)

        if response.status not in (403, 429):
            return super().increment(*args, **kwargs)
        # Log response info for problematic responses
        self._log_response_info(response)

        # Check if this is actually a rate limit issue
        if 'X-RateLimit-Resource' in headers:
            # Primary rate limit exceeded
            if 'X-RateLimit-Remaining' in headers and int(headers['X-RateLimit-Remaining']) == 0:
                reset_time = int(headers['X-RateLimit-Reset'])
                wait_time = reset_time - int(time.time())
                if wait_time > 0:
                    # Add 1 second buffer
                    wait_time += 1

                    if self.logger:
                        self.logger.info(
                            f"Primary rate limit exceeded. Waiting {wait_time + 1} seconds."
                        )
                    time.sleep(wait_time)

            if 'Retry-After' in headers:
                retry_after = int(headers['Retry-After'])
                retry_after += 1
                if self.logger:
                    self.logger.info(f"Secondary rate limit hit. Waiting {retry_after} seconds.")
                time.sleep(retry_after)

            # Exponential backoff for unclear rate limit cases
            if self.total is not None:
                wait_time = min(2**self.total, 60)
                if self.logger:
                    self.logger.info(
                        "Rate limit detected but no wait time specified. "
                        f"Using exponential backoff: {wait_time} seconds"
                    )
                time.sleep(wait_time)

        # Handle other 403 cases
        elif 'X-GitHub-Request-Id' in headers:
            try:
                error_msg = json.loads(response.data.decode('utf-8')).get('message', '').lower()
                if self.logger:
                    self.logger.warning(f"GitHub API error: {error_msg}")
            except (ValueError, AttributeError) as e:
                if self.logger:
                    self.logger.warning(f"Failed to parse error message from response: {e}")

        return super().increment(*args, **kwargs)

    def _is_rate_limit_error(self, response: requests.Response) -> bool:
        if not response or 'X-GitHub-Request-Id' not in response.headers:
            return False

        try:
            error_msg = response.json().get('message', '').lower()
            is_rate_limit = (
                'rate limit exceeded' in error_msg or 'secondary rate limit' in error_msg
            )
            if self.logger:
                self.logger.debug(
                    f"Rate limit error detection: {is_rate_limit} (message: {error_msg})"
                )
            return is_rate_limit
        except (ValueError, AttributeError) as e:
            if self.logger:
                self.logger.warning(f"Failed to check rate limit error: {e}")
            return False


# ignore[type-arg]: base class is a generic class, but we cannot list
# its parameter type, because in Python 3.6 the class "is not subscriptable".
class retry_session(contextlib.AbstractContextManager):  # type: ignore[type-arg]  # noqa: N801
    """
    Context manager for :py:class:`requests.Session` with retries and timeout
    """

    @staticmethod
    def create(
        retries: int = DEFAULT_RETRY_SESSION_RETRIES,
        backoff_factor: float = DEFAULT_RETRY_SESSION_BACKOFF_FACTOR,
        allowed_methods: Optional[tuple[str, ...]] = None,
        status_forcelist: Optional[tuple[int, ...]] = None,
        timeout: Optional[int] = None,
        logger: Optional[tmt.log.Logger] = None,
    ) -> requests.Session:
        # `method_whitelist`` has been renamed to `allowed_methods` since
        # urllib3 1.26, and it will be removed in urllib3 2.0.
        # `allowed_methods` is therefore the future-proof name, but for the
        # sake of backward compatibility, internally might need to use the
        # deprecated parameter.
        if urllib3.__version__.startswith('1.'):
            retry_strategy = RetryStrategy(
                total=retries,
                status_forcelist=status_forcelist,
                method_whitelist=allowed_methods,
                backoff_factor=backoff_factor,
                logger=logger,
            )

        else:
            retry_strategy = RetryStrategy(
                total=retries,
                status_forcelist=status_forcelist,
                allowed_methods=allowed_methods,
                backoff_factor=backoff_factor,
                logger=logger,
            )

        if timeout is not None:
            http_adapter: requests.adapters.HTTPAdapter = TimeoutHTTPAdapter(
                timeout=timeout, max_retries=retry_strategy
            )
        else:
            http_adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy)

        session = requests.Session()
        session.mount('http://', http_adapter)
        session.mount('https://', http_adapter)

        return session

    def __init__(
        self,
        retries: int = DEFAULT_RETRY_SESSION_RETRIES,
        backoff_factor: float = DEFAULT_RETRY_SESSION_BACKOFF_FACTOR,
        allowed_methods: Optional[tuple[str, ...]] = None,
        status_forcelist: Optional[tuple[int, ...]] = None,
        timeout: Optional[int] = None,
        logger: Optional[tmt.log.Logger] = None,
    ) -> None:
        self.retries = retries
        self.backoff_factor = backoff_factor
        self.allowed_methods = allowed_methods
        self.status_forcelist = status_forcelist
        self.timeout = timeout

    def __enter__(self) -> requests.Session:
        return self.create(
            retries=self.retries,
            backoff_factor=self.backoff_factor,
            allowed_methods=self.allowed_methods,
            status_forcelist=self.status_forcelist,
            timeout=self.timeout,
        )

    def __exit__(self, *args: object) -> None:
        pass


def remove_color(text: str) -> str:
    """
    Remove ansi color sequences from the string
    """

    return re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)


def generate_runs(path: Path, id_: tuple[str, ...]) -> Iterator[Path]:
    """
    Generate absolute paths to runs from path
    """

    # Prepare absolute workdir path if --id was used
    run_path = None
    for id_name in id_:
        if id_name:
            run_path = Path(id_name)
            if '/' not in id_name:
                run_path = path / run_path
            if not run_path.exists():
                raise tmt.utils.GeneralError(f"Directory '{run_path}' does not exist.")
            if run_path.is_absolute() and run_path.exists():
                yield run_path
        else:
            raise tmt.utils.GeneralError("Value of '--id' option cannot be an empty string.")
    if run_path:
        return
    if not path.exists():
        return
    for childpath in path.iterdir():
        abs_child_path = childpath.absolute()
        # If id_ is None, the abs_path is considered valid (no filtering
        # is being applied). If it is defined, it has been transformed
        # to absolute path and must be equal to abs_path for the run
        # in abs_path to be generated.
        invalid_id = id_ and str(abs_child_path) not in id_
        invalid_run = not abs_child_path.joinpath('run.yaml').exists()
        if not abs_child_path.is_dir() or invalid_id or invalid_run:
            continue
        yield abs_child_path


def load_run(run: 'tmt.base.Run') -> tuple[bool, Optional[Exception]]:
    """
    Load a run and its steps from the workdir
    """

    try:
        run.load_from_workdir()

        for plan in run.plans:
            for step in plan.steps(enabled_only=False):
                step.load()

    except GeneralError as error:
        return False, error

    return True, None


# ignore[type-arg]: base class is a generic class, but we cannot list its parameter type, because
# in Python 3.6 the class "is not subscriptable".
class UpdatableMessage(contextlib.AbstractContextManager):  # type: ignore[type-arg]
    """
    Updatable message suitable for progress-bar-like reporting
    """

    def __init__(
        self,
        key: str,
        enabled: bool = True,
        indent_level: int = 0,
        key_color: 'tmt.utils.themes.Style' = None,
        default_value_color: 'tmt.utils.themes.Style' = None,
        clear_on_exit: bool = False,
    ) -> None:
        """
        Updatable message suitable for progress-bar-like reporting.

        .. code-block:: python3

           with UpdatableMessage('foo') as message:
               while ...:
                   ...

                   # check state of remote request, and update message
                   state = remote_api.check()
                   message.update(state)

        :param key: a string to use as the left-hand part of logged message.
        :param enabled: if unset, no output would be performed.
        :param indent_level: desired indentation level.
        :param key_color: optional color to apply to ``key``.
        :param default_color: optional color to apply to value when
            :py:meth:`update` is called with ``color`` left out.
        :param clear_on_exit: if set, the message area would be cleared when
            leaving the progress bar when used as a context manager.
        """

        self.key = key
        self.enabled = enabled
        self.indent_level = indent_level
        self.key_color = key_color
        self.default_value_color = default_value_color
        self.clear_on_exit = clear_on_exit

        # No progress if terminal not attached
        if not sys.stdout.isatty():
            self.enabled = False

        self._previous_line: Optional[str] = None

    def __enter__(self) -> 'Self':
        return self

    def __exit__(self, *args: object) -> None:
        if self.clear_on_exit:
            self.clear()

        sys.stdout.write('\n')
        sys.stdout.flush()

    def clear(self) -> None:
        """
        Clear the message area
        """

        self._update_message_area('')

    def _update_message_area(self, value: str, color: 'tmt.utils.themes.Style' = None) -> None:
        """
        Update message area with given value.

        .. note::

            This method is the workhorse for :py:meth:`update` which, in our
            basic implementation, is a thin wrapper for
            :py:meth:`_update_message_area`.

            Derived classes may choose to override the default implementation of
            :py:meth:`update`, to simplify the message construction, and call
            :py:meth:`_update_message_area` to emit the message.
        """

        if not self.enabled:
            return

        if self._previous_line is not None:
            message = value.ljust(len(self._previous_line))

        else:
            message = value

        self._previous_line = value

        message = tmt.log.indent(
            self.key,
            value=style(message, style=color or self.default_value_color),
            color=self.key_color,
            level=self.indent_level,
        )

        sys.stdout.write(f"\r{message}")
        sys.stdout.flush()

    def update(self, value: str, color: 'tmt.utils.themes.Style' = None) -> None:
        """
        Update progress message.

        :param value: new message to update message area with.
        :param color: optional message color.
        """

        self._update_message_area(value, color=color)


def find_fmf_root(path: Path, ignore_paths: Optional[list[Path]] = None) -> list[Path]:
    """
    Search through path and return all fmf roots that exist there

    Returned list is ordered by path length, shortest one first.

    Raise `MetadataError` if no fmf root is found.
    """

    fmf_roots = []
    for _root, _, files in os.walk(path):
        root = Path(_root)
        if root.name != '.fmf':
            continue
        if ignore_paths and root.parent in ignore_paths:
            continue
        if 'version' in files:
            fmf_roots.append(root.parent)
    if not fmf_roots:
        raise MetadataError(f"No fmf root present inside '{path}'.")
    fmf_roots.sort(key=lambda path: len(str(path)))
    return fmf_roots


#
# JSON schema-based validation helpers
#
# Aims at FMF data consumed by tmt, but can be used for any structure.
#

# `Schema` represents a loaded JSON schema structure. It may be fairly complex,
# but it's not needed to provide the exhaustive and fully detailed type since
# tmt code is not actually "reading" it. Loaded schema is passed down to
# jsonschema library, and while `Any` would be perfectly valid, let's use an
# alias to make schema easier to track in our code.
Schema = dict[str, Any]
SchemaStore = dict[str, Schema]


def _patch_plan_schema(schema: Schema, store: SchemaStore) -> None:
    """
    Resolve references to per-plugin schema known to steps. All schemas have
    been loaded into store, all that's left is to update each step in plan
    schema with the list of schemas allowed for that particular step.

    For each step, we create the following schema (see also plan.yaml for the
    rest of plan schema):

    .. code-block:: yaml

       <step name>:
         oneOf:
           - $ref: "/schemas/<step name>/plugin1"
           - $ref: "/schemas/<step name>/plugin2"
           ...
           - $ref: "/schemas/<step name>/pluginN"
           - type: array
             items:
               anyOf:
                 - $ref: "/schemas/<step name>/plugin1"
                 - $ref: "/schemas/<step name>/plugin2"
                 ...
                 - $ref: "/schemas/<step name>/pluginN"
    """

    for step in ('discover', 'execute', 'finish', 'prepare', 'provision', 'report'):
        step_schema_prefix = f'/schemas/{step}/'

        step_plugin_schema_ids = [
            schema_id
            for schema_id in store
            if schema_id.startswith(step_schema_prefix)
            and schema_id not in PLAN_SCHEMA_IGNORED_IDS
        ]

        refs: list[Schema] = [{'$ref': schema_id} for schema_id in step_plugin_schema_ids]

        schema['properties'][step] = {
            'oneOf': [*refs, {'type': 'array', 'items': {'anyOf': refs}}]
        }


def _load_schema(schema_filepath: Path) -> Schema:
    """
    Load a JSON schema from a given filepath.

    A helper returning the raw loaded schema.
    """

    if not schema_filepath.is_absolute():
        schema_filepath = resource_files('schemas') / schema_filepath

    try:
        return cast(Schema, yaml_to_dict(schema_filepath.read_text(encoding='utf-8')))

    except Exception as exc:
        raise FileError(f"Failed to load schema file {schema_filepath}\n{exc}")


@functools.cache
def load_schema(schema_filepath: Path) -> Schema:
    """
    Load a JSON schema from a given filepath.

    Recommended for general use, the method may apply some post-loading touches
    to the given schema, and unless caller is interested in the raw content of
    the file, this functions should be used instead of the real workhorse of
    schema loading, :py:func:`_load_schema`.
    """

    schema = _load_schema(schema_filepath)

    if schema.get('$id') == '/schemas/plan':
        _patch_plan_schema(schema, load_schema_store())

    return schema


@functools.cache
def load_schema_store() -> SchemaStore:
    """
    Load all available JSON schemas, and put them into a "store".

    Schema store is a simple mapping between schema IDs and schemas.
    """

    store: SchemaStore = {}
    schema_dirpath = resource_files('schemas')

    try:
        for filepath in schema_dirpath.glob('**/*ml'):
            # Ignore all files but YAML files.
            if filepath.suffix.lower() not in ('.yaml', '.yml'):
                continue

            schema = _load_schema(filepath)

            store[schema['$id']] = schema

    except Exception as exc:
        raise FileError(f"Failed to discover schema files\n{exc}")

    if '/schemas/plan' not in store:
        raise FileError('Failed to discover schema for plans')

    _patch_plan_schema(store['/schemas/plan'], store)

    return store


def _prenormalize_fmf_node(node: fmf.Tree, schema_name: str, logger: tmt.log.Logger) -> fmf.Tree:
    """
    Apply the minimal possible normalization steps to nodes before validating them with schemas.

    tmt allows some fields to have default values, and at least ``how`` field is necessary for
    schema-based validation to work reliably. Based on ``how`` field, plan schema identifies
    the correct *plugin* schema for step validation. Without ``how``, it's hard to pick the
    correct schema.

    This function tries to do minimal number of changes to a given fmf node to honor the promise
    of ``how`` being optional, with known defaults for each step. It might be possible to resolve
    this purely with schemas, but since we don't know how (yet?), a Python implementation has been
    chosen to unblock schema-based validation while keeping things easier for users. This may
    change in the future, dropping the need for this pre-validation step.

    .. note::

       This function is not part of the normalization process that happens after validation. The
       purpose of this function is to make the world nice and shiny for tmt users while avoiding
       the possibility of schema becoming way too complicated, especially when we would need
       non-trivial amount of time for experiments.

       The real normalization process takes place after validation, and is responsible for
       converting raw fmf data to data types and structures more suited for tmt internal
       implementation.
    """

    # As of now, only `how` field in plan steps seems to be required for schema-based validation
    # to work correctly, therefore ignore any other node.
    if schema_name != 'plan.yaml':
        return node

    # Perform the very crude and careful semi-validation. We need to set the `how` key to a default
    # value - but it's not our job to validate the general structure of node data. Walk the "happy"
    # path, touch the node only when it matches the specification of being a mapping of steps and
    # these being either mappings or lists of mappings. Whenever we notice some value does not
    # match this basic structure, ignore the step completely - its issues will be caught by schema
    # later, don't waste time on steps that do not follow specification.

    # Fmf data describing a plan shall be a mapping (with keys like `discover` or `adjust`).
    if not isinstance(node.data, dict):
        return node

    # Do NOT modify the given node! Changing it might taint or hide important
    # keys the later processing could need in their original state. Namely, we
    # need to initialize `how` to reach at least some schema, but CLI processing
    # needs to realize `how` was not given, and therefore it's possible to be
    # modified with `--update-missing`...
    node = node.copy()

    # Avoid possible circular imports
    import tmt.steps

    def _process_step(step_name: str, step: dict[Any, Any]) -> None:
        """
        Process a single step configuration
        """

        # If `how` is set, don't touch it, and there's nothing to do.
        if 'how' in step:
            return

        # Magic!
        # No, seriously: step is implemented in `tmt.steps.$step_name` package,
        # by a class `tmt.steps.$step_name.$step_name_with_capitalized_first_letter`.
        # Instead of having a set of if-elif tests, we can reach the default `how`
        # dynamically.

        from tmt.plugins import import_member

        step_module_name = f'tmt.steps.{step_name}'
        step_class_name = step_name.capitalize()

        step_class = import_member(
            module=step_module_name,
            member=step_class_name,
            logger=logger,
        )[1]

        if not issubclass(step_class, tmt.steps.Step):
            raise GeneralError(
                'Possible step {step_name} implementation '
                f'{step_module_name}.{step_class_name} is not a subclass '
                'of tmt.steps.Step class.'
            )

        step['how'] = step_class.DEFAULT_HOW

    def _process_step_collection(step_name: str, step_collection: Any) -> None:
        """
        Process a collection of step configurations
        """

        # Ignore anything that is not a step.
        if step_name not in tmt.steps.STEPS:
            return

        # A single step configuration, represented as a mapping.
        if isinstance(step_collection, dict):
            _process_step(step_name, step_collection)

            return

        # Multiple step configurations, as mappings in a list
        if isinstance(step_collection, list):
            for step_config in step_collection:
                # Unexpected, maybe instead of a mapping describing a step someone put
                # in an integer... Ignore, schema will report it.
                if not isinstance(step_config, dict):
                    continue

                _process_step(step_name, step_config)

    for step_name, step_config in node.data.items():
        _process_step_collection(step_name, step_config)

    return node


def preformat_jsonschema_validation_errors(
    raw_errors: list[jsonschema.ValidationError],
    prefix: Optional[str] = None,
) -> list[tuple[jsonschema.ValidationError, str]]:
    """
    A helper to preformat JSON schema validation errors.

    Raw errors can be converted to strings with a simple ``str()`` call,
    but resulting string is very JSON-ish. This helper provides
    simplified string representation consisting of error message and
    element path.

    :param raw_error: raw validation errors as provided by
        :py:mod:`jsonschema`.
    :param prefix: if specified, it is added at the beginning of each
        stringified error.
    :returns: a list of two-item tuples, the first item being the
        original validation error, the second item being its simplified
        string rendering.
    """

    prefix = f'{prefix}:' if prefix else ''
    errors: list[tuple[jsonschema.ValidationError, str]] = []

    for error in raw_errors:
        path = f'{prefix}{".".join(str(p) for p in error.path)}'

        errors.append((error, f'{path} - {error.message}'))

    return errors


def validate_fmf_node(
    node: fmf.Tree,
    schema_name: str,
    logger: tmt.log.Logger,
) -> list[tuple[jsonschema.ValidationError, str]]:
    """
    Validate a given fmf node
    """

    node = _prenormalize_fmf_node(node, schema_name, logger)

    result = node.validate(load_schema(Path(schema_name)), schema_store=load_schema_store())

    if result.result is True:
        return []

    return preformat_jsonschema_validation_errors(result.errors, prefix=node.name)


class ValidateFmfMixin(_CommonBase):
    """
    Mixin adding validation of an fmf node.

    Loads a schema whose name is derived from class name, and uses fmf's validate()
    method to perform the validation.
    """

    def _validate_fmf_node(
        self,
        node: fmf.Tree,
        raise_on_validation_error: bool,
        logger: tmt.log.Logger,
    ) -> None:
        """
        Validate a given fmf node
        """

        errors = validate_fmf_node(node, f'{self.__class__.__name__.lower()}.yaml', logger)

        if errors:
            if raise_on_validation_error:
                raise SpecificationError(
                    f'fmf node {node.name} failed validation', validation_errors=errors
                )

            for _, error_message in errors:
                logger.warning(error_message, shift=1)

    def __init__(
        self,
        *,
        node: fmf.Tree,
        skip_validation: bool = False,
        raise_on_validation_error: bool = False,
        logger: tmt.log.Logger,
        **kwargs: Any,
    ) -> None:
        # Validate *before* letting next class in line touch the data.
        if not skip_validation:
            self._validate_fmf_node(node, raise_on_validation_error, logger)

        super().__init__(node=node, logger=logger, **kwargs)


def dataclass_normalize_field(
    container: Any,
    key_address: str,
    keyname: str,
    raw_value: Any,
    value_source: 'FieldValueSource',
    logger: tmt.log.Logger,
) -> Any:
    """
    Normalize and assign a value to container field.

    If there is a normalization callback defined for the field via ``normalize``
    parameter of :py:func:`field`, the callback is called to coerce ``raw_value``,
    and the return value is assigned to container field instead of ``value``.
    """

    from tmt.container import container_field

    # Find out whether there's a normalization callback, and use it. Otherwise,
    # the raw value is simply used.
    value = raw_value

    if dataclasses.is_dataclass(container):
        _, _, _, _, metadata = container_field(
            type(container) if not isinstance(container, type) else container, keyname
        )

        if metadata.normalize_callback:
            value = metadata.normalize_callback(key_address, raw_value, logger)

    # TODO: we already access parameter source when importing CLI invocations in `Step.wake()`,
    # we should do the same here as well. It will require adding (optional) Click context
    # as one of the inputs, but that's acceptable. Then we can get rid of this less-than-perfect
    # test.
    #
    # Keep for debugging purposes, as long as normalization settles down.
    if not value:
        logger.debug(
            f'field "{key_address}" normalized to false-ish value',
            f'{container.__class__.__name__}.{keyname}',
            level=4,
            topic=tmt.log.Topic.KEY_NORMALIZATION,
        )

        with_getattr = getattr(container, keyname, None)
        with_dict = container.__dict__.get(keyname, None)

        logger.debug('value', str(value), level=4, shift=1, topic=tmt.log.Topic.KEY_NORMALIZATION)
        logger.debug(
            'current value (getattr)',
            str(with_getattr),
            level=4,
            shift=1,
            topic=tmt.log.Topic.KEY_NORMALIZATION,
        )
        logger.debug(
            'current value (__dict__)',
            str(with_dict),
            level=4,
            shift=1,
            topic=tmt.log.Topic.KEY_NORMALIZATION,
        )

        if value != with_getattr or with_getattr != with_dict:
            logger.debug(
                'known values do not match',
                level=4,
                shift=2,
                topic=tmt.log.Topic.KEY_NORMALIZATION,
            )

    # Set attribute by adding it to __dict__ directly. Messing with setattr()
    # might cause reuse of mutable values by other instances.
    container.__dict__[keyname] = value

    if hasattr(container, '_field_value_sources'):
        container._field_value_sources[keyname] = value_source

    return value


def normalize_int(
    key_address: str,
    value: Any,
    logger: tmt.log.Logger,
) -> int:
    """
    Normalize an integer.

    For a field that takes an integer input. The field might be also
    left out, but it does have a default value.
    """

    if isinstance(value, int):
        return value

    try:
        return int(value)

    except ValueError as exc:
        raise NormalizationError(key_address, value, 'an integer') from exc


def normalize_optional_int(
    key_address: str,
    value: Any,
    logger: tmt.log.Logger,
) -> Optional[int]:
    """
    Normalize an integer that may be unset as well.

    For a field that takes an integer input, but might be also left out,
    and has no default value.
    """

    if value is None:
        return None

    if isinstance(value, int):
        return value

    try:
        return int(value)

    except ValueError as exc:
        raise NormalizationError(key_address, value, 'unset or an integer') from exc


def normalize_storage_size(
    key_address: str,
    value: Any,
    logger: tmt.log.Logger,
) -> int:
    """
    Normalize a storage size.

    As of now, it's just a simple integer with units interpreted by the owning
    plugin. In the future, we want this function to switch to proper units
    and return ``pint.Quantity`` instead.
    """

    return normalize_int(key_address, value, logger)


def normalize_string_list(
    key_address: str,
    value: Any,
    logger: tmt.log.Logger,
) -> list[str]:
    """
    Normalize a string-or-list-of-strings input value.

    This is a fairly common input format present mostly in fmf nodes where
    tmt, to make things easier for humans, allows this:

    .. code-block:: yaml

       foo: bar

       foo:
         - bar
         - baz

    Internally, we should stick to one type only, and make sure whatever we get
    on the input, a list of strings would be the output.

    :param value: input value from key source.
    """

    if value is None:
        return []

    if isinstance(value, str):
        return [value]

    if isinstance(value, (list, tuple)):
        normalized_value: list[str] = []

        for i, raw_item in enumerate(value):
            if isinstance(raw_item, str):
                normalized_value.append(raw_item)
                continue

            raise NormalizationError(f'{key_address}[{i}]', raw_item, 'a string')

        return normalized_value

    raise NormalizationError(key_address, value, 'a string or a list of strings')


def normalize_pattern_list(
    key_address: str,
    value: Any,
    logger: tmt.log.Logger,
) -> list[Pattern[str]]:
    """
    Normalize a pattern-or-list-of-patterns input value.

    .. code-block:: yaml

       foo: 'bar.*'

       foo:
         - 'bar.*'
         - '(?i)BaZ+'
    """

    def _normalize(raw_patterns: list[Any]) -> list[Pattern[str]]:
        patterns: list[Pattern[str]] = []

        for i, raw_pattern in enumerate(raw_patterns):
            if isinstance(raw_pattern, str):
                try:
                    patterns.append(re.compile(raw_pattern))

                except Exception:
                    raise NormalizationError(
                        f'{key_address}[{i}]', raw_pattern, 'a regular expression'
                    )

            elif isinstance(raw_pattern, re.Pattern):
                patterns.append(raw_pattern)

            else:
                raise NormalizationError(
                    f'{key_address}[{i}]', raw_pattern, 'a regular expression'
                )

        return patterns

    if value is None:
        return []

    if isinstance(value, str):
        return _normalize([value])

    if isinstance(value, (list, tuple)):
        return _normalize(list(value))

    raise NormalizationError(
        key_address, value, 'a regular expression or a list of regular expressions'
    )


def normalize_integer_list(
    key_address: str,
    value: Any,
    logger: tmt.log.Logger,
) -> list[int]:
    """
    Normalize an integer-or-list-of-integers input value.

    .. code-block:: yaml

       foo: 11

       foo:
         - 11
         - 79

    :param value: input value from key source.
    """

    if value is None:
        return []

    normalized: list[int] = []

    if not isinstance(value, list):
        value = [value]

    for i, item in enumerate(value):
        try:
            normalized.append(int(item))

        except Exception as exc:
            raise NormalizationError(f'{key_address}[{i}]', item, 'an integer') from exc

    return normalized


def normalize_path(
    key_address: str,
    value: Any,
    logger: tmt.log.Logger,
) -> Optional[Path]:
    """
    Normalize content of the test `path` key
    """

    if value is None:
        return None

    if isinstance(value, Path):
        return value

    if isinstance(value, str):
        return Path(value)

    raise tmt.utils.NormalizationError(key_address, value, 'a string')


def normalize_path_list(
    key_address: str,
    value: Union[None, str, list[str]],
    logger: tmt.log.Logger,
) -> list[Path]:
    """
    Normalize a path-or-list-of-paths input value.

    This is a fairly common input format present mostly in fmf nodes where
    tmt, to make things easier for humans, allows this:

    .. code-block:: yaml

       foo: /foo/bar

       foo:
         - /foo/bar
         - /baz

    Internally, we should stick to one type only, and make sure whatever we get
    on the input, a list of strings would be the output.

    :param value: input value from key source.
    """

    if value is None:
        return []

    if isinstance(value, str):
        return [Path(value)]

    if isinstance(value, (list, tuple)):
        return [Path(path) for path in value]

    raise NormalizationError(key_address, value, 'a path or a list of paths')


def normalize_shell_script_list(
    key_address: str,
    value: Union[None, str, list[str]],
    logger: tmt.log.Logger,
) -> list[ShellScript]:
    """
    Normalize a string-or-list-of-strings input value.

    This is a fairly common input format present mostly in fmf nodes where
    tmt, to make things easier for humans, allows this:

    .. code-block:: yaml

       foo: bar

       foo:
         - bar
         - baz

    Internally, we should stick to one type only, and make sure whatever we get
    on the input, a list of strings would be the output.

    :param value: input value from key source.
    """

    if value is None:
        return []

    if isinstance(value, str):
        return [ShellScript(value)]

    if isinstance(value, (list, tuple)):
        return [ShellScript(str(item)) for item in value]

    raise NormalizationError(key_address, value, 'a string or a list of strings')


def normalize_shell_script(
    key_address: str,
    value: Union[None, str],
    logger: tmt.log.Logger,
) -> Optional[ShellScript]:
    """
    Normalize a single shell script input that may be unset.

    :param value: input value from key source.
    """

    if value is None:
        return None

    if isinstance(value, str):
        return ShellScript(value)

    raise NormalizationError(key_address, value, 'a string')


def normalize_adjust(
    key_address: str, raw_value: Any, logger: tmt.log.Logger
) -> Optional[list['tmt.base._RawAdjustRule']]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    return [raw_value]


def normalize_string_dict(
    key_address: str,
    raw_value: Any,
    logger: tmt.log.Logger,
) -> dict[str, str]:
    """
    Normalize a key/value dictionary.

    The input value could be specified in two ways:

    * a dictionary, or
    * a list of ``KEY=VALUE`` strings.

    For example, the following are acceptable inputs:

    .. code-block:: python

       {'foo': 'bar', 'qux': 'quux'}

       ['foo=bar', 'qux=quux']

    :param value: input value from key source.
    """

    if isinstance(raw_value, dict):
        return {str(key).strip(): str(value).strip() for key, value in raw_value.items()}

    if isinstance(raw_value, (list, tuple)):
        normalized = {}

        for datum in cast(list[str], raw_value):
            try:
                key, value = datum.split('=', 1)

            except ValueError as exc:
                raise NormalizationError(key_address, datum, 'a KEY=VALUE string') from exc

            normalized[key.strip()] = value.strip()

        return normalized

    raise tmt.utils.NormalizationError(
        key_address, value, 'a dictionary or a list of KEY=VALUE strings'
    )


def normalize_data_amount(
    key_address: str,
    raw_value: Any,
    logger: tmt.log.Logger,
) -> 'Size':
    from pint import Quantity

    if isinstance(raw_value, Quantity):
        return raw_value

    if isinstance(raw_value, str):
        import tmt.hardware

        return tmt.hardware.UNITS(raw_value)

    raise NormalizationError(key_address, raw_value, 'a quantity or a string')


# TODO: once we replace our custom "containers" with pydantic's `MetadataContainer`,
# this enum and `_field_value_sources` should move there.
class FieldValueSource(enum.Enum):
    """
    Indicates source of metadata field value.
    """

    #: The value was provided by fmf node key.
    FMF = 'fmf'

    #: The value was provided by CLI option.
    CLI = 'cli'

    #: The value is the default value defined for the field.
    DEFAULT = 'default'

    POLICY = 'policy'


class NormalizeKeysMixin(_CommonBase):
    """
    Mixin adding support for loading fmf keys into object attributes.

    When invoked, annotated class-level variables are searched for in a given source
    container - a mapping, an fmf node, etc. - and if the key of the same name as the
    variable exists, its value is "promoted" to instance variable.

    If a method named ``_normalize_<variable name>`` exists, it is called with the fmf
    key value as its single argument, and its return value is assigned to instance
    variable. This gives class chance to modify or transform the original value when
    needed, e.g. to convert the original value to a type more suitable for internal
    processing.
    """

    _field_value_sources: dict[str, FieldValueSource]

    # If specified, keys would be iterated over in the order as listed here.
    _KEYS_SHOW_ORDER: list[str] = []

    @classmethod
    def _iter_key_annotations(cls) -> Iterator[tuple[str, Any]]:
        """
        Iterate over keys' type annotations.

        Keys are yielded in the order: keys declared by parent classes first, then
        keys declared by the class itself, all following the order in which keys
        were defined in their respective classes.

        :yields: pairs of key name and its annotations.
        """

        def _iter_class_annotations(klass: type) -> Iterator[tuple[str, Any]]:
            # Skip, needs fixes to become compatible
            if klass is Common:
                return

            for name, value in klass.__dict__.get('__annotations__', {}).items():
                # Skip special fields that are not keys.
                if name in (
                    '_KEYS_SHOW_ORDER',
                    '_linter_registry',
                    '_export_plugin_registry',
                    '_field_value_sources',
                ):
                    continue

                yield (name, value)

        # Reverse MRO to start with the most base classes first, to iterate over keys
        # in the order they are defined.
        for klass in reversed(cls.__mro__):
            yield from _iter_class_annotations(klass)

    @classmethod
    def keys(cls) -> Iterator[str]:
        """
        Iterate over key names.

        Keys are yielded in the order: keys declared by parent classes first, then
        keys declared by the class itself, all following the order in which keys
        were defined in their respective classes.

        :yields: key names.
        """

        for keyname, _ in cls._iter_key_annotations():
            yield keyname

    def items(self) -> Iterator[tuple[str, Any]]:
        """
        Iterate over keys and their values.

        Keys are yielded in the order: keys declared by parent classes first, then
        keys declared by the class itself, all following the order in which keys
        were defined in their respective classes.

        :yields: pairs of key name and its value.
        """
        # SIM118 Use `{key} in {dict}` instead of `{key} in {dict}.keys().
        # "Type[SerializableContainerDerivedType]" has no attribute "__iter__" (not iterable)
        for keyname in self.keys():
            yield (keyname, getattr(self, keyname))

    # TODO: exists for backward compatibility for the transition period. Once full
    # type annotations land, there should be no need for extra _keys attribute.
    @classmethod
    def _keys(cls) -> list[str]:
        """
        Return a list of names of object's keys.
        """

        return list(cls.keys())

    def _load_keys(
        self,
        key_source: dict[str, Any],
        key_source_name: str,
        logger: tmt.log.Logger,
    ) -> None:
        """
        Extract values for class-level attributes, and verify they match declared types.
        """

        from tmt.container import key_to_option

        log_shift, log_level = 2, 4

        debug_intro = functools.partial(
            logger.debug,
            shift=log_shift - 1,
            level=log_level,
            topic=tmt.log.Topic.KEY_NORMALIZATION,
        )
        debug = functools.partial(
            logger.debug,
            shift=log_shift,
            level=log_level,
            topic=tmt.log.Topic.KEY_NORMALIZATION,
        )

        debug_intro('key source')
        for k, v in key_source.items():
            debug(f'{k}: {v} ({type(v)})')

        debug('')

        self._field_value_sources = {}

        for keyname, keytype in self._iter_key_annotations():
            key_address = f'{key_source_name}:{keyname}'

            source_keyname = key_to_option(keyname)
            source_keyname_cli = keyname

            # Do not indent this particular entry like the rest, so it could serve
            # as a "header" for a single key processing.
            debug_intro('key', key_address)
            debug('field', source_keyname)

            debug('desired type', str(keytype))

            value: Any = None
            value_source: FieldValueSource

            # Verbose, let's hide it a bit deeper.
            debug('dict', self.__dict__, level=log_level + 1)

            if hasattr(self, keyname):
                # If the key exists as instance's attribute already, it is because it's been
                # declared with a default value, and the attribute now holds said default value.
                default_value = getattr(self, keyname)

                # If the default value is a mutable container, we cannot use it directly.
                # Should we do so, the very same default value would be assigned to multiple
                # instances/attributes instead of each instance having its own distinct container.
                if isinstance(default_value, (list, dict)):
                    debug('detected mutable default')
                    default_value = copy.copy(default_value)

                debug('default value', str(default_value))
                debug('default value type', str(type(default_value)))

                if source_keyname in key_source:
                    value = key_source[source_keyname]
                    value_source = FieldValueSource.FMF

                elif source_keyname_cli in key_source:
                    value = key_source[source_keyname_cli]
                    value_source = FieldValueSource.CLI

                else:
                    value = default_value
                    value_source = FieldValueSource.DEFAULT

                debug('raw value', str(value))
                debug('raw value type', str(type(value)))
                debug('raw value source', value_source.name)

            else:
                if source_keyname in key_source:
                    value = key_source[source_keyname]
                    value_source = FieldValueSource.FMF

                elif source_keyname_cli in key_source:
                    value = key_source[source_keyname_cli]
                    value_source = FieldValueSource.CLI

                else:
                    value = None
                    value_source = FieldValueSource.DEFAULT

                debug('raw value', str(value))
                debug('raw value type', str(type(value)))
                debug('raw value source', value_source.name)

            value = dataclass_normalize_field(
                self, key_address, keyname, value, value_source, logger
            )

            debug('final value', str(value))
            debug('final value type', str(type(value)))
            debug('final value source', value_source.name)

            # Apparently pointless, but makes the debugging output more readable.
            # There may be plenty of tests and plans and keys, a bit of spacing
            # can't hurt.
            debug('')

        debug_intro('normalized fields')
        for k, v in self.__dict__.items():
            debug(f'{k}: {v} ({type(v)})')

        debug('')

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class LoadFmfKeysMixin(NormalizeKeysMixin):
    def __init__(
        self,
        *,
        node: fmf.Tree,
        logger: tmt.log.Logger,
        **kwargs: Any,
    ) -> None:
        self._load_keys(node.get(), node.name, logger)

        super().__init__(node=node, logger=logger, **kwargs)


def locate_key_origin(node: fmf.Tree, key: str) -> Optional[fmf.Tree]:
    """
    Find an fmf node where the given key is defined.

    :param node: node to begin with.
    :param key: key to look for.
    :returns: first node in which the key is defined, ``None`` if ``node`` nor
        any of its parents define it.
    """

    # Find the closest parent with different key content
    while node.parent:
        if node.get(key) != node.parent.get(key):
            break
        node = node.parent

    # Return node only if the key is defined
    if node.get(key) is None:
        return None

    return node


def is_key_origin(node: fmf.Tree, key: str) -> bool:
    """
    Find out whether the given key is defined in the given node.

    :param node: node to check.
    :param key: key to check.
    :returns: ``True`` if the key is defined in ``node``, not by one of its
        parents, ``False`` otherwise.
    """

    origin = locate_key_origin(node, key)

    return origin is not None and node.name == origin.name


def resource_files(path: Union[str, Path], package: Union[str, ModuleType] = "tmt") -> Path:
    """
    Helper function to get path of package file or directory.

    A thin wrapper for :py:func:`importlib.resources.files`:
    ``files()`` returns ``Traversable`` object, though in our use-case
    it should always produce a :py:class:`pathlib.PosixPath` object.
    Converting it to :py:class:`tmt.utils.Path` instance should be
    safe and stick to the "``Path`` only!" rule in tmt's code base.

    :param path: file or directory path to retrieve, relative to the ``package`` root.
    :param package: package in which to search for the file/directory.
    :returns: an absolute path to the requested file or directory.
    """

    return Path(importlib.resources.files(package)) / path  # type: ignore[arg-type]


class Stopwatch(contextlib.AbstractContextManager['Stopwatch']):
    start_time: datetime.datetime
    end_time: datetime.datetime

    def __init__(self) -> None:
        pass

    def __enter__(self) -> 'Stopwatch':
        self.start_time = datetime.datetime.now(datetime.timezone.utc)

        return self

    def __exit__(self, *args: object) -> None:
        self.end_time = datetime.datetime.now(datetime.timezone.utc)

    @property
    def duration(self) -> datetime.timedelta:
        return self.end_time - self.start_time


def format_timestamp(timestamp: datetime.datetime) -> str:
    """
    Convert timestamp to a human readable format
    """

    return timestamp.isoformat()


def format_duration(duration: datetime.timedelta) -> str:
    """
    Convert duration to a human readable format
    """

    # A helper variable to hold the duration while we cut away days, hours and seconds.
    counter = int(duration.total_seconds())

    hours, counter = divmod(counter, 3600)
    minutes, seconds = divmod(counter, 60)

    return f'{hours:02}:{minutes:02}:{seconds:02}'


def retry(
    func: Callable[..., T],
    attempts: int,
    interval: int,
    label: str,
    logger: tmt.log.Logger,
    *args: Any,
    **kwargs: Any,
) -> T:
    """
    Retry functionality to be used elsewhere in the code.

    :param func: function to be called with all unclaimed positional
        and keyword arguments.
    :param attempts: number of tries to call the function
    :param interval: amount of seconds to wait before a new try
    :param label: action to retry
    :returns: propagates return value of ``func``.
    """

    exceptions: list[Exception] = []
    for i in range(attempts):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            exceptions.append(exc)
            logger.debug(
                'retry',
                f"{label} failed, {attempts - i} retries left, "
                f"trying again in {interval:.2f} seconds.",
            )
            logger.fail(str(exc))
            time.sleep(interval)
    raise RetryError(label, causes=exceptions)


def get_url_content(url: str) -> str:
    """
    Get content of a given URL as a string
    """

    try:
        with retry_session() as session:
            response = session.get(url)

            if response.ok:
                return response.text

    except Exception as error:
        raise GeneralError(f"Could not open url '{url}'.") from error

    raise GeneralError(f"Could not open url '{url}'.")


def is_url(url: str) -> bool:
    """
    Check if the given string is a valid URL
    """

    parsed = urllib.parse.urlparse(url)
    return bool(parsed.scheme and parsed.netloc)


# Handle the thread synchronization for the `catch_warnings(...)` context manager
_catch_warning_lock = RLock()
ActionType = Literal['default', 'error', 'ignore', 'always', 'module', 'once']


@contextlib.contextmanager
def catch_warnings_safe(
    action: ActionType,
    category: type[Warning] = Warning,
) -> Iterator[None]:
    """
    Optionally catch the given warning category.

    Using this context manager you can catch/suppress given warnings category. These warnings gets
    re-enabled/reset with an exit from this context manager.

    This function uses a reentrant lock for thread synchronization to be a thread-safe. That's why
    it's wrapping :py:meth:`warnings.catch_warnings` instead of using it directly.

    The example can be suppressing of the urllib insecure request warning:

    .. code-block:: python

        with catch_warnings_safe('ignore', urllib3.exceptions.InsecureRequestWarning):
            ...
    """

    with _catch_warning_lock, warnings.catch_warnings():
        warnings.simplefilter(action=action, category=category)
        yield

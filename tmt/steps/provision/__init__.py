import ast
import contextlib
import dataclasses
import enum
import functools
import hashlib
import os
import re
import secrets
import shlex
import signal as _signal
import string
import subprocess
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from shlex import quote
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    NewType,
    Optional,
    TypeVar,
    Union,
    cast,
    overload,
)

import click
import fmf
import fmf.utils
from click import echo

import tmt
import tmt.hardware
import tmt.log
import tmt.package_managers
import tmt.plugins
import tmt.queue
import tmt.steps
import tmt.steps.provision
import tmt.steps.scripts
import tmt.utils
import tmt.utils.wait
from tmt._compat.typing import Self
from tmt.container import SerializableContainer, container, field, key_to_option
from tmt.log import Logger
from tmt.options import option
from tmt.package_managers import (
    FileSystemPath,
    Package,
    PackageManagerClass,
)
from tmt.plugins import PluginRegistry
from tmt.steps import Action, ActionTask, PhaseQueue
from tmt.utils import (
    Command,
    GeneralError,
    OnProcessEndCallback,
    OnProcessStartCallback,
    Path,
    ProvisionError,
    ShellScript,
    configure_constant,
    effective_workdir_root,
)
from tmt.utils.hints import get_hint
from tmt.utils.wait import Deadline, Waiting

if TYPE_CHECKING:
    import tmt.base
    import tmt.cli
    from tmt._compat.typing import TypeAlias


#: How many seconds to wait for a connection to succeed after guest reboot.
#: This is the default value tmt would use unless told otherwise.
DEFAULT_REBOOT_TIMEOUT: int = 10 * 60

#: How many seconds to wait for a connection to succeed after guest reboot.
#: This is the effective value, combining the default and optional envvar,
#: ``TMT_REBOOT_TIMEOUT``.
REBOOT_TIMEOUT: int = configure_constant(DEFAULT_REBOOT_TIMEOUT, 'TMT_REBOOT_TIMEOUT')


def default_reboot_waiting() -> Waiting:
    """
    Create default waiting context for guest reboots.
    """

    return Waiting(deadline=Deadline.from_seconds(REBOOT_TIMEOUT))


# When waiting for guest to recover from reboot, try re-connecting every
# this many seconds.
RECONNECT_WAIT_TICK = 5
RECONNECT_WAIT_TICK_INCREASE = 1.0


def default_reconnect_waiting() -> Waiting:
    """
    Create default waiting context for guest reconnect.
    """

    return Waiting(
        deadline=Deadline.from_seconds(REBOOT_TIMEOUT),
        tick=RECONNECT_WAIT_TICK,
        tick_increase=RECONNECT_WAIT_TICK_INCREASE,
    )


# Types for things Ansible can execute
ANSIBLE_COLLECTION_PLAYBOOK_PATTERN = re.compile(r'[a-zA-z0-9_]+\.[a-zA-z0-9_]+\.[a-zA-z0-9_]+')

AnsiblePlaybook: 'TypeAlias' = Path
AnsibleCollectionPlaybook = NewType('AnsibleCollectionPlaybook', str)
AnsibleApplicable = Union[AnsibleCollectionPlaybook, AnsiblePlaybook]


def configure_ssh_options() -> tmt.utils.RawCommand:
    """
    Extract custom SSH options from environment variables
    """

    options: tmt.utils.RawCommand = []

    for name, value in os.environ.items():
        match = re.match(r'TMT_SSH_([a-zA-Z_]+)', name)

        if not match:
            continue

        options.append(f'-o{match.group(1).title().replace("_", "")}={value}')

    return options


#: Default SSH options.
#: This is the default set of SSH options tmt would use for all SSH connections.
DEFAULT_SSH_OPTIONS: tmt.utils.RawCommand = [
    '-oForwardX11=no',
    '-oStrictHostKeyChecking=no',
    '-oUserKnownHostsFile=/dev/null',
    # Try establishing connection multiple times before giving up.
    '-oConnectionAttempts=5',
    '-oConnectTimeout=60',
    # Prevent ssh from disconnecting if no data has been
    # received from the server for a long time (#868).
    '-oServerAliveInterval=5',
    '-oServerAliveCountMax=60',
]

#: Base SSH options.
#: This is the base set of SSH options tmt would use for all SSH
#: connections. It is a combination of the default SSH options and those
#: provided by environment variables.
BASE_SSH_OPTIONS: tmt.utils.RawCommand = DEFAULT_SSH_OPTIONS + configure_ssh_options()

#: SSH master socket path is limited to this many characters.
#:
#: * UNIX socket path is limited to either 108 or 104 characters, depending
#:   on the platform. See `man 7 unix` and/or kernel sources, for example.
#: * SSH client processes may create paths with added "connection hash"
#:   when connecting to the master, that is a couple of characters we need
#:   space for.
#:
SSH_MASTER_SOCKET_LENGTH_LIMIT = 104 - 20

#: A minimal number of characters of guest ID hash used by
#: :py:func:`_socket_path_hash` when looking for a free SSH socket
#: filename.
SSH_MASTER_SOCKET_MIN_HASH_LENGTH = 4

#: A maximal number of characters of guest ID hash used by
#: :py:func:`_socket_path_hash` when looking for a free SSH socket
#: filename.
SSH_MASTER_SOCKET_MAX_HASH_LENGTH = 64

#: Default username to use in SSH connections.
DEFAULT_USER = 'root'


@overload
def _socket_path_trivial(
    *,
    socket_dir: Path,
    guest_id: str,
    limit_size: Literal[True] = True,
    logger: tmt.log.Logger,
) -> Optional[Path]:
    pass


@overload
def _socket_path_trivial(
    *,
    socket_dir: Path,
    guest_id: str,
    limit_size: Literal[False] = False,
    logger: tmt.log.Logger,
) -> Path:
    pass


def _socket_path_trivial(
    *,
    socket_dir: Path,
    guest_id: str,
    limit_size: bool = True,
    logger: tmt.log.Logger,
) -> Optional[Path]:
    """
    Generate SSH socket path using guest IDs
    """

    socket_path = socket_dir / f'{guest_id}.socket'

    logger.debug(f"Possible SSH master socket path '{socket_path}' (trivial method).", level=4)

    if not limit_size:
        return socket_path

    return socket_path if len(str(socket_path)) < SSH_MASTER_SOCKET_LENGTH_LIMIT else None


def _socket_path_hash(
    *,
    socket_dir: Path,
    guest_id: str,
    limit_size: bool = True,
    logger: tmt.log.Logger,
) -> Optional[Path]:
    """
    Generate SSH socket path using a hash of guest IDs.

    Generates less readable, but hopefully shorter and therefore
    acceptable filename. We try to make sure we create unique
    names for sockets, names that are not shared by multiple
    guests, and we try to make them reasonably short.
    """

    # We're using hashing function which should, in theory, be prone to
    # conflicts enough for us to never hit a collision. However, we cannot
    # rule out the chance of getting same hash for different guests, and
    # letting one socket serve two different guests is extremely hard to
    # debug.
    #
    # Therefore we try to avoid the collision by not using the
    # full size of the hash, just its substring - if we really reach the
    # point where more than one guest yields the same hash, the first
    # would use N starting characters for its socket, the second would
    # use N+1 starting characters, and so on.
    #
    # For each potential socket path, a "reservation" file is used as
    # a placeholder: once atomically created, no other guest can grab
    # the given socket path.
    for i in range(SSH_MASTER_SOCKET_MIN_HASH_LENGTH, SSH_MASTER_SOCKET_MAX_HASH_LENGTH):
        digest = hashlib.sha256(guest_id.encode()).hexdigest()[:i]

        socket_path = socket_dir / f'{digest}.socket'
        socket_reservation_path = f'{socket_path}.reservation'

        logger.debug(f"Possible SSH master socket path '{socket_path}' (hash method).", level=4)

        if limit_size and len(str(socket_path)) >= SSH_MASTER_SOCKET_LENGTH_LIMIT:
            return None

        # O_CREAT | O_EXCL means "atomic create-and-fail-if-exists".
        # It's pretty much what `tempfile` does, but we need to control
        # the full name, not just a prefix or suffix.
        try:
            fd = os.open(socket_reservation_path, flags=os.O_CREAT | os.O_EXCL)

        except FileExistsError:
            logger.debug(f"Proposed SSH socket '{socket_path}' already reserved.", level=4)
            continue

        # Successfully reserved the socket path, we can close the
        # reservation file & return the actual path.
        os.close(fd)

        return socket_path

    return None


# Default rsync options
DEFAULT_RSYNC_OPTIONS = ["-s", "-R", "-r", "-z", "--links", "--safe-links", "--delete"]

DEFAULT_RSYNC_PUSH_OPTIONS = ["-s", "-R", "-r", "-z", "--links", "--safe-links", "--delete"]
DEFAULT_RSYNC_PULL_OPTIONS = ["-s", "-R", "-r", "-z", "--links", "--safe-links", "--protect-args"]

#: A pattern to extract ``btime`` from ``/proc/stat`` file.
STAT_BTIME_PATTERN = re.compile(r'btime\s+(\d+)')


# Note: returns a static list, but we cannot make it a mere list,
# because `tmt.base` needs to be imported and that creates a circular
# import loop.
def essential_ansible_requires() -> list['tmt.base.Dependency']:
    """
    Return essential requirements for running Ansible modules
    """

    return [tmt.base.DependencySimple('/usr/bin/python3')]


def format_guest_full_name(name: str, role: Optional[str]) -> str:
    """
    Render guest's full name, i.e. name and its role
    """

    if role is None:
        return name

    return f'{name} ({role})'


class RebootModeNotSupportedError(ProvisionError):
    """A requested reboot mode is not supported by the guest"""

    def __init__(
        self,
        message: Optional[str] = None,
        guest: Optional['Guest'] = None,
        hard: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if message is not None:
            pass

        elif guest is not None:
            message = f"Guest '{guest.multihost_name}' does not support {'hard' if hard else 'soft'} reboot."  # noqa: E501

        else:
            message = f"Guest does not support {'hard' if hard else 'soft'} reboot."

        super().__init__(message, *args, **kwargs)


class CheckRsyncOutcome(enum.Enum):
    ALREADY_INSTALLED = 'already-installed'
    INSTALLED = 'installed'


T = TypeVar('T')


class GuestCapability(enum.Enum):
    """
    Various Linux capabilities
    """

    # See man 2 syslog:
    #: Read all messages remaining in the ring buffer.
    SYSLOG_ACTION_READ_ALL = 'syslog-action-read-all'
    #: Read and clear all messages remaining in the ring buffer.
    SYSLOG_ACTION_READ_CLEAR = 'syslog-action-read-clear'


@container
class GuestFacts(SerializableContainer):
    """
    Contains interesting facts about the guest.

    Inspired by Ansible or Puppet facts, interesting guest facts tmt
    discovers while managing the guest are stored in this container,
    plus the code performing the discovery of these facts.
    """

    #: Set to ``True`` by the first call to :py:meth:`sync`.
    in_sync: bool = False

    arch: Optional[str] = None
    distro: Optional[str] = None
    kernel_release: Optional[str] = None
    package_manager: Optional['tmt.package_managers.GuestPackageManager'] = field(
        # cast: since the default is None, mypy cannot infere the full type,
        # and reports `package_manager` parameter to be `object`.
        default=cast(Optional['tmt.package_managers.GuestPackageManager'], None)
    )
    bootc_builder: Optional['tmt.package_managers.GuestPackageManager'] = field(
        # cast: since the default is None, mypy cannot infere the full type,
        # and reports `bootc_builder` parameter to be `object`.
        default=cast(Optional['tmt.package_managers.GuestPackageManager'], None)
    )

    has_selinux: Optional[bool] = None
    has_systemd: Optional[bool] = None
    is_superuser: Optional[bool] = None
    is_ostree: Optional[bool] = None
    is_toolbox: Optional[bool] = None
    toolbox_container_name: Optional[str] = None
    is_container: Optional[bool] = None

    #: Various Linux capabilities and whether they are permitted to
    #: commands executed on this guest.
    capabilities: dict[GuestCapability, bool] = field(
        default_factory=cast(Callable[[], dict[GuestCapability, bool]], dict),
        serialize=lambda capabilities: {
            capability.value: enabled for capability, enabled in capabilities.items()
        }
        if capabilities
        else {},
        unserialize=lambda raw_value: {
            GuestCapability(raw_capability): enabled
            for raw_capability, enabled in raw_value.items()
        },
    )

    os_release_content: dict[str, str] = field(default_factory=dict)
    lsb_release_content: dict[str, str] = field(default_factory=dict)

    def has_capability(self, cap: GuestCapability) -> bool:
        if not self.capabilities:
            return False

        return self.capabilities.get(cap, False)

    # TODO nothing but a fancy helper, to check for some special errors that
    # may appear this soon in provisioning. But, would it make sense to put
    # this detection into the `GuestSsh.execute()` method?
    def _execute(self, guest: 'Guest', command: Command) -> Optional[tmt.utils.CommandOutput]:
        """
        Run a command on the given guest.

        On top of the basic :py:meth:`Guest.execute`, this helper is able to
        detect a common issue with guest access. Facts are the first info tmt
        fetches from the guest, and would raise the error as soon as possible.

        :returns: command output if the command quit with a zero exit code,
            ``None`` otherwise.
        :raises tmt.units.GeneralError: when logging into the guest fails
            because of a username mismatch.
        """

        try:
            return guest.execute(command, silent=True)

        except tmt.utils.RunError as exc:
            if exc.stdout and 'Please login as the user' in exc.stdout:
                raise tmt.utils.GeneralError(f'Login to the guest failed.\n{exc.stdout}') from exc
            if (
                exc.stderr
                and f'executable file `{tmt.utils.DEFAULT_SHELL}` not found' in exc.stderr
            ):
                raise tmt.utils.GeneralError(
                    f'{tmt.utils.DEFAULT_SHELL.capitalize()} is required on the guest.'
                ) from exc

        return None

    def _fetch_keyval_file(self, guest: 'Guest', filepath: Path) -> dict[str, str]:
        """
        Load key/value pairs from a file on the given guest.

        Converts file with ``key=value`` pairs into a mapping. Some values might
        be wrapped with quotes.

        .. code:: shell

           $ cat /etc/os-release
           NAME="Ubuntu"
           VERSION="20.04.5 LTS (Focal Fossa)"
           ID=ubuntu
           ID_LIKE=debian
           ...

        See https://www.freedesktop.org/software/systemd/man/os-release.html for
        more details on syntax of these files.

        :returns: mapping with key/value pairs loaded from ``filepath``, or an
            empty mapping if it was impossible to load the content.
        """

        content: dict[str, str] = {}

        output = self._execute(guest, Command('cat', filepath))

        if not output or not output.stdout:
            return content

        def _iter_pairs() -> Iterator[tuple[str, str]]:
            assert output  # narrow type in a closure
            assert output.stdout  # narrow type in a closure

            line_pattern = re.compile(r'([A-Z][A-Z_0-9]+)=(.*)')

            for line_number, line in enumerate(output.stdout.splitlines(keepends=False), start=1):
                line = line.rstrip()

                if not line or line.startswith('#'):
                    continue

                match = line_pattern.match(line)

                if not match:
                    raise tmt.utils.ProvisionError(
                        f"Cannot parse line {line_number} in '{filepath}' on guest '{guest.name}':"
                        f" {line}"
                    )

                key, value = match.groups()

                if value and value[0] in '"\'':
                    value = ast.literal_eval(value)

                yield key, value

        return dict(_iter_pairs())

    def _probe(self, guest: 'Guest', probes: list[tuple[Command, T]]) -> Optional[T]:
        """
        Find a first successful command.

        :param guest: the guest to run commands on.
        :param probes: list of command/mark pairs.
        :returns: "mark" corresponding to the first command to quit with
            a zero exit code.
        :raises tmt.utils.GeneralError: when no command succeeded.
        """

        for command, outcome in probes:
            if self._execute(guest, command):
                return outcome

        return None

    def _query(self, guest: 'Guest', probes: list[tuple[Command, str]]) -> Optional[str]:
        """
        Find a first successful command, and extract info from its output.

        :param guest: the guest to run commands on.
        :param probes: list of command/pattenr pairs.
        :returns: substring extracted by the first matching pattern.
        :raises tmt.utils.GeneralError: when no command succeeded, or when no
            pattern matched.
        """

        for command, pattern in probes:
            output = self._execute(guest, command)

            if not output or not output.stdout:
                guest.debug('query', f"Command '{command!s}' produced no usable output.")
                continue

            match = re.search(pattern, output.stdout)

            if not match:
                guest.debug('query', f"Command '{command!s}' produced no usable output.")
                continue

            return match.group(1)

        return None

    def _query_arch(self, guest: 'Guest') -> Optional[str]:
        return self._query(guest, [(Command('arch'), r'(.+)')])

    def _query_distro(self, guest: 'Guest') -> Optional[str]:
        # Try some low-hanging fruits first. We already might have the answer,
        # provided by some standardized locations.
        if 'PRETTY_NAME' in self.os_release_content:
            return self.os_release_content['PRETTY_NAME']

        if 'DISTRIB_DESCRIPTION' in self.lsb_release_content:
            return self.lsb_release_content['DISTRIB_DESCRIPTION']

        # Nope, inspect more files.
        return self._query(
            guest,
            [
                (Command('cat', '/etc/redhat-release'), r'(.*)'),
                (Command('cat', '/etc/fedora-release'), r'(.*)'),
            ],
        )

    def _query_kernel_release(self, guest: 'Guest') -> Optional[str]:
        return self._query(guest, [(Command('uname', '-r'), r'(.+)')])

    def _query_package_manager(
        self, guest: 'Guest'
    ) -> Optional['tmt.package_managers.GuestPackageManager']:
        # Discover as many package managers as possible: sometimes, the
        # first discovered package manager is not the only or the best
        # one available. Collect them, and sort them by their priorities
        # to find the most suitable one.

        discovered_package_managers: list[
            PackageManagerClass[tmt.package_managers.PackageManagerEngine]
        ] = [
            package_manager_class
            for package_manager_id, package_manager_class in tmt.package_managers._PACKAGE_MANAGER_PLUGIN_REGISTRY.items()  # noqa: E501
            if self._execute(guest, package_manager_class.probe_command)
        ]

        discovered_package_managers.sort(key=lambda pm: pm.probe_priority, reverse=True)

        if discovered_package_managers:
            guest.debug(
                'Discovered package managers',
                fmf.utils.listed([pm.NAME for pm in discovered_package_managers]),
                level=4,
            )

            return discovered_package_managers[0].NAME

        return None

    def _query_bootc_builder(
        self, guest: 'Guest'
    ) -> Optional['tmt.package_managers.GuestPackageManager']:
        # Discover as many package managers as possible: sometimes, the
        # first discovered package manager is not the only or the best
        # one available. Collect them, and sort them by their priorities
        # to find the most suitable one.

        discovered_package_managers: list[
            PackageManagerClass[tmt.package_managers.PackageManagerEngine]
        ] = []

        for (
            package_manager_class
        ) in tmt.package_managers._PACKAGE_MANAGER_PLUGIN_REGISTRY.iter_plugins():
            if not package_manager_class.bootc_builder:
                continue

            if self._execute(guest, package_manager_class.probe_command):
                discovered_package_managers.append(package_manager_class)

        discovered_package_managers.sort(key=lambda pm: pm.probe_priority, reverse=True)

        if discovered_package_managers:
            guest.debug(
                'Discovered bootc builders',
                fmf.utils.listed([pm.NAME for pm in discovered_package_managers]),
                level=4,
            )

            return discovered_package_managers[0].NAME

        return None

    def _query_has_selinux(self, guest: 'Guest') -> Optional[bool]:
        """
        Detect whether guest uses SELinux.

        For detection ``/proc/filesystems`` is used, see ``man 5 filesystems`` for details.
        """

        output = self._execute(guest, Command('cat', '/proc/filesystems'))

        if output is None or output.stdout is None:
            return None

        return 'selinux' in output.stdout

    def _query_has_systemd(self, guest: 'Guest') -> Optional[bool]:
        """
        Detect whether guest uses systemd.
        For detection we check if systemctl exists and is executable.
        """
        try:
            guest.execute(Command('systemctl', '--version'), silent=True)
            return True
        except tmt.utils.RunError:
            return False

    def _query_is_superuser(self, guest: 'Guest') -> Optional[bool]:
        output = self._execute(guest, Command('whoami'))

        if output is None or output.stdout is None:
            return None

        return output.stdout.strip() == 'root'

    def _query_is_ostree(self, guest: 'Guest') -> Optional[bool]:
        # https://github.com/vrothberg/chkconfig/commit/538dc7edf0da387169d83599fe0774ea080b4a37#diff-562b9b19cb1cd12a7343ce5c739745ebc8f363a195276ca58e926f22927238a5R1334
        output = self._execute(
            guest,
            ShellScript(
                """
                ( [ -e /run/ostree-booted ] || [ -L /ostree ] ) && echo yes || echo no
                """
            ).to_shell_command(),
        )

        if output is None or output.stdout is None:
            return None

        return output.stdout.strip() == 'yes'

    def _query_is_toolbox(self, guest: 'Guest') -> Optional[bool]:
        # https://www.reddit.com/r/Fedora/comments/g6flgd/toolbox_specific_environment_variables/
        output = self._execute(
            guest,
            ShellScript('[ -e /run/.toolboxenv ] && echo yes || echo no').to_shell_command(),
        )

        if output is None or output.stdout is None:
            return None

        return output.stdout.strip() == 'yes'

    def _query_toolbox_container_name(self, guest: 'Guest') -> Optional[str]:
        output = self._execute(
            guest,
            ShellScript('[ -e /run/.containerenv ] && echo yes || echo no').to_shell_command(),
        )

        if output is None or output.stdout is None:
            return None

        if output.stdout.strip() == 'no':
            return None

        output = self._execute(guest, Command('cat', '/run/.containerenv'))

        if output is None or output.stdout is None:
            return None

        for line in output.stdout.splitlines():
            if line.startswith('name="'):
                return line[6:-1]

        return None

    def _query_is_container(self, guest: 'Guest') -> Optional[bool]:
        """
        Detect whether guest is a container (running systemd)

        In containers running systemd pid 1 has environment variable ``container`` set
        (e.g. container=podman). See https://systemd.io/CONTAINER_INTERFACE/ for more details.
        """
        output = self._execute(guest, ShellScript('echo -n "$container"').to_shell_command())

        if output is None or output.stdout is None:
            return None

        return len(output.stdout) > 0

    def _query_capabilities(self, guest: 'Guest') -> dict[GuestCapability, bool]:
        # TODO: there must be a canonical way of getting permitted capabilities.
        # For now, we're interested in whether we can access kernel message buffer.
        return {
            GuestCapability.SYSLOG_ACTION_READ_ALL: True,
            GuestCapability.SYSLOG_ACTION_READ_CLEAR: True,
        }

    def sync(self, guest: 'Guest') -> None:
        """
        Update stored facts to reflect the given guest
        """

        self.os_release_content = self._fetch_keyval_file(guest, Path('/etc/os-release'))
        self.lsb_release_content = self._fetch_keyval_file(guest, Path('/etc/lsb-release'))

        self.arch = self._query_arch(guest)
        self.distro = self._query_distro(guest)
        self.kernel_release = self._query_kernel_release(guest)
        self.package_manager = self._query_package_manager(guest)
        self.bootc_builder = self._query_bootc_builder(guest)
        self.has_selinux = self._query_has_selinux(guest)
        self.has_systemd = self._query_has_systemd(guest)
        self.is_superuser = self._query_is_superuser(guest)
        self.is_ostree = self._query_is_ostree(guest)
        self.is_toolbox = self._query_is_toolbox(guest)
        self.toolbox_container_name = self._query_toolbox_container_name(guest)
        self.is_container = self._query_is_container(guest)
        self.capabilities = self._query_capabilities(guest)

        self.in_sync = True

    def format(self) -> Iterator[tuple[str, str, str]]:
        """
        Format facts for pretty printing.

        :yields: three-item tuples: the field name, its pretty label, and formatted representation
            of its value.
        """

        yield 'arch', 'arch', self.arch or 'unknown'
        yield 'distro', 'distro', self.distro or 'unknown'
        yield 'kernel_release', 'kernel', self.kernel_release or 'unknown'
        yield (
            'package_manager',
            'package manager',
            self.package_manager if self.package_manager else 'unknown',
        )
        yield (
            'bootc builder',
            'bootc builder',
            self.bootc_builder if self.bootc_builder else 'unknown',
        )
        yield 'has_selinux', 'selinux', 'yes' if self.has_selinux else 'no'
        yield 'has_systemd', 'systemd', 'yes' if self.has_systemd else 'no'
        yield 'is_superuser', 'is superuser', 'yes' if self.is_superuser else 'no'
        yield 'is_container', 'is_container', 'yes' if self.is_container else 'no'


GUEST_FACTS_INFO_FIELDS: list[str] = ['arch', 'distro']
GUEST_FACTS_VERBOSE_FIELDS: list[str] = [
    # SIM118: Use `{key} in {dict}` instead of `{key} in {dict}.keys()`
    # "NormalizeKeysMixin" has no attribute "__iter__" (not iterable)
    key
    for key in GuestFacts.keys()  # noqa: SIM118
    if key not in GUEST_FACTS_INFO_FIELDS
]


def normalize_hardware(
    key_address: str,
    raw_hardware: Union[None, tmt.hardware.Spec, tmt.hardware.Hardware],
    logger: tmt.log.Logger,
) -> Optional[tmt.hardware.Hardware]:
    """
    Normalize a ``hardware`` key value.

    :param key_address: location of the key being that's being normalized.
    :param logger: logger to use for logging.
    :param raw_hardware: input from either command line or fmf node.
    """

    if raw_hardware is None:
        return None

    if isinstance(raw_hardware, tmt.hardware.Hardware):
        return raw_hardware

    # From command line
    if isinstance(raw_hardware, (list, tuple)):
        merged: dict[str, Any] = {}

        for raw_datum in raw_hardware:
            components = tmt.hardware.ConstraintComponents.from_spec(raw_datum)

            if (
                components.name not in tmt.hardware.CHILDLESS_CONSTRAINTS
                and components.child_name is None
            ):
                raise tmt.utils.SpecificationError(
                    f"Hardware requirement '{raw_datum}' lacks "
                    f"child property ({components.name}[N].M)."
                )

            if (
                components.name in tmt.hardware.INDEXABLE_CONSTRAINTS
                and components.peer_index is None
            ):
                raise tmt.utils.SpecificationError(
                    f"Hardware requirement '{raw_datum}' lacks entry index ({components.name}[N])."
                )

            if components.peer_index is not None:
                # This should not happen, the test above already ruled
                # out `child_name` being `None`, but mypy does not know
                # everything is fine.
                assert components.child_name is not None  # narrow type

                if components.name not in merged:
                    merged[components.name] = []

                # Calculate the number of placeholders needed.
                placeholders = components.peer_index - len(merged[components.name]) + 1

                # Fill in empty spots between the existing ones and the
                # one we're adding with placeholders.
                if placeholders > 0:
                    merged[components.name].extend([{} for _ in range(placeholders)])

                merged[components.name][components.peer_index][components.child_name] = (
                    f'{components.operator} {components.value}'
                )

            elif components.name == 'cpu' and components.child_name == 'flag':
                if components.name not in merged:
                    merged[components.name] = {}

                if 'flag' not in merged['cpu']:
                    merged['cpu']['flag'] = []

                merged['cpu']['flag'].append(f'{components.operator} {components.value}')

            elif components.child_name:
                if components.name not in merged:
                    merged[components.name] = {}

                merged[components.name][components.child_name] = (
                    f'{components.operator} {components.value}'
                )

            else:
                merged[components.name] = f'{components.operator} {components.value}'

        # Very crude, we will need something better to handle `and` and
        # `or` and nesting.
        def _drop_placeholders(data: dict[str, Any]) -> dict[str, Any]:
            new_data: dict[str, Any] = {}

            for key, value in data.items():
                if isinstance(value, list):
                    new_data[key] = []

                    for item in value:
                        if isinstance(item, dict) and not item:
                            continue

                        new_data[key].append(item)

                else:
                    new_data[key] = value

            return new_data

        # TODO: if the index matters - and it does, because `disk[0]` is
        # often a "root disk" - we need sparse list. Cannot prune
        # placeholders now, because it would turn `disk[1]` into `disk[0]`,
        # overriding whatever was set for the root disk.
        # https://github.com/teemtee/tmt/issues/3004 for tracking.
        # merged = _drop_placeholders(merged)

        return tmt.hardware.Hardware.from_spec(merged)

    # From fmf
    return tmt.hardware.Hardware.from_spec(raw_hardware)


GuestDataT = TypeVar('GuestDataT', bound='GuestData')


@container
class GuestData(SerializableContainer):
    """
    Keys necessary to describe, create, save and restore a guest.

    Very basic set of keys shared across all known guest classes.
    """

    # TODO: it'd be nice to generate this from all fields, but it seems some
    # fields are not created by `field()` - not sure why, but we can fix that
    # later.
    #: List of fields that are not allowed to be set via fmf keys/CLI options.
    _OPTIONLESS_FIELDS: tuple[str, ...] = ('primary_address', 'topology_address', 'facts')

    #: Primary hostname or IP address for tmt/guest communication.
    primary_address: Optional[str] = None

    #: Guest topology hostname or IP address for guest/guest communication.
    topology_address: Optional[str] = None

    role: Optional[str] = field(
        default=None,
        option='--role',
        metavar='NAME',
        help="""
             Marks guests with the same purpose so that common actions
             can be applied to all such guests at once.
             """,
    )

    become: bool = field(
        default=False,
        is_flag=True,
        option=('-b', '--become'),
        help="""
             Whether to run tests and shell scripts in prepare and
             finish steps with ``sudo``.
             """,
    )

    facts: GuestFacts = field(
        default_factory=GuestFacts,
        serialize=lambda facts: facts.to_serialized(),
        unserialize=lambda serialized: GuestFacts.from_serialized(serialized),
    )

    hardware: Optional[tmt.hardware.Hardware] = field(
        default=cast(Optional[tmt.hardware.Hardware], None),
        option='--hardware',
        help="""
             Hardware requirements the provisioned guest must satisfy.
             """,
        metavar='KEY=VALUE',
        multiple=True,
        normalize=normalize_hardware,
        serialize=lambda hardware: hardware.to_spec() if hardware else None,
        unserialize=lambda serialized: tmt.hardware.Hardware.from_spec(serialized)
        if serialized is not None
        else None,
    )

    # TODO: find out whether this could live in DataContainer. It probably could,
    # but there are containers not backed by options... Maybe a mixin then?
    @classmethod
    def options(cls) -> Iterator[tuple[str, str]]:
        """
        Iterate over option names.

        Based on :py:meth:`keys`, but skips fields that cannot be set by options.

        :yields: two-item tuples, a key and corresponding option name.
        """

        for f in dataclasses.fields(cls):
            if f.name in cls._OPTIONLESS_FIELDS:
                continue

            yield f.name, key_to_option(f.name)

    @classmethod
    def from_plugin(
        cls,
        container: 'ProvisionPlugin[ProvisionStepDataT]',
    ) -> Self:
        """
        Create guest data from plugin and its current configuration
        """

        return cls(
            **{
                key: container.get(option)
                # SIM118: Use `{key} in {dict}` instead of `{key} in {dict}.keys()`.
                # "Type[ArtemisGuestData]" has no attribute "__iter__" (not iterable)
                for key, option in cls.options()
            }
        )

    def show(
        self,
        *,
        keys: Optional[list[str]] = None,
        verbose: int = 0,
        logger: tmt.log.Logger,
    ) -> None:
        """
        Display guest data in a nice way.

        :param keys: if set, only these keys would be shown.
        :param verbose: desired verbosity. Some fields may be omitted in low
            verbosity modes.
        :param logger: logger to use for logging.
        """

        # If all keys are set to their defaults, do not bother showing them - unless
        # forced to do so by the power of `-v`.
        if self.is_bare and not verbose:
            return

        keys = keys or list(self.keys())

        for key in keys:
            # TODO: teach GuestFacts to cooperate with show() methods, honor
            # the verbosity at the same time.
            if key == 'facts':
                continue

            value = getattr(self, key)

            if value == self.default(key):
                continue

            # TODO: it seems tmt.utils.format() needs a key, and logger.info()
            # does not accept already formatted string.
            if isinstance(value, (list, tuple)):
                printable_value = fmf.utils.listed(value)

            elif isinstance(value, dict):
                printable_value = tmt.utils.format_value(value)

            elif isinstance(value, tmt.hardware.Hardware):
                printable_value = tmt.utils.dict_to_yaml(value.to_spec())

            else:
                printable_value = str(value)

            logger.info(key_to_option(key).replace('-', ' '), printable_value, color='green')


@container
class GuestLog:
    # Log file name
    name: str

    # Linked guest
    guest: "Guest"

    def fetch(self, logger: tmt.log.Logger) -> Optional[str]:
        """
        Fetch and return content of a log.

        :returns: content of the log, or ``None`` if the log cannot be retrieved.
        """
        raise NotImplementedError

    def store(self, logger: tmt.log.Logger, path: Path, logname: Optional[str] = None) -> None:
        """
        Save log content to a file.

        :param logger: logger to use for logging.
        :param path: a path to save into, could be a directory
            or a file path.
        :param logname: name of the log, if not set, ``path``
            is supposed to be a file path.
        """
        log_content = self.fetch(logger)
        if log_content:
            # if path is file path
            if not path.is_dir():
                path.write_text(log_content)
            # if path is a directory
            elif logname:
                (path / logname).write_text(log_content)
            else:
                raise tmt.utils.GeneralError(
                    'Log path is a directory but log name is not defined.'
                )
        else:
            logger.warning(f'Failed to fetch log: {self.name}')


class Guest(tmt.utils.Common):
    """
    Guest provisioned for test execution

    A base class for guest-like classes. Provides some of the basic methods
    and functionality, but note some of the methods are left intentionally
    empty. These do not have valid implementation on this level, and it's up
    to Guest subclasses to provide one working in their respective
    infrastructure.

    The following keys are expected in the 'data' container::

        role ....... guest role in the multihost scenario
        guest ...... name, hostname or ip address
        become ..... boolean, whether to run shell scripts in tests, prepare, and finish with sudo

    These are by default imported into instance attributes.
    """

    # Used by save() to construct the correct container for keys.
    _data_class: type[GuestData] = GuestData

    @classmethod
    def get_data_class(cls) -> type[GuestData]:
        """
        Return step data class for this plugin.

        By default, :py:attr:`_data_class` is returned, but plugin may
        override this method to provide different class.
        """

        return cls._data_class

    role: Optional[str]

    #: Primary hostname or IP address for tmt/guest communication.
    primary_address: Optional[str] = None

    #: Guest topology hostname or IP address for guest/guest communication.
    topology_address: Optional[str] = None

    become: bool

    hardware: Optional[tmt.hardware.Hardware]

    # Flag to indicate localhost guest, requires special handling
    localhost = False

    # TODO: do we need this list? Can whatever code is using it use _data_class directly?
    # List of supported keys
    # (used for import/export to/from attributes during load and save)
    @property
    def _keys(self) -> list[str]:
        return list(self.get_data_class().keys())

    def __init__(
        self,
        *,
        data: GuestData,
        name: Optional[str] = None,
        parent: Optional[tmt.utils.Common] = None,
        logger: tmt.log.Logger,
    ) -> None:
        """
        Initialize guest data
        """
        self.guest_logs: list[GuestLog] = []

        super().__init__(logger=logger, parent=parent, name=name)
        self.load(data)

    def _random_name(self, prefix: str = '', length: int = 16) -> str:
        """
        Generate a random name
        """

        # Append at least 5 random characters
        min_random_part = max(5, length - len(prefix))
        name = prefix + ''.join(
            secrets.choice(string.ascii_letters) for _ in range(min_random_part)
        )
        # Return tail (containing random characters) of name
        return name[-length:]

    def _tmt_name(self) -> str:
        """
        Generate a name prefixed with tmt run id
        """

        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        parent = cast(Provision, self.parent)

        assert parent.plan.my_run is not None  # narrow type
        assert parent.plan.my_run.workdir is not None  # narrow type
        run_id = parent.plan.my_run.workdir.name
        return self._random_name(prefix=f"tmt-{run_id[-3:]}-")

    @functools.cached_property
    def multihost_name(self) -> str:
        """
        Return guest's multihost name, i.e. name and its role
        """

        return format_guest_full_name(self.name, self.role)

    @property
    def is_ready(self) -> bool:
        """
        Detect guest is ready or not
        """

        raise NotImplementedError

    @functools.cached_property
    def package_manager(
        self,
    ) -> 'tmt.package_managers.PackageManager[tmt.package_managers.PackageManagerEngine]':
        if not self.facts.package_manager:
            raise tmt.utils.GeneralError(
                f"Package manager was not detected on guest '{self.name}'."
            )

        return tmt.package_managers.find_package_manager(self.facts.package_manager)(
            guest=self, logger=self._logger
        )

    @functools.cached_property
    def bootc_builder(
        self,
    ) -> 'tmt.package_managers.PackageManager[tmt.package_managers.PackageManagerEngine]':
        if not self.facts.bootc_builder:
            raise tmt.utils.GeneralError(f"Bootc builder was not detected on guest '{self.name}'.")

        return tmt.package_managers.find_package_manager(self.facts.bootc_builder)(
            guest=self, logger=self._logger
        )

    @functools.cached_property
    def scripts_path(self) -> Path:
        """
        Absolute path to tmt scripts directory
        """

        # For rpm-ostree based distributions use a different default destination directory
        return tmt.steps.scripts.effective_scripts_dest_dir(
            default=tmt.steps.scripts.DEFAULT_SCRIPTS_DEST_DIR_OSTREE
            if self.facts.is_ostree
            else tmt.steps.scripts.DEFAULT_SCRIPTS_DEST_DIR
        )

    @classmethod
    def options(cls, how: Optional[str] = None) -> list[tmt.options.ClickOptionDecoratorType]:
        """
        Prepare command line options related to guests
        """

        return []

    def load(self, data: GuestData) -> None:
        """
        Load guest data into object attributes for easy access

        Called during guest object initialization. Takes care of storing
        all supported keys (see class attribute _keys for the list) from
        provided data to the guest object attributes. Child classes can
        extend it to make additional guest attributes easily available.

        Data dictionary can contain guest information from both command
        line options / L2 metadata / user configuration and wake up data
        stored by the save() method below.
        """

        data.inject_to(self)

    def save(self) -> GuestData:
        """
        Save guest data for future wake up

        Export all essential guest data into a dictionary which will be
        stored in the `guests.yaml` file for possible future wake up of
        the guest. Everything needed to attach to a running instance
        should be added into the data dictionary by child classes.
        """

        return self.get_data_class().extract_from(self)

    def wake(self) -> None:
        """
        Wake up the guest

        Perform any actions necessary after step wake up to be able to
        attach to a running guest instance and execute commands. Called
        after load() is completed so all guest data should be prepared.
        """

        self.debug(f"Doing nothing to wake up guest '{self.primary_address}'.")

    def suspend(self) -> None:
        """
        Suspend the guest.

        Perform any actions necessary before quitting step and tmt. The
        guest may be reused by future tmt invocations.
        """

        self.debug(f"Suspending guest '{self.name}'.")

    def start(self) -> None:
        """
        Start the guest

        Get a new guest instance running. This should include preparing
        any configuration necessary to get it started. Called after
        load() is completed so all guest data should be available.
        """

        self.debug(f"Doing nothing to start guest '{self.primary_address}'.")

    def install_scripts(self, scripts: Sequence[tmt.steps.scripts.Script]) -> None:
        """
        Install scripts required by tmt.
        """

        # Make sure scripts directory exists
        command = Command("mkdir", "-p", f"{self.scripts_path}")

        if not self.facts.is_superuser:
            command = Command("sudo") + command

        self.execute(command)

        # Install all scripts on guest
        for script in scripts:
            if not script.enabled(self):
                continue

            with script as source:
                for filename in [script.source_filename, *script.aliases]:
                    self.push(
                        source=source,
                        destination=script.destination_path or self.scripts_path / filename,
                        options=["-p", "--chmod=755"],
                        superuser=self.facts.is_superuser is not True,
                    )

    def setup(self) -> None:
        """
        Setup the guest

        Setup the guest after it has been started. It is called after :py:meth:`Guest.start`.
        """

        self.install_scripts(tmt.steps.scripts.SCRIPTS)

    # A couple of requiremens for this field:
    #
    # * it should be valid, i.e. when someone tries to access it, the values
    #   should be there.
    # * it should be serializable so we can save & load it, to save time when
    #   using the guest once again.
    #
    # Note that the facts container, `GuestFacts`, is already provided to us,
    # in `GuestData` package given to `Guest.__init__()`, and it's saved in
    # our `__dict__`. It's just empty.
    #
    # A bit of Python magic then:
    #
    # * a property it is, it allows us to do some magic on access. Also,
    #   `guest.facts` is much better than `guest.data.facts`.
    # * property does not need to care about instantiation of the container,
    #   it just works with it.
    # * when accessed, property takes the facts container and starts the sync,
    #   if needed. This is probably going to happen just once, on the first
    #   access, unless something explicitly invalidates the facts.
    # * when loaded from `guests.yaml`, the container is unserialized and put
    #   directly into `__dict__`, like nothing has happened.
    @property
    def facts(self) -> GuestFacts:
        facts = cast(GuestFacts, self.__dict__['facts'])

        if not facts.in_sync:
            facts.sync(self)

        return facts

    @facts.setter
    def facts(self, facts: Union[GuestFacts, dict[str, Any]]) -> None:
        if isinstance(facts, GuestFacts):
            self.__dict__['facts'] = facts

        else:
            self.__dict__['facts'] = GuestFacts.from_serialized(facts)

    def show(self, show_multihost_name: bool = True) -> None:
        """
        Show guest details such as distro and kernel
        """

        if show_multihost_name:
            self.info('multihost name', self.multihost_name, color='green')

        # Skip active checks in dry mode
        if self.is_dry_run:
            return

        if not self.is_ready:
            return

        for key, key_formatted, value_formatted in self.facts.format():
            if key in GUEST_FACTS_INFO_FIELDS:
                self.info(key_formatted, value_formatted, color='green')

            elif key in GUEST_FACTS_VERBOSE_FIELDS:
                self.verbose(key_formatted, value_formatted, color='green')

    def _ansible_verbosity(self) -> list[str]:
        """
        Prepare verbose level based on the --debug option count
        """

        if self.debug_level < 3:
            return []
        return ['-' + (self.debug_level - 2) * 'v']

    @staticmethod
    def _ansible_extra_args(extra_args: Optional[str]) -> list[str]:
        """
        Prepare extra arguments for ansible-playbook
        """

        if extra_args is None:
            return []
        return shlex.split(str(extra_args))

    def _ansible_summary(self, output: Optional[str]) -> None:
        """
        Check the output for ansible result summary numbers
        """

        if not output:
            return
        keys = ['ok', 'changed', 'unreachable', 'failed', 'skipped', 'rescued', 'ignored']
        for key in keys:
            matched = re.search(rf'^.*\s:\s.*{key}=(\d+).*$', output, re.MULTILINE)
            if matched and int(matched.group(1)) > 0:
                tasks = fmf.utils.listed(matched.group(1), 'task')
                self.verbose(key, tasks, 'green')

    def _sanitize_ansible_playbook_path(
        self, playbook: AnsibleApplicable, playbook_root: Optional[Path]
    ) -> AnsibleApplicable:
        """
        Prepare full ansible playbook path.

        :param playbook: path to the playbook to run.
        :param playbook_root: if set, ``playbook`` path must be located
            under the given root path.
        :returns: an absolute path to a playbook.
        :raises GeneralError: when ``playbook_root`` is set, but
            ``playbook`` is not located in this filesystem tree, or when
            the eventual playbook path is not absolute.
        """

        # Handle the individual types under the hood of `AnsibleApplicable`.
        # Note that `isinstance()` calls do not use our fancy names,
        # `AnsibleCollectionPlaybook` and `AnsiblePlaybook`. These are
        # extremely helpful to type checkers, but Python interpreter
        # sees only the aliased types, `Path` and `str`.

        # First, a path:
        if isinstance(playbook, Path):
            # Some playbooks must be under playbook root, which is often
            # a metadata tree root.
            if playbook_root is not None:
                playbook = playbook_root / playbook.unrooted()

                if not playbook.is_relative_to(playbook_root):
                    raise tmt.utils.GeneralError(
                        f"'{playbook}' is not relative to the expected root '{playbook_root}'."
                    )

            if not playbook.exists():
                raise tmt.utils.FileError(f"Playbook '{playbook}' does not exist.")

            self.debug(f"Playbook full path: '{playbook}'", level=2)

            return playbook

        # Second, a collection playbook:
        if isinstance(playbook, str):
            self.debug(f"Collection playbook: '{playbook}'", level=2)

            return playbook

        raise GeneralError(f"Unknown Ansible object type, '{type(playbook)}'.")

    def _prepare_environment(
        self, execute_environment: Optional[tmt.utils.Environment] = None
    ) -> tmt.utils.Environment:
        """
        Prepare dict of environment variables
        """
        # Prepare environment variables so they can be correctly passed
        # to shell. Create a copy to prevent modifying source.
        environment = tmt.utils.Environment()
        environment.update(execute_environment or {})
        # Plan environment and variables provided on the command line
        # override environment provided to execute().
        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        if self.parent:
            parent = cast(Provision, self.parent)
            environment.update(parent.plan.environment)
        return environment

    @staticmethod
    def _export_environment(environment: tmt.utils.Environment) -> list[ShellScript]:
        """
        Prepare shell export of environment variables
        """

        if not environment:
            return []
        return [
            ShellScript(f'export {variable}')
            for variable in tmt.utils.shell_variables(environment)
        ]

    def _run_guest_command(
        self,
        command: Command,
        friendly_command: Optional[str] = None,
        silent: bool = False,
        cwd: Optional[Path] = None,
        env: Optional[tmt.utils.Environment] = None,
        interactive: bool = False,
        log: Optional[tmt.log.LoggingFunction] = None,
        **kwargs: Any,
    ) -> tmt.utils.CommandOutput:
        """
        Run a command, local or remote, related to the guest.

        A rather thin wrapper of :py:meth:`run` whose purpose is to be a single
        point through all commands related to a guest must go through. We expect
        consistent logging from such commands, be it an ``ansible-playbook``
        running on the control host or a test script on the guest.

        :param command: a command to execute.
        :param friendly_command: if set, it would be logged instead of the
            command itself, to improve visibility of the command in logging output.
        :param silent: if set, logging of steps taken by this function would be
            reduced.
        :param cwd: if set, command would be executed in the given directory,
            otherwise the current working directory is used.
        :param env: environment variables to combine with the current environment
            before running the command.
        :param interactive: if set, the command would be executed in an interactive
            manner, i.e. with stdout and stdout connected to terminal for live
            interaction with user.
        :param log: a logging function to use for logging of command output. By
            default, ``self._logger.debug`` is used.
        :returns: command output, bundled in a :py:class:`CommandOutput` tuple.
        """

        if friendly_command is None:
            friendly_command = str(command)

        return self.run(
            command,
            friendly_command=friendly_command,
            silent=silent,
            cwd=cwd,
            env=env,
            interactive=interactive,
            log=log if log else self._command_verbose_logger,
            **kwargs,
        )

    def _run_ansible(
        self,
        playbook: AnsibleApplicable,
        playbook_root: Optional[Path] = None,
        extra_args: Optional[str] = None,
        friendly_command: Optional[str] = None,
        log: Optional[tmt.log.LoggingFunction] = None,
        silent: bool = False,
    ) -> tmt.utils.CommandOutput:
        """
        Run an Ansible playbook on the guest.

        This is a main workhorse for :py:meth:`ansible`. It shall run the
        playbook in whatever way is fitting for the guest and infrastructure.

        :param playbook: path to the playbook to run.
        :param playbook_root: if set, ``playbook`` path must be located
            under the given root path.
        :param extra_args: additional arguments to be passed to ``ansible-playbook``
            via ``--extra-args``.
        :param friendly_command: if set, it would be logged instead of the
            command itself, to improve visibility of the command in logging output.
        :param log: a logging function to use for logging of command output. By
            default, ``logger.debug`` is used.
        :param silent: if set, logging of steps taken by this function would be
            reduced.
        """

        raise NotImplementedError

    def ansible(
        self,
        playbook: AnsibleApplicable,
        playbook_root: Optional[Path] = None,
        extra_args: Optional[str] = None,
        friendly_command: Optional[str] = None,
        log: Optional[tmt.log.LoggingFunction] = None,
        silent: bool = False,
    ) -> tmt.utils.CommandOutput:
        """
        Run an Ansible playbook on the guest.

        A wrapper for :py:meth:`_run_ansible` which is responsible for running
        the playbook while this method makes sure our logging is consistent.

        :param playbook: path to the playbook to run.
        :param playbook_root: if set, ``playbook`` path must be located
            under the given root path.
        :param extra_args: additional arguments to be passed to ``ansible-playbook``
            via ``--extra-args``.
        :param friendly_command: if set, it would be logged instead of the
            command itself, to improve visibility of the command in logging output.
        :param log: a logging function to use for logging of command output. By
            default, ``logger.debug`` is used.
        :param silent: if set, logging of steps taken by this function would be
            reduced.
        """

        output = self._run_ansible(
            playbook,
            playbook_root=playbook_root,
            extra_args=extra_args,
            friendly_command=friendly_command,
            log=log if log else self._command_verbose_logger,
            silent=silent,
        )

        self._ansible_summary(output.stdout)

        return output

    @overload
    def execute(
        self,
        command: tmt.utils.ShellScript,
        cwd: Optional[Path] = None,
        env: Optional[tmt.utils.Environment] = None,
        friendly_command: Optional[str] = None,
        test_session: bool = False,
        tty: bool = False,
        silent: bool = False,
        log: Optional[tmt.log.LoggingFunction] = None,
        interactive: bool = False,
        on_process_start: Optional[OnProcessStartCallback] = None,
        on_process_end: Optional[OnProcessEndCallback] = None,
        **kwargs: Any,
    ) -> tmt.utils.CommandOutput:
        pass

    @overload
    def execute(
        self,
        command: tmt.utils.Command,
        cwd: Optional[Path] = None,
        env: Optional[tmt.utils.Environment] = None,
        friendly_command: Optional[str] = None,
        test_session: bool = False,
        tty: bool = False,
        silent: bool = False,
        log: Optional[tmt.log.LoggingFunction] = None,
        interactive: bool = False,
        on_process_start: Optional[OnProcessStartCallback] = None,
        on_process_end: Optional[OnProcessEndCallback] = None,
        **kwargs: Any,
    ) -> tmt.utils.CommandOutput:
        pass

    def execute(
        self,
        command: Union[tmt.utils.Command, tmt.utils.ShellScript],
        cwd: Optional[Path] = None,
        env: Optional[tmt.utils.Environment] = None,
        friendly_command: Optional[str] = None,
        test_session: bool = False,
        tty: bool = False,
        silent: bool = False,
        log: Optional[tmt.log.LoggingFunction] = None,
        interactive: bool = False,
        on_process_start: Optional[OnProcessStartCallback] = None,
        on_process_end: Optional[OnProcessEndCallback] = None,
        **kwargs: Any,
    ) -> tmt.utils.CommandOutput:
        """
        Execute a command on the guest.

        :param command: either a command or a shell script to execute.
        :param cwd: if set, execute command in this directory on the guest.
        :param env: if set, set these environment variables before running the command.
        :param friendly_command: nice, human-friendly representation of the command.
        """

        raise NotImplementedError

    def push(
        self,
        source: Optional[Path] = None,
        destination: Optional[Path] = None,
        options: Optional[list[str]] = None,
        superuser: bool = False,
    ) -> None:
        """
        Push files to the guest
        """

        raise NotImplementedError

    def pull(
        self,
        source: Optional[Path] = None,
        destination: Optional[Path] = None,
        options: Optional[list[str]] = None,
        extend_options: Optional[list[str]] = None,
    ) -> None:
        """
        Pull files from the guest
        """

        raise NotImplementedError

    def stop(self) -> None:
        """
        Stop the guest

        Shut down a running guest instance so that it does not consume
        any memory or cpu resources. If needed, perform any actions
        necessary to store the instance status to disk.
        """

        raise NotImplementedError

    @overload
    def reboot(
        self,
        hard: Literal[True] = True,
        command: None = None,
        waiting: Optional[Waiting] = None,
    ) -> bool:
        pass

    @overload
    def reboot(
        self,
        hard: Literal[False] = False,
        command: Optional[Union[Command, ShellScript]] = None,
        waiting: Optional[Waiting] = None,
    ) -> bool:
        pass

    def reboot(
        self,
        hard: bool = False,
        command: Optional[Union[Command, ShellScript]] = None,
        waiting: Optional[Waiting] = None,
    ) -> bool:
        """
        Reboot the guest, and wait for the guest to recover.

        .. note::

           Custom reboot command can be used only in combination with a
           soft reboot. If both ``hard`` and ``command`` are set, a hard
           reboot will be requested, and ``command`` will be ignored.

        :param hard: if set, force the reboot. This may result in a loss
            of data. The default of ``False`` will attempt a graceful
            reboot.
        :param command: a command to run on the guest to trigger the
            reboot. If ``hard`` is also set, ``command`` is ignored.
        :param timeout: amount of time in which the guest must become available
            again.
        :param tick: how many seconds to wait between two consecutive attempts
            of contacting the guest.
        :param tick_increase: a multiplier applied to ``tick`` after every
            attempt.
        :returns: ``True`` if the reboot succeeded, ``False`` otherwise.
        """

        raise NotImplementedError

    def reconnect(
        self,
        wait: Optional[Waiting] = None,
    ) -> bool:
        """
        Ensure the connection to the guest is working

        The default timeout is 5 minutes. Custom number of seconds can be
        provided in the `timeout` parameter. This may be useful when long
        operations (such as system upgrade) are performed.
        """

        wait = wait or default_reconnect_waiting()

        self.debug("Wait for a connection to the guest.")

        def try_whoami() -> None:
            try:
                self.execute(Command('whoami'), silent=True)

            except tmt.utils.RunError:
                raise tmt.utils.wait.WaitingIncompleteError

        try:
            wait.wait(try_whoami, self._logger)

        except tmt.utils.wait.WaitingTimedOutError:
            self.debug("Connection to guest failed after reboot.")
            return False

        return True

    def remove(self) -> None:
        """
        Remove the guest

        Completely remove all guest instance data so that it does not
        consume any disk resources.
        """

        self.debug(f"Doing nothing to remove guest '{self.primary_address}'.")

    def _check_rsync(self) -> CheckRsyncOutcome:
        """
        Make sure that rsync is installed on the guest

        On read-only distros install it under the '/root/pkg' directory.
        Returns 'already installed' when rsync is already present.
        """

        # Check for rsync (nothing to do if already installed)
        self.debug("Ensure that rsync is installed on the guest.")
        try:
            self.execute(Command('rsync', '--version'))
            return CheckRsyncOutcome.ALREADY_INSTALLED
        except tmt.utils.RunError:
            pass

        self.package_manager.install(Package('rsync'))

        return CheckRsyncOutcome.INSTALLED

    @classmethod
    def essential_requires(cls) -> list['tmt.base.Dependency']:
        """
        Collect all essential requirements of the guest.

        Essential requirements of a guest are necessary for the guest to be
        usable for testing.

        :returns: a list of requirements.
        """

        return []

    @property
    def logdir(self) -> Optional[Path]:
        """
        Path to store logs

        Create the directory if it does not exist yet.
        """

        if not self.workdir:
            return None

        dirpath = self.workdir / 'logs'
        dirpath.mkdir(parents=True, exist_ok=True)

        return dirpath

    def fetch_logs(
        self,
        logger: tmt.log.Logger,
        dirpath: Optional[Path] = None,
        guest_logs: Optional[list[GuestLog]] = None,
    ) -> None:
        """
        Get log content and save it to a directory.

        :param logger: logger to use for logging.
        :param dirpath: a directory to save into. If not set, :py:attr:`logdir`,
            or current working directory will be used.
        :param guest_logs: optional list of :py:attr:`GuestLog`. If not set,
            all guest logs from :py:attr:`Guest.guest_logs` would be collected.
        """

        guest_logs = guest_logs or self.guest_logs

        dirpath = dirpath or self.logdir or Path.cwd()
        dirpath.mkdir(parents=True, exist_ok=True)
        for log in guest_logs:
            log.store(logger, dirpath, log.name)

    def _construct_mkdtemp_command(
        self,
        prefix: Optional[str] = None,
        template: Optional[str] = None,
        parent: Optional[Path] = None,
    ) -> Command:
        template = template or 'tmp.XXXXXXXXXX'

        if prefix is not None:
            template = f'{prefix}{template}'

        options: list[str] = ['--directory']

        if parent is not None:
            options += ['-p', str(parent)]

        return Command(*('mktemp', *options, template))

    @contextlib.contextmanager
    def mkdtemp(
        self,
        # Suffix is not supported everywhere, namely Alpine does not
        # recognize it, and even requires template to end with `XXXXXX`.
        # Therefore not supporting this option - in the future, someone
        # may need it, fix it for all distros, and uncomment the
        # parameter.
        # suffix: Optional[str] = None,
        prefix: Optional[str] = None,
        template: Optional[str] = None,
        parent: Optional[Path] = None,
    ) -> Iterator[Path]:
        """
        Create a temporary directory.

        Modeled after :py:func:`tempfile.mkdtemp`, but creates the
        temporary directory on the guest, by invoking ``mktemp -d``. The
        implementation may slightly differ, but the temporary directory
        should be created safely, without conflicts, and it should be
        accessible only to user who created it.

        Since the caller is responsible for removing the directory, it
        is recommended to use it as a context manager, just as one would
        use :py:func:`tempfile.mkdtemp`; leaving the context will remove
        the directory:

        .. code-block:: python

            with guest.mkdtemp() as path:
                ...

        :param prefix: if set, the directory name will begin with this
            string.
        :param template: if set, the directory name will follow the
            given naming scheme: the template must end with 6
            consecutive ``X``s, i.e. ``XXXXXX``. All ``X`` letters will
            be replaced with random characters.
        :param parent: if set, new directory will be created under this
            path. Otherwise, the default directory is used.
        """

        output = self.execute(
            self._construct_mkdtemp_command(prefix=prefix, template=template, parent=parent)
        )

        if not output.stdout:
            raise GeneralError(f"Failed to create temporary directory on guest: {output.stderr}")

        path = Path(output.stdout.strip())

        try:
            yield path

        except Exception as exc:
            raise exc

        else:
            self.execute(Command('rm', '-rf', path))


@container
class GuestSshData(GuestData):
    """
    Keys necessary to describe, create, save and restore a guest with SSH
    capability.

    Derived from GuestData, this class adds keys relevant for guests that can be
    reached over SSH.
    """

    port: Optional[int] = field(
        default=None,
        option=('-P', '--port'),
        metavar='PORT',
        help="""
             Port to use for SSH connections instead of the default
             one.
             """,
        normalize=tmt.utils.normalize_optional_int,
    )
    user: str = field(
        default=DEFAULT_USER,
        option=('-u', '--user'),
        metavar='NAME',
        help='A username to use for all guest operations.',
    )
    key: list[Path] = field(
        default_factory=list,
        option=('-k', '--key'),
        metavar='PATH',
        help="""
             Private key to use as SSH identity for key-based
             authentication.
             """,
        normalize=tmt.utils.normalize_path_list,
    )
    password: Optional[str] = field(
        default=None,
        option=('-p', '--password'),
        metavar='PASSWORD',
        help="""
             Password to use for password-based authentication.
             """,
    )
    ssh_option: list[str] = field(
        default_factory=list,
        option='--ssh-option',
        metavar="OPTION",
        multiple=True,
        help="""
             Additional SSH option. Value is passed to the ``-o``
             option of ``ssh``, see ``ssh_config(5)`` for supported
             options. Can be specified multiple times.
             """,
        normalize=tmt.utils.normalize_string_list,
    )


class GuestSsh(Guest):
    """
    Guest provisioned for test execution, capable of accepting SSH connections

    The following keys are expected in the 'data' dictionary::

        role ....... guest role in the multihost scenario (inherited)
        guest ...... hostname or ip address (inherited)
        become ..... run shell scripts in tests, prepare, and finish with sudo (inherited)
        port ....... port to connect to
        user ....... user name to log in
        key ........ path to the private key (str or list)
        password ... password

    These are by default imported into instance attributes.
    """

    _data_class: type[GuestData] = GuestSshData

    port: Optional[int]
    user: Optional[str]
    key: list[Path]
    password: Optional[str]
    ssh_option: list[str]

    # Master ssh connection process and socket path
    _ssh_master_process_lock: threading.Lock
    _ssh_master_process: Optional['subprocess.Popen[bytes]'] = None

    def __init__(
        self,
        *,
        data: GuestData,
        name: Optional[str] = None,
        parent: Optional[tmt.utils.Common] = None,
        logger: tmt.log.Logger,
    ) -> None:
        self._ssh_master_process_lock = threading.Lock()

        super().__init__(data=data, logger=logger, parent=parent, name=name)

    @functools.cached_property
    def _ssh_guest(self) -> str:
        """
        Return user@guest
        """

        return f'{self.user}@{self.primary_address}'

    @functools.cached_property
    def _is_ssh_master_socket_path_acceptable(self) -> bool:
        """
        Whether the SSH master socket path we create is acceptable by SSH
        """

        if len(str(self._ssh_master_socket_path)) >= SSH_MASTER_SOCKET_LENGTH_LIMIT:
            self.warn(
                "SSH multiplexing will not be used because the SSH master socket path "
                f"'{self._ssh_master_socket_path}' is too long."
            )
            return False

        return True

    @property
    def is_ssh_multiplexing_enabled(self) -> bool:
        """
        Whether SSH multiplexing should be used
        """

        if self.primary_address is None:
            return False

        if not self._is_ssh_master_socket_path_acceptable:
            return False

        return True

    @functools.cached_property
    def _ssh_master_socket_path(self) -> Path:
        """
        Return path to the SSH master socket
        """

        # Can be any step opening the connection
        assert isinstance(self.parent, tmt.steps.Step)
        assert self.parent.plan.my_run is not None
        assert self.parent.plan.my_run.workdir is not None

        socket_dir = self.parent.plan.my_run.workdir / 'ssh-sockets'

        try:
            socket_dir.mkdir(parents=True, exist_ok=True)

        except Exception as exc:
            raise ProvisionError(f"Failed to create SSH socket directory '{socket_dir}'.") from exc

        # Try more informative, but possibly too long path, constructed
        # from pieces humans can easily understand and follow.
        #
        # The template is what seems to be a common template in general
        # SSH discussions, hostname, port, username. Can we use guest
        # name? Maybe, on the other hand, guest name is meaningless
        # outside of its plan, it might be too ambiguous. Starting with
        # what SSH folk uses, we may amend it later.

        # This should be true, otherwise `is_ssh_multiplexing_enabled` would return `False`
        # and nobody would need to use SSH master socket path.
        assert self.primary_address

        guest_id_components: list[str] = [self.primary_address]

        if self.port:
            guest_id_components.append(str(self.port))

        if self.user:
            guest_id_components.append(self.user)

        guest_id = '-'.join(guest_id_components)

        socket_path = _socket_path_trivial(
            socket_dir=socket_dir, guest_id=guest_id, logger=self._logger
        )

        if socket_path is not None:
            self.debug(
                f"SSH master socket path will be '{socket_path}' (trivial method).", level=4
            )

            return socket_path

        # The readable name was too long. Try different approach: use
        # a hash of the pieces, and use just a substring of the hash,
        # not all 64 or whatever characters. If the substring is already
        # in use - extremely unlikely, yet possible - try a slightly
        # longer one.
        socket_path = _socket_path_hash(
            socket_dir=socket_dir, guest_id=guest_id, logger=self._logger
        )

        if socket_path is not None:
            self.debug(f"SSH master socket path will be '{socket_path}' (hash method).", level=4)

            return socket_path

        # Not even the hashing function and short substrings helped.
        # Return the most readable one, and let caller decide whether
        # they use it or not. We run out of options.
        socket_path = _socket_path_trivial(
            socket_dir=socket_dir, guest_id=guest_id, limit_size=False, logger=self._logger
        )

        self.debug(
            f"SSH master socket path will be '{socket_path}' (trivial method, no size limit).",
            level=4,
        )

        return socket_path

    @functools.cached_property
    def _ssh_master_socket_reservation_path(self) -> Path:
        return Path(f'{self._ssh_master_socket_path}.reservation')

    @property
    def _ssh_options(self) -> Command:
        """
        Return common SSH options
        """

        options = BASE_SSH_OPTIONS[:]

        if self.key or self.password:
            # Skip ssh-agent (it adds additional identities)
            options.append('-oIdentitiesOnly=yes')
        if self.port:
            options.append(f'-p{self.port}')
        if self.key:
            for key in self.key:
                options.extend(['-i', key])
        if self.password:
            options.extend(['-oPasswordAuthentication=yes'])
        else:
            # Make sure the connection is rejected when we want key-
            # based authentication only instead of presenting a prompt.
            # Prevents issues like https://github.com/teemtee/tmt/issues/2687
            # from happening and makes the ssh connection more robust
            # by allowing proper re-try mechanisms to kick-in.
            options.extend(['-oPasswordAuthentication=no'])

        # Include the SSH master process
        if self.is_ssh_multiplexing_enabled:
            options.append(f'-S{self._ssh_master_socket_path}')

        options.extend([f'-o{option}' for option in self.ssh_option])

        return Command(*options)

    @property
    def _base_ssh_command(self) -> Command:
        """
        A base SSH command shared by all SSH processes
        """

        command = Command(*(["sshpass", "-p", self.password] if self.password else []), "ssh")

        return command + self._ssh_options

    def _spawn_ssh_master_process(self) -> subprocess.Popen[bytes]:
        """
        Spawn the SSH master process
        """

        # NOTE: do not modify `command`, it might be reused by the caller. To
        # be safe, include it in our own command.
        ssh_master_command = (
            self._base_ssh_command + self._ssh_options + Command("-MNnT", self._ssh_guest)
        )

        self.debug(f"Spawning the SSH master process: {ssh_master_command}")

        return subprocess.Popen(
            ssh_master_command.to_popen(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _cleanup_ssh_master_process(
        self, signal: _signal.Signals = _signal.SIGTERM, logger: Optional[tmt.log.Logger] = None
    ) -> None:
        logger = logger or self._logger

        if not self.is_ssh_multiplexing_enabled:
            logger.debug(
                'The SSH master process cannot be terminated because it is disabled.', level=3
            )

            return

        with self._ssh_master_process_lock:
            if self._ssh_master_process is None:
                logger.debug(
                    'The SSH master process cannot be terminated because it is unset.', level=3
                )

                return

            logger.debug(
                f'Terminating the SSH master process {self._ssh_master_process.pid}'
                f' with {signal.name}.',
                level=3,
            )

            self._ssh_master_process.send_signal(signal)

            try:
                # TODO: make the deadline configurable
                self._ssh_master_process.wait(timeout=3)

            except subprocess.TimeoutExpired:
                logger.warning(
                    f'Terminating the SSH master process {self._ssh_master_process.pid} timed out.'
                )

            self._ssh_master_process = None

    @property
    def _ssh_command(self) -> Command:
        """
        A base SSH command shared by all SSH processes
        """

        if self.is_ssh_multiplexing_enabled:
            with self._ssh_master_process_lock:
                if self._ssh_master_process is None:
                    self._ssh_master_process = self._spawn_ssh_master_process()

        return self._base_ssh_command

    def _unlink_ssh_master_socket_path(self) -> None:
        if not self.is_ssh_multiplexing_enabled:
            return

        with self._ssh_master_process_lock:
            if not self._ssh_master_socket_path:
                return

            self.debug(f"Remove SSH master socket '{self._ssh_master_socket_path}'.", level=3)

            try:
                self._ssh_master_socket_path.unlink(missing_ok=True)
                self._ssh_master_socket_reservation_path.unlink(missing_ok=True)

            except OSError as error:
                self.debug(f"Failed to remove the SSH master socket: {error}", level=3)

            del self._ssh_master_socket_path
            del self._ssh_master_socket_reservation_path

    def _run_ansible(
        self,
        playbook: AnsibleApplicable,
        playbook_root: Optional[Path] = None,
        extra_args: Optional[str] = None,
        friendly_command: Optional[str] = None,
        log: Optional[tmt.log.LoggingFunction] = None,
        silent: bool = False,
    ) -> tmt.utils.CommandOutput:
        """
        Run an Ansible playbook on the guest.

        This is a main workhorse for :py:meth:`ansible`. It shall run the
        playbook in whatever way is fitting for the guest and infrastructure.

        :param playbook: path to the playbook to run.
        :param playbook_root: if set, ``playbook`` path must be located
            under the given root path.
        :param extra_args: additional arguments to be passed to ``ansible-playbook``
            via ``--extra-args``.
        :param friendly_command: if set, it would be logged instead of the
            command itself, to improve visibility of the command in logging output.
        :param log: a logging function to use for logging of command output. By
            default, ``logger.debug`` is used.
        :param silent: if set, logging of steps taken by this function would be
            reduced.
        """

        playbook = self._sanitize_ansible_playbook_path(playbook, playbook_root)

        ansible_command = Command('ansible-playbook', *self._ansible_verbosity())

        if extra_args:
            ansible_command += self._ansible_extra_args(extra_args)

        ansible_command += Command(
            '--ssh-common-args',
            self._ssh_options.to_element(),
            '-i',
            f'{self._ssh_guest},',
            playbook,
        )

        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        parent = cast(Provision, self.parent)

        try:
            return self._run_guest_command(
                ansible_command,
                friendly_command=friendly_command,
                silent=silent,
                cwd=parent.plan.worktree,
                env=self._prepare_environment(),
                log=log,
            )
        except tmt.utils.RunError as exc:
            hint = get_hint('ansible-not-available', ignore_missing=False)

            if hint.search_cli_patterns(exc.stderr, exc.stdout, exc.message):
                hint.print(self._logger)

            raise exc

    @property
    def is_ready(self) -> bool:
        """
        Detect guest is ready or not
        """

        # Enough for now, ssh connection can be created later
        return self.primary_address is not None

    def setup(self) -> None:
        super().setup()

        if self.is_dry_run:
            return
        if not self.facts.is_superuser and self.become:
            self.package_manager.install(FileSystemPath('/usr/bin/setfacl'))
            workdir_root = effective_workdir_root()
            self.execute(
                ShellScript(
                    f"""
                    mkdir -p {workdir_root};
                    setfacl -d -m o:rX {workdir_root}
                    """
                )
            )

    def execute(
        self,
        command: Union[tmt.utils.Command, tmt.utils.ShellScript],
        cwd: Optional[Path] = None,
        env: Optional[tmt.utils.Environment] = None,
        friendly_command: Optional[str] = None,
        test_session: bool = False,
        tty: bool = False,
        silent: bool = False,
        log: Optional[tmt.log.LoggingFunction] = None,
        interactive: bool = False,
        on_process_start: Optional[OnProcessStartCallback] = None,
        on_process_end: Optional[OnProcessEndCallback] = None,
        **kwargs: Any,
    ) -> tmt.utils.CommandOutput:
        """
        Execute a command on the guest.

        :param command: either a command or a shell script to execute.
        :param cwd: execute command in this directory on the guest.
        :param env: if set, set these environment variables before running the command.
        :param friendly_command: nice, human-friendly representation of the command.
        """

        # Abort if guest is unavailable
        if self.primary_address is None and not self.is_dry_run:
            raise tmt.utils.GeneralError('The guest is not available.')

        ssh_command: tmt.utils.Command = self._ssh_command

        # Run in interactive mode if requested
        if interactive:
            ssh_command += Command('-t')

        # Force ssh to allocate pseudo-terminal if requested. Without a pseudo-terminal,
        # remote processes spawned by SSH would keep running after SSH process death, e.g.
        # in the case of a timeout.
        #
        # Note that polite request, `-t`, is not enough since `ssh` itself has no pseudo-terminal,
        # and a single `-t` wouldn't have the necessary effect.
        if test_session or tty:
            ssh_command += Command('-tt')

        # Accumulate all necessary commands - they will form a "shell" script, a single
        # string passed to SSH to execute on the remote machine.
        remote_commands: ShellScript = ShellScript.from_scripts(
            self._export_environment(self._prepare_environment(env))
        )

        # Change to given directory on guest if cwd provided
        if cwd:
            remote_commands += ShellScript(f'cd {quote(str(cwd))}')

        if isinstance(command, Command):
            remote_commands += command.to_script()

        else:
            remote_commands += command

        remote_command = remote_commands.to_element()

        ssh_command += [self._ssh_guest, remote_command]

        self.debug(f"Execute command '{remote_command}' on guest '{self.primary_address}'.")

        output = self._run_guest_command(
            ssh_command,
            log=log,
            friendly_command=friendly_command or str(command),
            silent=silent,
            cwd=cwd,
            interactive=interactive,
            on_process_start=on_process_start,
            on_process_end=on_process_end,
            **kwargs,
        )

        # Drop ssh connection closed messages, #2524
        if test_session and output.stdout:
            # Get last line index
            last_line_index = output.stdout.rfind(os.linesep, 0, -2)
            # Drop the connection closed message line, keep the ending lineseparator
            if (
                'Shared connection to ' in output.stdout[last_line_index:]
                or 'Connection to ' in output.stdout[last_line_index:]
            ):
                output = dataclasses.replace(
                    output, stdout=output.stdout[: last_line_index + len(os.linesep)]
                )

        return output

    def push(
        self,
        source: Optional[Path] = None,
        destination: Optional[Path] = None,
        options: Optional[list[str]] = None,
        superuser: bool = False,
    ) -> None:
        """
        Push files to the guest

        By default the whole plan workdir is synced to the same location
        on the guest. Use the 'source' and 'destination' to sync custom
        location and the 'options' parameter to modify default options
        which are '-Rrz --links --safe-links --delete'.

        Set 'superuser' if rsync command has to run as root or passwordless
        sudo on the Guest (e.g. pushing to r/o destination)
        """

        # Abort if guest is unavailable
        if self.primary_address is None and not self.is_dry_run:
            raise tmt.utils.GeneralError('The guest is not available.')

        # Prepare options and the push command
        options = options or DEFAULT_RSYNC_PUSH_OPTIONS
        if destination is None:
            destination = Path("/")
        if source is None:
            # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
            parent = cast(Provision, self.parent)

            assert parent.plan.workdir is not None

            source = parent.plan.workdir
            self.debug(f"Push workdir to guest '{self.primary_address}'.")
        else:
            self.debug(f"Copy '{source}' to '{destination}' on the guest.")

        def rsync() -> None:
            """
            Run the rsync command
            """

            # In closure, mypy has hard times to reason about the state of used variables.
            assert options
            assert source
            assert destination

            cmd = ['rsync']
            if superuser and self.user != 'root':
                cmd += ['--rsync-path', 'sudo rsync']

            self._run_guest_command(
                Command(
                    *cmd,
                    *options,
                    "-e",
                    self._ssh_command.to_element(),
                    source,
                    f"{self._ssh_guest}:{destination}",
                ),
                silent=True,
            )

        # Try to push twice, check for rsync after the first failure
        try:
            rsync()
        except tmt.utils.RunError:
            try:
                if self._check_rsync() == CheckRsyncOutcome.ALREADY_INSTALLED:
                    raise
                rsync()
            except tmt.utils.RunError:
                # Provide a reasonable error to the user
                self.fail(
                    f"Failed to push workdir to the guest. This usually means "
                    f"that login as '{self.user}' to the guest does not work."
                )
                raise

    def pull(
        self,
        source: Optional[Path] = None,
        destination: Optional[Path] = None,
        options: Optional[list[str]] = None,
        extend_options: Optional[list[str]] = None,
    ) -> None:
        """
        Pull files from the guest

        By default the whole plan workdir is synced from the same
        location on the guest. Use the 'source' and 'destination' to
        sync custom location, the 'options' parameter to modify
        default options :py:data:`DEFAULT_RSYNC_PULL_OPTIONS`
        and 'extend_options' to extend them (e.g. by exclude).
        """

        # Abort if guest is unavailable
        if self.primary_address is None and not self.is_dry_run:
            raise tmt.utils.GeneralError('The guest is not available.')

        # Prepare options and the pull command
        options = options or DEFAULT_RSYNC_PULL_OPTIONS
        if extend_options is not None:
            options.extend(extend_options)
        if destination is None:
            destination = Path("/")
        if source is None:
            # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
            parent = cast(Provision, self.parent)

            assert parent.plan.workdir is not None

            source = parent.plan.workdir
            self.debug(f"Pull workdir from guest '{self.primary_address}'.")
        else:
            self.debug(f"Copy '{source}' from the guest to '{destination}'.")

        def rsync() -> None:
            """
            Run the rsync command
            """

            # In closure, mypy has hard times to reason about the state of used variables.
            assert options
            assert source
            assert destination

            self._run_guest_command(
                Command(
                    "rsync",
                    *options,
                    "-e",
                    self._ssh_command.to_element(),
                    f"{self._ssh_guest}:{source}",
                    destination,
                ),
                silent=True,
            )

        # Try to pull twice, check for rsync after the first failure
        try:
            rsync()
        except tmt.utils.RunError:
            try:
                if self._check_rsync() == CheckRsyncOutcome.ALREADY_INSTALLED:
                    raise
                rsync()
            except tmt.utils.RunError:
                # Provide a reasonable error to the user
                self.fail(
                    f"Failed to pull workdir from the guest. "
                    f"This usually means that login as '{self.user}' "
                    f"to the guest does not work."
                )
                raise

    def suspend(self) -> None:
        """
        Suspend the guest.

        Perform any actions necessary before quitting step and tmt. The
        guest may be reused by future tmt invocations.
        """

        super().suspend()

        # Close the master ssh connection
        self._cleanup_ssh_master_process()

        # Remove the ssh socket
        self._unlink_ssh_master_socket_path()

    def stop(self) -> None:
        """
        Stop the guest

        Shut down a running guest instance so that it does not consume
        any memory or cpu resources. If needed, perform any actions
        necessary to store the instance status to disk.
        """

        self.suspend()

    def perform_reboot(
        self,
        action: Callable[[], Any],
        wait: Waiting,
        fetch_boot_time: bool = True,
    ) -> bool:
        """
        Perform the actual reboot and wait for the guest to recover.

        This is the core implementation of the common task of triggering
        a reboot and waiting for the guest to recover. :py:meth:`reboot`
        is the public API of guest classes, and feeds
        :py:meth:`perform_reboot` with the right ``action`` callable.

        :param action: a callable which will trigger the requested reboot.
        :param timeout: amount of time in which the guest must become available
            again.
        :param tick: how many seconds to wait between two consecutive attempts
            of contacting the guest.
        :param tick_increase: a multiplier applied to ``tick`` after every
            attempt.
        :param fetch_boot_time: if set, the current boot time of the
            guest would be read first, and used for testing whether the
            reboot has been performed. This will require communication
            with the guest, therefore it is recommended to use ``False``
            with hard reboot of unhealthy guests.
        :returns: ``True`` if the reboot succeeded, ``False`` otherwise.
        """

        def get_boot_time() -> int:
            """
            Reads btime from /proc/stat
            """

            stdout = self.execute(Command("cat", "/proc/stat")).stdout
            assert stdout

            match = STAT_BTIME_PATTERN.search(stdout)

            if match is None:
                raise tmt.utils.ProvisionError('Failed to retrieve boot time from guest')

            return int(match.group(1))

        current_boot_time = get_boot_time() if fetch_boot_time else 0

        self.debug(f"Triggering reboot with '{action}'.")

        try:
            action()

        except tmt.utils.RunError as error:
            # Connection can be closed by the remote host even before the
            # reboot command is completed. Let's ignore such errors.
            if error.returncode == 255:
                self.debug("Seems the connection was closed too fast, ignoring.")
            else:
                raise

        # Wait until we get new boot time, connection will drop and will be
        # unreachable for some time
        def check_boot_time() -> None:
            try:
                new_boot_time = get_boot_time()

                if new_boot_time != current_boot_time:
                    # Different boot time and we are reconnected
                    return

                # Same boot time, reboot didn't happen yet, retrying
                raise tmt.utils.wait.WaitingIncompleteError

            except tmt.utils.RunError:
                self.debug('Failed to connect to the guest.')
                raise tmt.utils.wait.WaitingIncompleteError

        try:
            wait.wait(check_boot_time, self._logger)

        except tmt.utils.wait.WaitingTimedOutError:
            self.debug("Connection to guest failed after reboot.")
            return False

        self.debug("Connection to guest succeeded after reboot.")
        return True

    @overload
    def reboot(
        self,
        hard: Literal[True] = True,
        command: None = None,
        waiting: Optional[Waiting] = None,
    ) -> bool:
        pass

    @overload
    def reboot(
        self,
        hard: Literal[False] = False,
        command: Optional[Union[Command, ShellScript]] = None,
        waiting: Optional[Waiting] = None,
    ) -> bool:
        pass

    def reboot(
        self,
        hard: bool = False,
        command: Optional[Union[Command, ShellScript]] = None,
        waiting: Optional[Waiting] = None,
    ) -> bool:
        """
        Reboot the guest, and wait for the guest to recover.

        .. note::

           Custom reboot command can be used only in combination with a
           soft reboot. If both ``hard`` and ``command`` are set, a hard
           reboot will be requested, and ``command`` will be ignored.

        :param hard: if set, force the reboot. This may result in a loss
            of data. The default of ``False`` will attempt a graceful
            reboot.
        :param command: a command to run on the guest to trigger the
            reboot. If ``hard`` is also set, ``command`` is ignored.
        :param timeout: amount of time in which the guest must become available
            again.
        :param tick: how many seconds to wait between two consecutive attempts
            of contacting the guest.
        :param tick_increase: a multiplier applied to ``tick`` after every
            attempt.
        :returns: ``True`` if the reboot succeeded, ``False`` otherwise.
        """

        if hard:
            raise tmt.utils.ProvisionError(
                f"Guest '{self.multihost_name}' does not support hard reboot."
            )

        command = command or tmt.steps.DEFAULT_REBOOT_COMMAND
        waiting = waiting or default_reboot_waiting()

        self.debug(f"Soft reboot using command '{command}'.")

        return self.perform_reboot(lambda: self.execute(command), waiting)

    def remove(self) -> None:
        """
        Remove the guest

        Completely remove all guest instance data so that it does not
        consume any disk resources.
        """

        self.debug(f"Doing nothing to remove guest '{self.primary_address}'.")


@container
class ProvisionStepData(tmt.steps.StepData):
    # guest role in the multihost scenario
    role: Optional[str] = None

    hardware: Optional[tmt.hardware.Hardware] = field(
        default=cast(Optional[tmt.hardware.Hardware], None),
        normalize=normalize_hardware,
        serialize=lambda hardware: hardware.to_spec() if hardware else None,
        unserialize=lambda serialized: tmt.hardware.Hardware.from_spec(serialized)
        if serialized is not None
        else None,
    )


ProvisionStepDataT = TypeVar('ProvisionStepDataT', bound=ProvisionStepData)


class ProvisionPlugin(tmt.steps.GuestlessPlugin[ProvisionStepDataT, None]):
    """
    Common parent of provision plugins
    """

    # ignore[assignment]: as a base class, ProvisionStepData is not included in
    # ProvisionStepDataT.
    _data_class = ProvisionStepData  # type: ignore[assignment]
    _guest_class = Guest

    #: If set, the plugin can be asked to provision in multiple threads at the
    #: same time. Plugins that do not support parallel provisioning should keep
    #: this set to ``False``.
    _thread_safe: bool = False

    # Default implementation for provision is a virtual machine
    how = 'virtual'

    # Methods ("how: ..." implementations) registered for the same step.
    _supported_methods: PluginRegistry[tmt.steps.Method] = PluginRegistry('step.provision')

    # TODO: Generics would provide a better type, https://github.com/teemtee/tmt/issues/1437
    _guest: Optional[Guest] = None

    @property
    def _preserved_workdir_members(self) -> set[str]:
        """
        A set of members of the step workdir that should not be removed.
        """

        return {*super()._preserved_workdir_members, "logs"}

    @classmethod
    def base_command(
        cls,
        usage: str,
        method_class: Optional[type[click.Command]] = None,
    ) -> click.Command:
        """
        Create base click command (common for all provision plugins)
        """

        # Prepare general usage message for the step
        if method_class:
            usage = Provision.usage(method_overview=usage)

        # Create the command
        @click.command(cls=method_class, help=usage)
        @click.pass_context
        @option('-h', '--how', metavar='METHOD', help='Use specified method for provisioning.')
        @tmt.steps.PHASE_OPTIONS
        def provision(context: 'tmt.cli.Context', **kwargs: Any) -> None:
            context.obj.steps.add('provision')
            Provision.store_cli_invocation(context)

        return provision

    def go(self, *, logger: Optional[tmt.log.Logger] = None) -> None:
        """
        Perform actions shared among plugins when beginning their tasks
        """

        self.go_prolog(logger or self._logger)

    # TODO: this might be needed until https://github.com/teemtee/tmt/issues/1696 is resolved
    def opt(self, option: str, default: Optional[Any] = None) -> Any:
        """
        Get an option from the command line options
        """

        if option == 'ssh-option':
            value = super().opt(option, default=default)

            if isinstance(value, tuple):
                return list(value)

            return value

        return super().opt(option, default=default)

    def wake(self, data: Optional[GuestData] = None) -> None:
        """
        Wake up the plugin

        Override data with command line options.
        Wake up the guest based on provided guest data.
        """

        super().wake()

        if data is not None:
            guest = self._guest_class(
                logger=self._logger, data=data, name=self.name, parent=self.step
            )
            guest.wake()
            self._guest = guest

    # TODO: getter. Like in Java. Do we need it?
    @property
    def guest(self) -> Optional[Guest]:
        """
        Return the provisioned guest.
        """

        return self._guest

    def essential_requires(self) -> list['tmt.base.Dependency']:
        """
        Collect all essential requirements of the guest implementation.

        Essential requirements of a guest are necessary for the guest to be
        usable for testing.

        By default, plugin's guest class, :py:attr:`ProvisionPlugin._guest_class`,
        is asked to provide the list of required packages via
        :py:meth:`Guest.requires` method.

        :returns: a list of requirements.
        """

        return self._guest_class.essential_requires()

    @classmethod
    def options(cls, how: Optional[str] = None) -> list[tmt.options.ClickOptionDecoratorType]:
        """
        Return list of options.
        """

        return super().options(how) + cls._guest_class.options(how)

    @classmethod
    def clean_images(cls, clean: 'tmt.base.Clean', dry: bool, workdir_root: Path) -> bool:
        """
        Remove the images of one particular plugin
        """

        return True

    def show(self, keys: Optional[list[str]] = None) -> None:
        keys = keys or list(set(self.data.keys()))

        show_hardware = 'hardware' in keys

        if show_hardware:
            keys.remove('hardware')

        super().show(keys=keys)

        if show_hardware:
            hardware: Optional[tmt.hardware.Hardware] = self.data.hardware

            if hardware:
                echo(tmt.utils.format('hardware', tmt.utils.dict_to_yaml(hardware.to_spec())))


@container
class ProvisionTask(tmt.queue.GuestlessTask[None]):
    """
    A task to run provisioning of multiple guests
    """

    #: Phases describing guests to provision. In the ``provision`` step,
    #: each phase describes one guest.
    phases: list[ProvisionPlugin[ProvisionStepData]]

    #: When ``ProvisionTask`` instance is received from the queue, ``phase``
    #: points to the phase that has been provisioned by the task.
    phase: Optional[ProvisionPlugin[ProvisionStepData]] = None

    @property
    def name(self) -> str:
        return cast(str, fmf.utils.listed([phase.name for phase in self.phases]))

    def go(self) -> Iterator['ProvisionTask']:
        multiple_guests = len(self.phases) > 1

        new_loggers = tmt.queue.prepare_loggers(self.logger, [phase.name for phase in self.phases])
        old_loggers: dict[str, Logger] = {}

        with ThreadPoolExecutor(max_workers=len(self.phases)) as executor:
            futures: dict[Future[None], ProvisionPlugin[ProvisionStepData]] = {}

            for phase in self.phases:
                old_loggers[phase.name] = phase._logger
                new_logger = new_loggers[phase.name]

                phase.inject_logger(new_logger)

                if multiple_guests:
                    new_logger.info('started', color='cyan')

                # Submit each phase as a distinct job for executor pool...
                futures[executor.submit(phase.go)] = phase

            # ... and then sit and wait as they get delivered to us as they
            # finish.
            for future in as_completed(futures):
                phase = futures[future]

                old_logger = old_loggers[phase.name]
                new_logger = new_loggers[phase.name]

                if multiple_guests:
                    new_logger.info('finished', color='cyan')

                # `Future.result()` will either 1. reraise an exception the
                # callable raised, if any, or 2. return whatever the callable
                # returned - which is `None` in our case, therefore we can
                # ignore the return value.
                try:
                    future.result()

                except SystemExit as exc:
                    yield ProvisionTask(
                        logger=new_logger,
                        result=None,
                        guest=phase.guest,
                        exc=None,
                        requested_exit=exc,
                        phases=[],
                    )

                except Exception as exc:
                    yield ProvisionTask(
                        logger=new_logger,
                        result=None,
                        guest=phase.guest,
                        exc=exc,
                        requested_exit=None,
                        phases=[],
                    )

                else:
                    yield ProvisionTask(
                        logger=new_logger,
                        result=None,
                        guest=phase.guest,
                        exc=None,
                        requested_exit=None,
                        phases=[],
                        phase=phase,
                    )

                # Don't forget to restore the original logger.
                phase.inject_logger(old_logger)


class ProvisionQueue(tmt.queue.Queue[ProvisionTask]):
    """
    Queue class for running provisioning tasks
    """

    def enqueue(self, *, phases: list[ProvisionPlugin[ProvisionStepData]], logger: Logger) -> None:
        self.enqueue_task(
            ProvisionTask(
                logger=logger,
                result=None,
                guest=None,
                exc=None,
                requested_exit=None,
                phases=phases,
            )
        )


class Provision(tmt.steps.Step):
    """
    Provision an environment for testing or use localhost.
    """

    # Default implementation for provision is a virtual machine
    DEFAULT_HOW = 'virtual'

    _plugin_base_class = ProvisionPlugin

    #: All known guests.
    #:
    #: .. warning::
    #:
    #:    Guests may not necessarily be fully provisioned. They are
    #:    from plugins as soon as possible, and guests may easily be
    #:    still waiting for their infrastructure to finish the task.
    #:    For the list of successfully provisioned guests, see
    #:    :py:attr:`ready_guests`.
    guests: list[Guest]

    @property
    def ready_guests(self) -> list[Guest]:
        """
        All successfully provisioned guests.

        Most of the time, after ``provision`` step finishes successfully,
        the list should be the same as :py:attr:`guests`, i.e. it should
        contain all known guests. There are situations when
        ``ready_guests`` will be a subset of ``guests``, and their users
        must decide which collection is the best for the desired goal:

        * when ``provision`` is still running. ``ready_guests`` will be
          slowly gaining new guests as they get up and running.
        * in dry-run mode, no actual provisioning is expected to happen,
          therefore there are no unsuccessfully provisioned guests. In
          this mode, all known guests are considered as ready, and
          ``ready_guests`` is the same as ``guests``.
        * if tmt is interrupted by a signal or user. Not all guests will
          finish their provisioning process, and ``ready_guests`` may
          contain just the finished ones.
        """

        if self.is_dry_run:
            return self.guests

        return [guest for guest in self.guests if guest.is_ready]

    def __init__(
        self,
        *,
        plan: 'tmt.Plan',
        data: tmt.steps.RawStepDataArgument,
        logger: tmt.log.Logger,
    ) -> None:
        """
        Initialize provision step data
        """

        super().__init__(plan=plan, data=data, logger=logger)

        self.guests = []
        self._guest_data: dict[str, GuestData] = {}

    @property
    def _preserved_workdir_members(self) -> set[str]:
        """
        A set of members of the step workdir that should not be removed.
        """

        return {*super()._preserved_workdir_members, 'guests.yaml'}

    @property
    def is_multihost(self) -> bool:
        return len(self.data) > 1

    def get_guests_info(self) -> list[tuple[str, Optional[str]]]:
        """
        Get a list containing the names and roles of guests that should be enabled.
        """

        phases = [
            cast(ProvisionPlugin[ProvisionStepData], phase)
            for phase in self.phases(classes=ProvisionPlugin)
            if phase.enabled_by_when
        ]
        return [(phase.data.name, phase.data.role) for phase in phases]

    def load(self) -> None:
        """
        Load guest data from the workdir
        """

        super().load()
        try:
            raw_guest_data = tmt.utils.yaml_to_dict(self.read(Path('guests.yaml')))

            self._guest_data = {
                name: SerializableContainer.unserialize(guest_data, self._logger)
                for name, guest_data in raw_guest_data.items()
            }

        except tmt.utils.FileError:
            self.debug('Provisioned guests not found.', level=2)

    def save(self) -> None:
        """
        Save guest data to the workdir
        """

        super().save()
        try:
            raw_guest_data = {
                guest.name: guest.save().to_serialized() for guest in self.ready_guests
            }

            self.write(Path('guests.yaml'), tmt.utils.dict_to_yaml(raw_guest_data))
        except tmt.utils.FileError:
            self.debug('Failed to save provisioned guests.')

    def wake(self) -> None:
        """
        Wake up the step (process workdir and command line)
        """

        super().wake()

        # Choose the right plugin and wake it up
        for data in self.data:
            # FIXME: cast() - see https://github.com/teemtee/tmt/issues/1599
            plugin = cast(
                ProvisionPlugin[ProvisionStepData], ProvisionPlugin.delegate(self, data=data)
            )
            self._phases.append(plugin)
            # If guest data loaded, perform a complete wake up
            plugin.wake(data=self._guest_data.get(plugin.name))

            if plugin.guest:
                self.guests.append(plugin.guest)

        # Nothing more to do if already done and not asked to run again
        if self.status() == 'done' and not self.should_run_again:
            self.debug('Provision wake up complete (already done before).', level=2)
        # Save status and step data (now we know what to do)
        else:
            self.status('todo')
            self.save()

    def suspend(self) -> None:
        super().suspend()

        for guest in self.guests:
            guest.suspend()

    def summary(self) -> None:
        """
        Give a concise summary of the provisioning
        """

        # Summary of provisioned guests
        guests = fmf.utils.listed(self.ready_guests, 'guest')
        self.info('summary', f'{guests} provisioned', 'green', shift=1)
        # Guest list in verbose mode
        for guest in self.ready_guests:
            if not guest.name.startswith(tmt.utils.DEFAULT_NAME):
                self.verbose(guest.name, color='red', shift=2)

    def go(self, force: bool = False) -> None:
        """
        Provision all guests
        """

        super().go(force=force)

        # Nothing more to do if already done
        if self.status() == 'done':
            self.info('status', 'done', 'green', shift=1)
            self.summary()
            self.actions()
            return

        # Provision guests
        self.guests = []

        def _run_provision_phases(
            phases: list[ProvisionPlugin[ProvisionStepData]],
        ) -> tuple[list[ProvisionTask], list[ProvisionTask]]:
            """
            Run the given set of ``provision`` phases.

            :param phases: list of ``provision`` step phases. By "running" them,
                they would provision their respective guests.
            :returns: two lists, a list of all :py:class:`ProvisionTask`
                instances queued, and a subset of the first list collecting only
                those tasks that failed.
            """

            queue: ProvisionQueue = ProvisionQueue(
                'provision.provision', self._logger.descend(logger_name=f'{self}.queue')
            )

            queue.enqueue(phases=phases, logger=queue._logger)

            all_tasks: list[ProvisionTask] = []
            failed_tasks: list[ProvisionTask] = []

            for outcome in queue.run():
                all_tasks.append(outcome)

                if outcome.exc:
                    outcome.logger.fail(str(outcome.exc))

                    failed_tasks.append(outcome)

                if outcome.guest:
                    outcome.guest.show()

                    self.guests.append(outcome.guest)

            return all_tasks, failed_tasks

        def _run_action_phases(phases: list[Action]) -> tuple[list[ActionTask], list[ActionTask]]:
            """
            Run the given set of actions.

            :param phases: list of actions, e.g. ``login`` or ``reboot``, given
                in the ``provision`` step.
            :returns: two lists, a list of all :py:class:`ActionTask` instances
                queued, and a subset of the first list collecting only those
                tasks that failed.
            """

            queue: PhaseQueue[ProvisionStepData, None] = PhaseQueue(
                'provision.action', self._logger.descend(logger_name=f'{self}.queue')
            )

            for action in phases:
                queue.enqueue_action(phase=action)

            all_tasks: list[ActionTask] = []
            failed_tasks: list[ActionTask] = []

            for outcome in queue.run():
                assert isinstance(outcome, ActionTask)

                all_tasks.append(outcome)

                if outcome.exc:
                    outcome.logger.fail(str(outcome.exc))

                    failed_tasks.append(outcome)

            return all_tasks, failed_tasks

        # Provisioning phases may be intermixed with actions. To perform all
        # phases and actions in a consistent manner, we will process them in
        # the order or their `order` key. We will group provisioning phases
        # not interrupted by action into batches, and run the sequence of
        # provisioning phases in parallel.
        all_phases = [
            p
            for p in self.phases(classes=(Action, ProvisionPlugin))
            if isinstance(p, Action) or p.enabled_by_when
        ]
        all_phases.sort(key=lambda x: x.order)

        all_outcomes: list[Union[ActionTask, ProvisionTask]] = []
        failed_outcomes: list[Union[ActionTask, ProvisionTask]] = []

        # Wrapping the code with try/except catching KeyboardInterrupt
        # exceptions that signals tmt has been interrupted. We need to
        # collect all known guests and populate `self.guests` so finish
        # can release them if necessary.
        try:
            while all_phases:
                # Start looking for sequences of phases of the same kind. Collect
                # as many as possible, until hitting a different one
                phase = all_phases.pop(0)

                if isinstance(phase, Action):
                    action_phases: list[Action] = [phase]

                    while all_phases and isinstance(all_phases[0], Action):
                        action_phases.append(cast(Action, all_phases.pop(0)))

                    all_action_outcomes, failed_action_outcomes = _run_action_phases(action_phases)

                    all_outcomes += all_action_outcomes
                    failed_outcomes += failed_action_outcomes

                else:
                    plugin_phases: list[ProvisionPlugin[ProvisionStepData]] = [phase]  # type: ignore[list-item]

                    # ignore[attr-defined]: mypy does not recognize `phase` as `ProvisionPlugin`.
                    if phase._thread_safe:  # type: ignore[attr-defined]
                        while all_phases:
                            if not isinstance(all_phases[0], ProvisionPlugin):
                                break

                            if not all_phases[0]._thread_safe:
                                break

                            plugin_phases.append(
                                cast(ProvisionPlugin[ProvisionStepData], all_phases.pop(0))
                            )

                    all_plugin_outcomes, failed_plugin_outcomes = _run_provision_phases(
                        plugin_phases
                    )

                    all_outcomes += all_plugin_outcomes
                    failed_outcomes += failed_plugin_outcomes

        except KeyboardInterrupt:
            self.guests = [
                phase.guest
                for phase in self.phases(classes=ProvisionPlugin)
                if phase.guest is not None
            ]

            raise

        # A plugin will only raise SystemExit if the exit is really desired
        # and no other actions should be done. An example of this is
        # listing available images. In such case, the workdir is deleted
        # as it's redundant and save() would throw an error.
        #
        # TODO: in theory, there may be many, many plugins raising `SystemExit`
        # but we can re-raise just a single one. It would be better to not use
        # an exception to signal this, but rather set/return a special object,
        # leaving the materialization into `SystemExit` to the step and/or tmt.
        # Or not do any one-shot actions under the disguise of provisioning...
        exiting_tasks = [outcome for outcome in all_outcomes if outcome.requested_exit is not None]

        if exiting_tasks:
            assert exiting_tasks[0].requested_exit is not None

            raise exiting_tasks[0].requested_exit

        if failed_outcomes:
            raise tmt.utils.GeneralError(
                'provision step failed',
                causes=[outcome.exc for outcome in failed_outcomes if outcome.exc is not None],
            )

        # To separate "provision" from the follow-up logging visually
        self.info('')

        # Give a summary, update status and save
        self.summary()
        self.status('done')
        self.save()

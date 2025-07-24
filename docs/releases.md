# Releases

## tmt-1.49.0

Output of the [tmt execute plugin](./plugins/execute.md#tmt) and
[display report plugin](./plugins/report.md#display) is changing in this release, to provide
slightly more details, headers and timestamps. `execute` now starts
using `display` for its own progress reporting, providing the unified
formatting and simplified code.

When the login step was called in a separate command after the guest
has been provisioned, the connection seemed to be stuck. This has been
caused by the SSH master process not being terminated together with tmt,
new tmt command would then spawn its own and conflict with the forgotten
one. tmt no longer leaves the SSH master process running, preventing the
issue.

An issue in the [beaker provision plugin](./plugins/provision.md#beaker)
prevented reconnecting to running guests. This has been fixed so
now it's possible to fully work with existing tmt runs as well.

The [import of remote plans](./spec/plans.md#import) support has been
extended to allow import of multiple plans. New keys, `scope` and
`importing`, allow users to control which plans to import and how to
connect them with the importing plans.

Fixed a bug that caused executed tests to remain in the
`pending` state when the machine became unresponsive. Tests will
now correctly transition to the `error` state.

Failures from tests and their checks were previously not fully saved
or reported. Now, a separate `failures.yaml` file is created for each
failed test and check, stored within their respective directories. When
a failure occurs, the path to this file is included in the result logs.
Check failures are now also reported to ReportPortal.

New [feature prepare plugin](./plugins/prepare.md#feature) `crb` has been
implemented which allows to easily enable or disable the CodeReady
Builder repository.

## tmt-1.48.0

The console log content is now available for guests provisioned by
the [virtual.testcloud plugin](./plugins/provision.md#virtual).

A new `tmt about` command has been introduced,
initially providing information about the [tmt plugins](./plugins/index.md).

The [HTML report plugin](./plugins/report.md#html) now supports a
new `file` key, allowing users to specify a custom output path for
the generated HTML report.

When using `and`/`or` groups in combination with
[hardware requirements](./spec/hardware.md), `tmt` will now emit
a warning to alert users about potential ambiguity in how these
constraints are applied.

For users of the [testcloud provisioner](./plugins/provision.md#virtual),
`PermitRootLogin` is now enabled by default for Red Hat CoreOS (RHCOS)
guests, simplifying access.

An issue with saving remote [Ansible playbooks](./plugins/prepare.md#ansible)
to the correct directory during provisioning and preparation has been fixed.

The internal representation of an imported plan has been improved,
though this should be largely transparent to users.

Several internal improvements and updates to development tooling and
CI processes have been made to enhance stability and maintainability.

## tmt-1.47.0

When `tmt` works with image mode, it now uses the native
package installation method instead of `rpm-ostree`.
`tmt` creates a `Containerfile` based on the booted image,
adds the required packages, builds a new image, and reboots the
system to use the updated image with the necessary packages.

If applicable, the `crb` repository is now automatically enabled
when enabling `epel` repository.

If a mixture of local and remote plans is detected, `tmt` now
prints a warning and skips the `local` plan.

In the `execute` step, the documentation of the `duration`
option was enhanced to correctly describe the effect of the
option.

The `execute` plugin now explicitly requires `awk` to be
installed on the machine, due to its recent removal from
Fedora containers.

The documentation of the `feature` plugins now includes a list
of required Ansible modules.

The documentation of plugins was improved to include examples
of keys with actual values.

The default unit of the `memory` hardware requirement is now
`MiB`. It is used if no unit was specified.

The steps documentation was deduplicated, and all information
from the specs was moved to the `plugins` section.

## tmt-1.46.0

The [junit report plugin](./plugins/report.md#junit) report plugin now supports a new
experimental `subresults` JUnit flavor. This flavor introduces
support for tmt subresults and adjusts the hierarchy of
`<testsuite>` and `<testcase>` tags. With this flavor, test
results are represented as `<testsuite>` tags, each containing a
`<testcase>` tag for the main result, along with additional
`<testcase>` tags for any subresults.

As a tech preview, a new [coredump check plugin](./plugins/test-checks.md#coredump)
has been added to detect system crashes using systemd-coredump
during test execution. The plugin monitors for any segmentation
faults and other crashes that produce core dumps. It can be configured
to ignore specific crash patterns and crash details are saved for
further investigation.

When reporting results to ReportPortal, each test result can now
directly link to a URL. To achieve this, a new key `link-template`
was added to the [reportportal plugin](./plugins/report.md#reportportal), which
can be used to provide a template that will be rendered for each test
result and appended to the end of its description. In cooperation with
Testing Farm, this will allow ReportPortal test results to directly
point to their respective artifacts.

A new `restraint-compatible` key has been implemented for the
[tmt execute plugin](./plugins/execute.md#tmt) which allows to enable
and disable the [restraint-compatibility features](questions.md#restraint-compatibility). For now
it only affects whether the `$OUTPUTFILE` variable is respected
or not. In the future this will allow users to enable/disable all
restraint compatibility features. Please, update your plans with
`restraint-compatibility: true` as soon as possible if your
tests depend on the restraint features.

A new [system.management-controller hardware property](./spec/hardware.md#system)
has been proposed to allow specifying the desired
system management interface (e.g., IPMI) when provisioning hardware.
While not yet implemented, this feature aims to support more precise
hardware selection in the future.

## tmt-1.45.0

FIPS mode can now be enabled for RHEL or CentosStream 8, 9 or 10
by a prepare step feature `fips`. Moreover, the `tmt try`
command now supports the new [`/try` option](./stories/cli.md#try)
option backed by the [feature plugin](./plugins/prepare.md#feature).

New option `--build-disk-image-only` is now supported by the
[bootc provision plugin](./plugins/provision.md#bootc) and can be used for just
building the disk image without actually provisioning the guest.

When running `tmt try`, failure in `prepare` phase drops the
user to the menu to be able to login to the machine and possibly
try it again.

When working with an existing run which involved executing only a
subset of plans, commands such as `tmt run --last report` will
load the respective plans only instead of all available plans to
save disk space and speed up the execution.

Aborted tests and tests that failed when
[`exit-first` was enabled](./spec/plans.md#execute-exit-first) did not skip all
remaining tests, only tests from the current `discover` phase.
Plans with multiple `discover` phases would start `execute`
step for remaining `discover` phases. This is now fixed, aborted
test and [`exit-first`](./spec/plans.md#execute-exit-first) will skip **all**
remaining tests.

Added support for translating hardware constraints using a config
file for the [beaker provision plugin](./plugins/provision.md#beaker). It
will try to get the config file, and find translations that would
match the constraints. See
`tmt.config.models.hardware.MrackTranslation` for an
example translation config.

When pruning a repository with a specified `path`, the
`discover` step now saves the data to the correct temporary
directory and respects the structure of the original repository.
This ensures that the test attributes have correct paths.

The latest `fmf` package is now required to ensure that the
`deployment-mode` context [dimension](./spec/context.md#dimension) is
fully supported.

The default [ssh-options used for connecting to provisioned guests](./plugins/provision.md#ssh-options) are now documented.

## tmt-1.44.0

The `results.yaml` file is now populated with test results
right after the `discover` step is finished and the file is
continuously updated during test execution to provide the latest
results. This change also adds a new `pending` result outcome
to the [results specification](./spec/results.md) for tests that were
discovered but not yet executed.

Execute tmt option `--ignore-duration` makes tmt to execute
the test as long as it needs. Execute plugin doesn't need to be
specified on the commandline for [plugin-variables](overview.md#plugin-variables) to work
for this option.

Add the `--command` option for the `tmt run reboot` so that
users specify the command to run on guest to trigger the reboot.

A new plan shaping plugin has been implemented to repeat a plan N times,
demonstrating how one plan can be turned into many plans.

The `deployment-mode` context dimension is now included in test run
exports to Polarion.

## tmt-1.43.0

Add the `--workdir-root` option for the `tmt clean images`
command so that users can specify the directory they want.

A new `upload-subresults` key has been introduced for the
[reportportal plugin](./plugins/report.md#reportportal), allowing the import of
tmt subresults as child test items into ReportPortal. This
behavior is optional and is disabled by default.

Option `tmt run --max N` can split plan to multiple plans to
include N tests at max.

Test name is logged in kernel buffer before and after the
[dmesg check plugin](./plugins/test-checks.md#dmesg) is executed.

## tmt-1.42.1

The `tmt show` command now prints in verbose mode manual test
instructions as well.

A new context [dimension](./spec/context.md#dimension)
has been added to the specification. It can be used to
[adjust test and plan metadata](./spec/core.md#adjust) for the
`package` or `image` mode context.

The `ansible-core` package is now a recommended dependency package
for tmt. It is used by plugins that use Ansible under the hood,
[prepare/ansible](./plugins/prepare.md#ansible),
[finish/ansible](./plugins/finish.md#ansible),
and [prepare/feature](./plugins/prepare.md#feature).

A new core attribute [author](./spec/core.md#author) has been implemented
for tracking the original author of the test, plan or story. In
contrast to the [contact key](./spec/core.md#contact), this field is not
supposed to be updated and can be useful when trying to track down
the original author for consultation.

The `container` executor now works in [Fedora Toolbx](https://docs.fedoraproject.org/en-US/fedora-silverblue/toolbox/) when Podman is run
using `flatpak-spawn --host` on the host system.

Add support for running playbooks from Ansible collections specified
using the `namespace.collection.playbook` notation.

Added `--dry` option for the `beaker` provision plugin. When
used it prints the Beaker Job XML without submitting it.

[Results specification documentation](./spec/results.md) has now
a dedicated place in the specification for improved discoverability.

The `rpm-ostree` package installation now includes the
`--assumeyes` option for improved compatibility.

Verbosity levels in `tmt * show` commands are now honored.

Added new traceback verbosity level, `TMT_SHOW_TRACEBACK=2`, which
prints local variables in every frame, shorterning long values. See
[command-variables](overview.md#command-variables) for details.

Fixed an issue where `execute` step incorrectly attempted to run
disabled `discover` phases.

Pre-defined order values of [prepare phases](./spec/plans.md#prepare)
were documented.

## tmt-1.41.1

Fedora Rawhide transitioned files from `/usr/sbin` to
`/usr/bin`, breaking path-based requirements installation for
the AVC check. This update adjusts the check to rely on packages,
restoring the functionality on Fedora Rawhide.

## tmt-1.41.0

Tests defined using the [shell discover method](./plugins/discover.md#shell)
are now executed in the exact order as listed in the config
file. This fixes a problem which has been introduced in the recent
`fmf` update.

The [reportportal plugin](./plugins/report.md#reportportal) now exports all
test contact information, rather than just the first contact
instance.

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin gains
support for submitting jobs on behalf of a group through the
`beaker-job-group` key. The submitting user must be a member of
the given job group.

The `note` field of tmt [results](./spec/results.md) changes from
a string to a list of strings, to better accommodate multiple notes.

The `Node` alias for the `Core` class has been dropped as it
has been deprecated a long time ago.

Previously when the test run was interrupted in the middle of the
test execution the [report step](./spec/plans.md#report) would be skipped
and no results would be reported. Now the report step is performed
always so that users can access results of those tests which were
successfully executed.

The `tmt try` command now accepts the whole action word in
addition to just a first letter, i.e. `l` and `login` now
both work.

## tmt-1.40.0

The execution of individual step configurations can be controlled
using the new [when key](./spec/plans.md#when-config) key. Enable and disable
selected step phase easily with the same syntax as used for the
context [adjust rules](./spec/core.md#adjust).

When the `login` command is used to enter an interactive session
on the guest, for example during a `tmt try` session, the
current working directory is set to the path of the last executed
test, so that users can easily investigate the test code there and
experiment with it directly on the guest.

A new `--workdir-root` option is now supported in the `tmt clean`
and `tmt run` commands so that users can specify the
directory which should be cleaned up and where new test runs
should be stored.

New `--keep` option has been implemented for the `tmt clean guests`
and `tmt clean` commands. Users can now choose to keep
the selected number of latest guests, and maybe also runs, clean
the rest to release the resources.

The log file paths of tmt subresults created by shell tests by
calling the `tmt-report-result` or by calling beakerlib's
`rlPhaseEnd` saved in `results.yaml` are now relative to the
`execute` directory.

The [reportportal plugin](./plugins/report.md#reportportal) now handles the
timestamps for `custom` and `restraint` results correctly. It
should prevent the `start-time` of a result being higher than
the `end-time`. It should be also ensured that the end time of
all launch items is the same or higher than the start time of a
parent item/launch.

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin gained
support for adding public keys to the guest instance by populating
the kickstart file.

Documentation pages now use the [new tmt logo](https://github.com/teemtee/docs/tree/main/logo) designed by Maria
Leonova.

## tmt-1.39.0

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin gains
support for [system.model-name](./spec/hardware.md#system),
[system.vendor-name](./spec/hardware.md#system),
[cpu.family](./spec/hardware.md#cpu) and
[cpu.frequency](./spec/hardware.md#cpu) hardware requirements.

The `tmt lint` command now reports a failure if empty
environment files are found.

The `tmt try` command now supports the new
[/try option](./stories/cli.md#try).

As a tech preview, a new [bootc provision plugin](./plugins/provision.md#bootc)
has been implemented. It takes a container image as input,
builds a bootc disk image from the container image, then uses the
[virtual.testcloud plugin](./plugins/provision.md#virtual) to create a
virtual machine using the bootc disk image.

The `tmt reportportal` plugin has newly introduced size limit
for logs uploaded to ReportPortal because large logs decreases
ReportPortal UI usability. Default limit are 1 MB for a test
output and 50 kB for a traceback (error log).
Limits can be controlled using the newly introduced
`reportportal` plugin options `--log-size-limit` and
`--traceback-size-limit` or the respective environment
variables.

## tmt-1.38.0

Test checks affect the overall test result by default. The
[check specification](./spec/tests.md#check) now supports a new
`result` key for individual checks. This attribute allows users
to control how the result of each check affects the overall test
result. Please note that tests, which were previously passing
with failing checks will now fail by default, unless the `xfail`
or `info` is added.

In order to prevent dangerous commands to be unintentionally run
on user's system, the [local provision plugin](./plugins/provision.md#local)
now requires to be executed with the `--feeling-safe`
option or with the environment variable `TMT_FEELING_SAFE` set
to `True`. See the [feeling-safe section](./stories/features.md#feeling-safe)
for more details and motivation behind this change.

The beakerlib test framework tests now generate tmt subresults.
The behavior is very similar to the shell test framework with
`tmt-report-result` command calls (see above). The
`tmt-report-result` now gets called with every `rlPhaseEnd`
macro and the tmt subresult gets created. The difference is that
the subresults outcomes are not evaluated by tmt. The tmt only
captures them and then relies on a beakerlib and its result
reporting, which does take the outcomes of phases into account to
determine the final test outcome. The subresults are always
assigned under the main tmt result and can be easily showed e.g.
by [display report plugin](./plugins/report.md#display) plugin when verbose mode is
enabled. There is only one exception - if the
`result: restraint` option is set to a beakerlib test, the
phase subresults get converted as normal tmt custom results.

Each execution of `tmt-report-result` command inside a shell
test will now create a tmt subresult. The main result outcome is
reduced from all subresults outcomes. If `tmt-report-result` is
not called during the test, the shell test framework behavior
remains the same - the test script exit code still has an impact
on the main test result. See also
[report-result feature](./stories/features.md#report-result).

Support for RHEL-like operating systems in [Image Mode](https://www.redhat.com/en/technologies/linux-platforms/enterprise-linux/image-mode) has been
added. The destination directory of the scripts added by `tmt`
on these operating systems is `/var/lib/tmt/scripts`. For
all others the `/usr/local/bin` destination directory is used.
A new environment variable `TMT_SCRIPTS_DIR` is available
to override the default locations.

The [fmf discover plugin](./plugins/discover.md#fmf) now supports
a new `adjust-tests` key which allows modifying metadata of all
discovered tests. This can be useful especially when fetching
tests from remote repositories where the user does not have write
access.

The `tmt link` command now supports providing multiple links by
using the `--link` option. See the [link-issues section](./stories/docs.md#link-issues)
for example usage.

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin gains support
for [cpu.stepping](./spec/hardware.md#cpu) hardware requirement.

The [junit report plugin](./plugins/report.md#junit) report plugin now removes all
invalid XML characters from the final JUnit XML.

A new [test-runner section](guide.md#test-runner) has been added to the tmt
[guide](guide.md). It describes some important differences between
running tests on a [user-system](guide.md#user-system) and scheduling test jobs in
[testing-farm](guide.md#testing-farm).

A race condition in the
[virtual.testcloud plugin](./plugins/provision.md#virtual) plugin has been fixed,
thus multihost tests using this provision method should now work
reliably without unexpected connection failures.

## tmt-1.37.0

The new `tmt link` command has been included as a Tech Preview
to gather early feedback from users about the way how issues are
linked with newly created and existing tests and plans. See the
[link-issues section](./stories/docs.md#link-issues) for details about the configuration.

The `tmt try` command now supports the new
[/try option](./stories/cli.md#try) backed by the
[prepare/feature plugin](./plugins/prepare.md#feature) and the
new [/try option](./stories/cli.md#try) backed by the
[prepare/install plugin](./plugins/prepare.md#install).

In verbose mode, the `discover` step now prints information
about the beakerlib libraries which were fetched for the test
execution. Use `tmt run discover -vvv` to see the details.

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin now newly
supports providing a custom [kickstart configuration](./spec/plans.md#provision-kickstart).

The new key [iommu](./spec/hardware.md#iommu) allowing to provision a
guest with the `Input–output memory management unit` has been
added into the [hardware specification](./spec/hardware.md) and implemented
in the [beaker provision plugin](./plugins/provision.md#beaker).

The [junit report plugin](./plugins/report.md#junit) report plugin now validates all
the XML flavors against their respective XSD schemas and tries to
prettify the final XML output. These functionalities are always
disabled for `custom` flavors.  The prettify functionality can
be controlled for non-custom templates by `--prettify` and
`--no-prettify` arguments.

The [junit report plugin](./plugins/report.md#junit) report plugin now uses Jinja
instead of `junit-xml` library to generate the JUnit XMLs. It
also adds support for a new `--flavor` argument. Using this
argument the user can choose between a `default` flavor, which
keeps the current behavior untouched, and a `custom` flavor
where user must provide a custom template using a
`--template-path` argument.

The [polarion report plugin](./plugins/report.md#polarion) report plugin now uses Jinja
template to generate the XUnit file. It doesn't do any extra
modifications to the XML tree using an `ElementTree` anymore.
Also the schema is now validated against the XSD.

The [reportportal plugin](./plugins/report.md#reportportal) plugin now uploads the
complete set of discovered tests, including those which have not
been executed. These tests are marked as `skipped`.

The `fmf-id.ref` will now try to report the most human-readable
committish reference, either branch, tag, git-describe, or if all
fails the commit hash.  You may encounter this in the verbose log
of `tmt tests show` or plan/test imports.

[Result specification](./spec/results.md) now defines
`original-result` key holding the original outcome of a test,
subtest or test checks. The effective outcome, stored in
`result` key, is computed from the original outcome, and it is
affected by inputs like [test result interpretation](./spec/tests.md#result)
or [test checks](./spec/tests.md#check).

The values in the generated `tmt-report-results.yaml` file are
now wrapped in double quotes, and any double quotes within the
values are escaped to ensure that the resulting file is always
valid YAML.

## tmt-1.36.1

tmt will now put SSH master control socket into `ssh-socket`
subdirectory of a workdir. Originally, sockets were stored in
`/run/user/$UID` directory, but this path led to conflicts when
multiple tmt instances shared sockets incorrectly. A fix landed in
1.36 that put sockets into `provision` subdirectory of each plan,
but this solution will break for plans with longer names because of
unavoidable UNIX socket path limit of 104 (or 108) characters.

## tmt-1.36.0

tmt will now emit a warning when [custom test results](./spec/tests.md#result)
file does not follow the [result specification](./spec/results.md).

We have started to use `warnings.deprecated` to advertise upcoming
API deprecations.

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin gains
support for submitting jobs on behalf of other users, through
`beaker-job-owner` key. The current user must be a submission delegate
for the given job owner.

In preparation for subresults: subresults and their checks have been integrated
into HTML report and display plugin, result phase renamed to subresult.

## tmt-1.35.0

If during test execution guest freezes in the middle of reboot,
test results are now correctly stored, all test artifacts from
the `TMT_TEST_DATA` and `TMT_PLAN_DATA` directories should be
fetched and available for investigation in the report.

New best practices in the [docs section](contribute.md#docs) now provide many
useful hints how to write good documentation when contributing
code.

The new key `include-output-log` and corresponding command line
options `--include-output-log` and `--no-include-output-log`
can now be used in the [junit report plugin](./plugins/report.md#junit) and
[polarion report plugin](./plugins/report.md#polarion) plugins to select whether only
failures or the full standard output should be included in the
generated report.

Change of Polarion field to store tmt id. Now using 'tmt ID' field,
specifically created for this purpose instead of 'Test Case ID' field.

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin gains
support for [cpu.vendor-name](./spec/hardware.md#cpu) and
[beaker.pool](./spec/hardware.md#beaker) hardware requirements.

The linting of tests, plans and stories has been extended by detecting
duplicate ids.

Test directories pruning now works correctly for nested fmf trees
and there is also a test for it.

The test key [result](./spec/tests.md#result) now supports new value
`restraint` which allows to treat each execution of the
`tmt-report-result`, `rstrnt-report-result` and
`rhts-report-result` commands as an independent test for which a
separate result is reported. The behaviour for existing tests
which already utilise these commands remains unchanged (the
overall result is determined by selecting the result with the
value which resides highest on the hierarchy of `skip`, `pass`,
`warn`, `fail`).

Add support for `--last`, `--id`, and `--skip` params for
the `clean` subcommand. Users can clean resources from the last
run or from a run with a given id. Users can also choose to skip
cleaning `guests`, `runs` or `images`.

## tmt-1.34.0

The [duration specification](./spec/tests.md#duration) now supports multiplication.

Added option `--failed-only` to the `tmt run tests` subcommand,
enabling rerunning failed tests from previous runs.

The [reportportal plugin](./plugins/report.md#reportportal) plugin copies
launch description also into the suite description when the
`--suite-per-plan` option is used.

The [virtual provision plugin](./plugins/provision.md#virtual)
gains support for adding multiple disks to guests, by adding
the corresponding `disk[N].size`
[HW requirements](./spec/hardware.md#disk).

## tmt-1.33.0

The [beaker provision plugin](./plugins/provision.md#beaker) provision plugin gains
support for [cpu.cores](./spec/hardware.md#cpu) and
[virtualization.hypervisor](./spec/hardware.md#virtualization)
hardware requirements.

It is now possible to set SSH options for all connections spawned by tmt
by setting environment variables `TMT_SSH_*`. This complements the
existing way of setting guest-specific SSH options by `ssh-options` key
of the guest. See [command-variables](overview.md#command-variables) for details.

New section [review](contribute.md#review) describing benefits and various forms of
pull request reviews has been added to the [contribute docs](contribute.md).

The [dmesg test check](./plugins/test-checks.md#dmesg) can be
configured to look for custom patterns in the output of `dmesg`
command, by setting its `failure-pattern` key.

Tests can now define their exit codes that would cause the test to be
restarted. Besides the `TMT_REBOOT_COUNT` environment variable, tmt
now exposes new variable called `TMT_TEST_RESTART_COUNT` to track
restarts of a said test. See [/spec/tests/restart](./spec/tests.md#restart) for details.

Requirements of the [upgrade execute plugin](./plugins/execute.md#upgrade)
tasks are now correctly installed before the upgrade is
performed on the guest.

## tmt-1.32.2

Set priorities for package manager discovery. They are now probed
in order: `rpm-ostree`, `dnf5`, `dnf`, `yum`, `apk`, `apt`.
This order picks the right package manager in the case when the
guest is `ostree-booted` but has the dnf installed.

## tmt-1.32.0

The hardware specification for [disk](./spec/hardware.md#disk) has been
extended with the new keys `driver` and `model-name`. Users
can provision Beaker guests with a given disk model or driver using
the [beaker provision plugin](./plugins/provision.md#beaker).

The [virtual provision plugin](./plugins/provision.md#virtual)
gains support for [TPM hardware requirement](./spec/hardware.md#tpm).
It is limited to TPM 2.0 for now, the future release of [testcloud](https://pagure.io/testcloud/),
the library behind `virtual` plugin, will extend the support to more
versions.

A new [watchdog test check](./plugins/test-checks.md#watchdog) has been
added. It monitors a guest running the test with either ping or SSH
connections, and may force reboot of the guest when it becomes
unresponsive. This is the first step towards helping tests handle kernel
panics and similar situations.

Internal implementation of basic package manager actions has been
refactored. tmt now supports package implementations to be shipped as
plugins, therefore allowing for tmt to work natively with distributions
beyond the ecosystem of rpm-based distributions. As a preview, `apt`,
the package manager used by Debian and Ubuntu, `rpm-ostree`, the
package manager used by `rpm-ostree`-based Linux systems and `apk`,
the package manager of Alpine Linux have been included in this release.

New environment variable `TMT_TEST_ITERATION_ID` has been added to
[test-variables](overview.md#test-variables). This variable is a combination of a unique
run ID and the test serial number. The value is different for each
new test execution.

New environment variable `TMT_REPORT_ARTIFACTS_URL` has been added
to [command-variables](overview.md#command-variables). It can be used to provide a link for
detailed test artifacts for report plugins to pick.

[Beaker provision plugin](./plugins/provision.md#beaker)
gains support for [System z cryptographic adapter HW requirement](./spec/hardware.md#zcrypt).

The [discover dist-git-source option](./spec/plans.md#discover-dist-git-source) apply patches now using
`rpmbuild -bp` command. This is done on provisioned guest during
the `prepare` step, before required packages are installed.
It is possible to install build requires automatically with
`dist-git-install-builddeps` flag or specify additional
packages required to be present with `dist-git-require` option.

## tmt-1.31.0

The [provision step](./spec/plans.md#provision) is now able to perform
**provisioning of multiple guests in parallel**. This can
considerably shorten the time needed for guest provisioning in
multihost plans. However, whether the parallel provisioning would
take place depends on what provision plugins were involved,
because not all plugins are compatible with this feature yet. As
of now, only [artemis](./plugins/provision.md#artemis),
[connect](./plugins/provision.md#connect),
[container](./plugins/provision.md#container),
[local](./plugins/provision.md#local), and
[virtual](./plugins/provision.md#virtual) are supported. All
other plugins would gracefully fall back to the pre-1.31 behavior,
provisioning in sequence.

The [prepare step](./spec/plans.md#prepare) now installs test requirements
only on guests on which the said tests would run. Tests can be
directed to subset of guests with a
[discover where key](./spec/plans.md#discover-where), but, until 1.31, tmt would
install all requirements of a given test on all guests, even on
those on which the said test would never run.  This approach
consumed resources needlessly and might be a issue for tests with
conflicting requirements. Since 1.31, handling of
[test require](./spec/tests.md#require) and [test recommend](./spec/tests.md#recommend)
affects only guests the test would be scheduled on.

New option `--again` can be used to execute an already completed
step once again without completely removing the step workdir which
is done when `--force` is used.

New environment variable `TMT_REBOOT_TIMEOUT` has been added to
[command-variables](overview.md#command-variables). It can be used to set a custom reboot
timeout. The default timeout was increased to 10 minutes.

New hardware specification key [zcrypt](./spec/hardware.md#zcrypt) has
been defined. It will be used for selecting guests with the given
`System z cryptographic adapter`.

A prepare step plugin [feature](./plugins/prepare.md#feature) has been
implemented. As the first supported feature, `epel` repositories
can now be enabled using a concise configuration.

The report plugin [report](./spec/plans.md#report) has received new options.
Namely option `--launch-per-plan` for creating a new launch per each
plan, option `--suite-per-plan` for mapping a suite per each plan,
all enclosed in one launch (launch uuid is stored in run of the first
plan), option `--launch-description` for providing unified launch
description, intended mainly for suite-per-plan mapping, option
`--upload-to-launch LAUNCH_ID` to append new plans to an existing
launch, option `--upload-to-suite SUITE_ID` to append new tests
to an existing suite within launch, option `--launch-rerun` for
reruns with 'Retry' item in RP, and option `--defect-type` for
passing the defect type to failing tests, enables report idle tests
to be additionally updated. Environment variables were rewritten to
the uniform form `TMT_PLUGIN_REPORT_REPORTPORTAL_${option}`.

## tmt-1.30.0

The new [tmt try command](./stories/cli.md#try) provides an
interactive session which allows to easily run tests and
experiment with the provisioned guest. The functionality might
still change. This is the very first proof of concept included in
the release as a **tech preview** to gather early feedback and
finalize the outlined design. Give it a [try](./stories/cli.md#try)
and let us know what you think! :)

Now it's possible to use [custom templates](./stories/docs.md#custom-templates) when creating new
tests, plans and stories. In this way you can substantially speed
up the initial phase of the test creation by easily applying test
metadata and test script skeletons tailored to your individual
needs.

The [contact key](./spec/core.md#contact) has been moved from the
[tests specification](./spec/tests.md) specification to the [core attributes](./spec/core.md)
so now it can be used with plans and stories as well.

The [container provision plugin](./plugins/provision.md#container)
enables a network accessible to all containers in the plan. So for
faster [multihost-testing](questions.md#mulithost-compatibility) it's now possible to use
containers as well.

For the purpose of tmt exit code, `info` test results are no
longer considered as failures, and therefore the exit code of tmt
changes. `info` results are now treated as `pass` results, and
would be counted towards the successful exit code, `0`, instead
of the exit code `2` in older releases.

The [polarion report plugin](./plugins/report.md#polarion) report now supports the
`fips` field to store information about whether the FIPS mode
was enabled or disabled on the guest during the test execution.

The `name` field of the [check specification](./spec/tests.md#check)
has been renamed to `how`, to be more aligned with how plugins
are selected for step phases and export formats.

A new [tty attribute](./spec/tests.md#tty) boolean attribute was added to the
[tests specification](./spec/tests.md). Tests can now control if they
want to keep tty enabled. The default value of the attribute is
`false`, in sync with the previous default behaviour.

See the [full changelog](https://github.com/teemtee/tmt/releases/tag/1.30.0) for more details.

## tmt-1.29.0

Test directories can be pruned with the `prune` option usable in
the [fmf discover plugin](./plugins/discover.md#fmf). When enabled, only
test's path and required files will be kept.

The [discover dist-git-source option](./spec/plans.md#discover-dist-git-source)
`download-only` skips extraction of downloaded sources. All
source files are now downloaded regardless this option.

Environment variables can now be also stored into the
`TMT_PLAN_ENVIRONMENT_FILE`. Variables defined in this file are
sourced immediately after the `prepare` step, making them
accessible in the tests and across all subsequent steps. See
the [step-variables section](overview.md#step-variables) for details.

When the `tmt-report-result` command is used it sets the test
result exclusively. The framework is not consulted any more. This
means that the test script exit code does not have any effect on
the test result. See also [report-result feature](./stories/features.md#report-result).

The `tmt-reboot` command is now usable outside of the test
process. See the [reboot feature section](./stories/features.md#reboot) for usage
details.

The [provision step](./spec/plans.md#provision) methods gain the `become`
option which allows to use a user account and execute
`prepare`, `execute` and `finish` steps using `sudo -E`
when necessary.

The [html report plugin](./plugins/report.md#html) report plugin now shows
[check results](./spec/tests.md#check) so that it's possible to inspect
detected AVC denials directly from the report.

See the [full changelog](https://github.com/teemtee/tmt/releases/tag/1.29.0) for more details.

## tmt-1.28.0

The new [update-missing option](./stories/cli.md#multiple-phases)
can be used to update step phase fields only when not set in the
`fmf` files. In this way it's possible to easily fill the gaps
in the plans, for example provide the default distro image.

The [html report plugin](./plugins/report.md#html) report plugin now shows
provided [context](./spec/plans.md#context) and link to the test `data`
directory so that additional logs can be easily checked.

The **avc** [check](./spec/tests.md#check) allows to detect avc denials
which appear during the test execution.

A new `skip` custom result outcome has been added to the
[results specification](./spec/results.md).

All context [dimension values](./spec/context.md#dimension) are now handled
in a case insensitive way.

See the [full changelog](https://github.com/teemtee/tmt/releases/tag/1.28.0) for more details.

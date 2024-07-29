# Releases

## tmt-1.35

If during test execution guest freezes in the middle of reboot, test
results are now correctly stored, all test artifacts from the
`TMT_TEST_DATA` and `TMT_PLAN_DATA` directories should be fetched and
available for investigation in the report.

New best practices in the `docs`{.interpreted-text role="ref"} section
now provide many useful hints how to write good documentation when
contributing code.

The new key `include-output-log` and corresponding command line options
`--include-output-log` and `--no-include-output-log` can now be used in
the `/plugins/report/junit`{.interpreted-text role="ref"} and
`/plugins/report/polarion`{.interpreted-text role="ref"} plugins to
select whether only failures or the full standard output should be
included in the generated report.

Change of Polarion field to store tmt id. Now using \'tmt ID\' field,
specifically created for this purpose instead of \'Test Case ID\' field.

## tmt-1.34

The `/spec/tests/duration`{.interpreted-text role="ref"} now supports
multiplication.

Added option `--failed-only` to the `tmt run tests` subcommand, enabling
rerunning failed tests from previous runs.

The `/plugins/report/reportportal`{.interpreted-text role="ref"} plugin
copies launch description also into the suite description when the
`--suite-per-plan` option is used.

The `virtual</plugins/provision/virtual.testcloud>`{.interpreted-text
role="ref"} provision plugin gains support for adding multiple disks to
guests, by adding the corresponding `disk[N].size`
`HW requirements</spec/hardware/disk>`{.interpreted-text role="ref"}.

## tmt-1.33

The `/plugins/provision/beaker`{.interpreted-text role="ref"} provision
plugin gains support for
`cpu.cores</spec/hardware/cpu>`{.interpreted-text role="ref"} and
`virtualization.hypervisor</spec/hardware/virtualization>`{.interpreted-text
role="ref"} hardware requirements.

It is now possible to set SSH options for all connections spawned by tmt
by setting environment variables `TMT_SSH_*`. This complements the
existing way of setting guest-specific SSH options by `ssh-options` key
of the guest. See `command-variables`{.interpreted-text role="ref"} for
details.

New section `review`{.interpreted-text role="ref"} describing benefits
and various forms of pull request reviews has been added to the
`contribute`{.interpreted-text role="ref"} docs.

The `dmesg test check</plugins/test-checks/dmesg>`{.interpreted-text
role="ref"} can be configured to look for custom patterns in the output
of `dmesg` command, by setting its `failure-pattern` key.

Tests can now define their exit codes that would cause the test to be
restarted. Besides the `TMT_REBOOT_COUNT` environment variable, tmt now
exposes new variable called `TMT_TEST_RESTART_COUNT` to track restarts
of a said test. See `/spec/tests/restart`{.interpreted-text role="ref"}
for details.

Requirements of the `/plugins/execute/upgrade`{.interpreted-text
role="ref"} execute plugin tasks are now correctly installed before the
upgrade is performed on the guest.

## tmt-1.32.2

Set priorities for package manager discovery. They are now probed in
order: `rpm-ostree`, `dnf5`, `dnf`, `yum`, `apk`, `apt`. This order
picks the right package manager in the case when the guest is
`ostree-booted` but has the dnf installed.

## tmt-1.32

The hardware specification for `/spec/hardware/disk`{.interpreted-text
role="ref"} has been extended with the new keys `driver` and
`model-name`. Users can provision Beaker guests with a given disk model
or driver using the `/plugins/provision/beaker`{.interpreted-text
role="ref"} provision plugin.

The `virtual</plugins/provision/virtual.testcloud>`{.interpreted-text
role="ref"} provision plugin gains support for
`TPM hardware requirement</spec/hardware/tpm>`{.interpreted-text
role="ref"}. It is limited to TPM 2.0 for now, the future release of
[testcloud](https://pagure.io/testcloud/), the library behind `virtual`
plugin, will extend the support to more versions.

A new
`watchdog test check</plugins/test-checks/watchdog>`{.interpreted-text
role="ref"} has been added. It monitors a guest running the test with
either ping or SSH connections, and may force reboot of the guest when
it becomes unresponsive. This is the first step towards helping tests
handle kernel panics and similar situations.

Internal implementation of basic package manager actions has been
refactored. tmt now supports package implementations to be shipped as
plugins, therefore allowing for tmt to work natively with distributions
beyond the ecosystem of rpm-based distributions. As a preview, `apt`,
the package manager used by Debian and Ubuntu, `rpm-ostree`, the package
manager used by `rpm-ostree`-based Linux systems and `apk`, the package
manager of Alpine Linux have been included in this release.

New environment variable `TMT_TEST_ITERATION_ID` has been added to
`test-variables`{.interpreted-text role="ref"}. This variable is a
combination of a unique run ID and the test serial number. The value is
different for each new test execution.

New environment variable `TMT_REPORT_ARTIFACTS_URL` has been added to
`command-variables`{.interpreted-text role="ref"}. It can be used to
provide a link for detailed test artifacts for report plugins to pick.

`Beaker</plugins/provision/beaker>`{.interpreted-text role="ref"}
provision plugin gains support for
`System z cryptographic adapter</spec/hardware/zcrypt>`{.interpreted-text
role="ref"} HW requirement.

The `/spec/plans/discover/dist-git-source`{.interpreted-text role="ref"}
apply patches now using `rpmbuild -bp` command. This is done on
provisioned guest during the `prepare` step, before required packages
are installed. It is possible to install build requires automatically
with `dist-git-install-builddeps` flag or specify additional packages
required to be present with `dist-git-require` option.

## tmt-1.31

The `/spec/plans/provision`{.interpreted-text role="ref"} step is now
able to perform **provisioning of multiple guests in parallel**. This
can considerably shorten the time needed for guest provisioning in
multihost plans. However, whether the parallel provisioning would take
place depends on what provision plugins were involved, because not all
plugins are compatible with this feature yet. As of now, only
`/plugins/provision/artemis`{.interpreted-text role="ref"},
`/plugins/provision/connect`{.interpreted-text role="ref"},
`/plugins/provision/container`{.interpreted-text role="ref"},
`/plugins/provision/local`{.interpreted-text role="ref"}, and
`virtual</plugins/provision/virtual.testcloud>`{.interpreted-text
role="ref"} are supported. All other plugins would gracefully fall back
to the pre-1.31 behavior, provisioning in sequence.

The `/spec/plans/prepare`{.interpreted-text role="ref"} step now
installs test requirements only on guests on which the said tests would
run. Tests can be directed to subset of guests with a
`/spec/plans/discover/where`{.interpreted-text role="ref"} key, but,
until 1.31, tmt would install all requirements of a given test on all
guests, even on those on which the said test would never run. This
approach consumed resources needlessly and might be a issue for tests
with conflicting requirements. Since 1.31, handling of
`/spec/tests/require`{.interpreted-text role="ref"} and
`/spec/tests/recommend`{.interpreted-text role="ref"} affects only
guests the test would be scheduled on.

New option `--again` can be used to execute an already completed step
once again without completely removing the step workdir which is done
when `--force` is used.

New environment variable `TMT_REBOOT_TIMEOUT` has been added to
`command-variables`{.interpreted-text role="ref"}. It can be used to set
a custom reboot timeout. The default timeout was increased to 10
minutes.

New hardware specification key `/spec/hardware/zcrypt`{.interpreted-text
role="ref"} has been defined. It will be used for selecting guests with
the given [System z cryptographic adapter]{.title-ref}.

A prepare step plugin `/plugins/prepare/feature`{.interpreted-text
role="ref"} has been implemented. As the first supported feature, `epel`
repositories can now be enabled using a concise configuration.

The report plugin `/spec/plans/report`{.interpreted-text role="ref"} has
received new options. Namely option `--launch-per-plan` for creating a
new launch per each plan, option `--suite-per-plan` for mapping a suite
per each plan, all enclosed in one launch (launch uuid is stored in run
of the first plan), option `--launch-description` for providing unified
launch description, intended mainly for suite-per-plan mapping, option
`--upload-to-launch LAUNCH_ID` to append new plans to an existing
launch, option `--upload-to-suite SUITE_ID` to append new tests to an
existing suite within launch, option `--launch-rerun` for reruns with
\'Retry\' item in RP, and option `--defect-type` for passing the defect
type to failing tests, enables report idle tests to be additionally
updated. Environment variables were rewritten to the uniform form
`TMT_PLUGIN_REPORT_REPORTPORTAL_${option}`.

## tmt-1.30

The new `tmt try</stories/cli/try>`{.interpreted-text role="ref"}
command provides an interactive session which allows to easily run tests
and experiment with the provisioned guest. The functionality might still
change. This is the very first proof of concept included in the release
as a **tech preview** to gather early feedback and finalize the outlined
design. Give it a `/stories/cli/try`{.interpreted-text role="ref"} and
let us know what you think! :)

Now it\'s possible to use `custom_templates`{.interpreted-text
role="ref"} when creating new tests, plans and stories. In this way you
can substantially speed up the initial phase of the test creation by
easily applying test metadata and test script skeletons tailored to your
individual needs.

The `/spec/core/contact`{.interpreted-text role="ref"} key has been
moved from the `/spec/tests`{.interpreted-text role="ref"} specification
to the `/spec/core`{.interpreted-text role="ref"} attributes so now it
can be used with plans and stories as well.

The `/plugins/provision/container`{.interpreted-text role="ref"}
provision plugin enables a network accessible to all containers in the
plan. So for faster `multihost-testing`{.interpreted-text role="ref"}
it\'s now possible to use containers as well.

For the purpose of tmt exit code, `info` test results are no longer
considered as failures, and therefore the exit code of tmt changes.
`info` results are now treated as `pass` results, and would be counted
towards the successful exit code, `0`, instead of the exit code `2` in
older releases.

The `/spec/plans/report/polarion`{.interpreted-text role="ref"} report
now supports the `fips` field to store information about whether the
FIPS mode was enabled or disabled on the guest during the test
execution.

The `name` field of the `/spec/tests/check`{.interpreted-text
role="ref"} specification has been renamed to `how`, to be more aligned
with how plugins are selected for step phases and export formats.

A new `/spec/tests/tty`{.interpreted-text role="ref"} boolean attribute
was added to the `/spec/tests`{.interpreted-text role="ref"}
specification. Tests can now control if they want to keep tty enabled.
The default value of the attribute is `false`, in sync with the previous
default behaviour.

See the [full
changelog](https://github.com/teemtee/tmt/releases/tag/1.30.0) for more
details.

## tmt-1.29

Test directories can be pruned with the `prune` option usable in the
`/plugins/discover/fmf`{.interpreted-text role="ref"} plugin. When
enabled, only test\'s path and required files will be kept.

The `/spec/plans/discover/dist-git-source`{.interpreted-text role="ref"}
option `download-only` skips extraction of downloaded sources. All
source files are now downloaded regardless this option.

Environment variables can now be also stored into the
`TMT_PLAN_ENVIRONMENT_FILE`. Variables defined in this file are sourced
immediately after the `prepare` step, making them accessible in the
tests and across all subsequent steps. See the
`step-variables`{.interpreted-text role="ref"} section for details.

When the `tmt-report-result` command is used it sets the test result
exclusively. The framework is not consulted any more. This means that
the test script exit code does not have any effect on the test result.
See also `/stories/features/report-result`{.interpreted-text
role="ref"}.

The `tmt-reboot` command is now usable outside of the test process. See
the `/stories/features/reboot`{.interpreted-text role="ref"} section for
usage details.

The `/spec/plans/provision`{.interpreted-text role="ref"} step methods
gain the `become` option which allows to use a user account and execute
`prepare`, `execute` and `finish` steps using `sudo -E` when necessary.

The `/spec/plans/report/html`{.interpreted-text role="ref"} report
plugin now shows `/spec/tests/check`{.interpreted-text role="ref"}
results so that it\'s possible to inspect detected AVC denials directly
from the report.

See the [full
changelog](https://github.com/teemtee/tmt/releases/tag/1.29.0) for more
details.

## tmt-1.28

The new `/stories/cli/multiple phases/update-missing`{.interpreted-text
role="ref"} option can be used to update step phase fields only when not
set in the `fmf` files. In this way it\'s possible to easily fill the
gaps in the plans, for example provide the default distro image.

The `/spec/plans/report/html`{.interpreted-text role="ref"} report
plugin now shows provided `/spec/plans/context`{.interpreted-text
role="ref"} and link to the test `data` directory so that additional
logs can be easily checked.

The **avc** `/spec/tests/check`{.interpreted-text role="ref"} allows to
detect avc denials which appear during the test execution.

A new `skip` custom result outcome has been added to the
`/spec/plans/results`{.interpreted-text role="ref"} specification.

All context `/spec/context/dimension`{.interpreted-text role="ref"}
values are now handled in a case insensitive way.

See the [full
changelog](https://github.com/teemtee/tmt/releases/tag/1.28.0) for more
details.

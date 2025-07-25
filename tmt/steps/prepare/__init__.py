import copy
from typing import TYPE_CHECKING, Any, Literal, Optional, TypeVar, cast

import click
import fmf
import fmf.utils

import tmt
import tmt.log
import tmt.steps
import tmt.steps.discover
import tmt.steps.provision
import tmt.utils
from tmt.container import container, simple_field
from tmt.options import option
from tmt.plugins import PluginRegistry
from tmt.result import PhaseResult, ResultOutcome
from tmt.steps import (
    Action,
    PhaseQueue,
    PluginOutcome,
    PluginTask,
    PullTask,
    PushTask,
    sync_with_guests,
)
from tmt.steps.provision import Guest
from tmt.utils import uniq

if TYPE_CHECKING:
    import tmt.base
    import tmt.cli
    from tmt.base import Plan


@container
class PrepareStepData(tmt.steps.WhereableStepData, tmt.steps.StepData):
    pass


PrepareStepDataT = TypeVar('PrepareStepDataT', bound=PrepareStepData)


class _RawPrepareStepData(tmt.steps._RawStepData, tmt.steps.RawWhereableStepData, total=False):
    pass


class PreparePlugin(tmt.steps.Plugin[PrepareStepDataT, PluginOutcome]):
    """
    Common parent of prepare plugins
    """

    # ignore[assignment]: as a base class, PrepareStepData is not included in
    # PrepareStepDataT.
    _data_class = PrepareStepData  # type: ignore[assignment]

    # Methods ("how: ..." implementations) registered for the same step.
    _supported_methods: PluginRegistry[tmt.steps.Method] = PluginRegistry('step.prepare')

    @classmethod
    def base_command(
        cls,
        usage: str,
        method_class: Optional[type[click.Command]] = None,
    ) -> click.Command:
        """
        Create base click command (common for all prepare plugins)
        """

        # Prepare general usage message for the step
        if method_class:
            usage = Prepare.usage(method_overview=usage)

        # Create the command
        @click.command(cls=method_class, help=usage)
        @click.pass_context
        @option(
            '-h',
            '--how',
            metavar='METHOD',
            help='Use specified method for environment preparation.',
        )
        @tmt.steps.PHASE_OPTIONS
        def prepare(context: 'tmt.cli.Context', **kwargs: Any) -> None:
            context.obj.steps.add('prepare')
            Prepare.store_cli_invocation(context)

        return prepare

    def go(
        self,
        *,
        guest: 'tmt.steps.provision.Guest',
        environment: Optional[tmt.utils.Environment] = None,
        logger: tmt.log.Logger,
    ) -> PluginOutcome:
        """
        Prepare the guest (common actions)
        """

        self.go_prolog(logger)

        # Show guest name first in multihost scenarios
        if self.step.plan.provision.is_multihost:
            logger.info('guest', guest.name, 'green')

        # Show requested role if defined
        if self.data.where:
            logger.info('where', fmf.utils.listed(self.data.where), 'green')

        return PluginOutcome()


# Required & recommended packages
#
# Structures and code for collecting requirements for different guests
# or their groups to avoid installing all test requirements on all
# guests. For example, a test running on the "server" guest might
# require package `foo` while the test running on the "client" might
# require package `bar`, and `foo` and `bar` cannot be installed at the
# same time.


@container
class DependencyCollection:
    """
    Bundle guests and packages to install on them
    """

    # Guest*s*, not a guest. The list will start with just one guest at
    # first, but when grouping guests by same requirements, we'd start
    # adding guests to the list when spotting same set of dependencies.
    guests: list[Guest]
    dependencies: list['tmt.base.DependencySimple'] = simple_field(default_factory=list)

    @property
    def as_key(self) -> 'DependencyCollectionKey':
        return frozenset(self.dependencies)


DependencyCollectionKey = frozenset['tmt.base.DependencySimple']


class Prepare(tmt.steps.Step):
    """
    Prepare the environment for testing.

    Use the 'order' attribute to select in which order preparation
    should happen if there are multiple configs. Default order is '50'.
    Default order of required packages installation is '70', for the
    recommended packages it is '75'.
    """

    _plugin_base_class = PreparePlugin

    @property
    def _preserved_workdir_members(self) -> set[str]:
        """
        A set of members of the step workdir that should not be removed.
        """

        return {*super()._preserved_workdir_members, 'results.yaml'}

    def __init__(
        self,
        *,
        plan: 'Plan',
        data: tmt.steps.RawStepDataArgument,
        logger: tmt.log.Logger,
    ) -> None:
        """
        Initialize prepare step data
        """

        super().__init__(plan=plan, data=data, logger=logger)
        self.preparations_applied = 0

    def wake(self) -> None:
        """
        Wake up the step (process workdir and command line)
        """

        super().wake()

        # Choose the right plugin and wake it up
        for data in self.data:
            # FIXME: cast() - see https://github.com/teemtee/tmt/issues/1599
            plugin = cast(PreparePlugin[PrepareStepData], PreparePlugin.delegate(self, data=data))
            plugin.wake()
            # Add plugin only if there are data
            if not plugin.data.is_bare:
                self._phases.append(plugin)

        # Nothing more to do if already done and not asked to run again
        if self.status() == 'done' and not self.should_run_again:
            self.debug('Prepare wake up complete (already done before).', level=2)
        # Save status and step data (now we know what to do)
        else:
            self.status('todo')
            self.save()

    def summary(self) -> None:
        """
        Give a concise summary of the preparation
        """

        preparations = fmf.utils.listed(self.preparations_applied, 'preparation')
        self.info('summary', f'{preparations} applied', 'green', shift=1)

    def go(self, force: bool = False) -> None:
        """
        Prepare the guests
        """

        super().go(force=force)

        # Nothing more to do if already done
        if self.status() == 'done':
            self.info('status', 'done', 'green', shift=1)
            self.summary()
            self.actions()
            return

        import tmt.base

        # All phases from all steps.
        phases = [
            phase
            for step in (
                self.plan.discover,
                self.plan.provision,
                self.plan.prepare,
                self.plan.execute,
                self.plan.finish,
                self.plan.report,
            )
            for phase in step.phases(classes=step._plugin_base_class)
        ]

        # All provisioned guests.
        guests = self.plan.provision.ready_guests

        # 1. collect all requirements, per guest. For each phase, test,
        # check and so on, find out on which guest it needs to run, and
        # add its requirements to a guest-specific collection.

        # Collecting all essential requirements, per guest.
        collected_essential_requires = {
            guest: DependencyCollection(guests=[guest]) for guest in guests
        }

        # Collecting all required packages, per guest.
        collected_requires = {guest: DependencyCollection(guests=[guest]) for guest in guests}

        # Collecting all recommended packages, per guest.
        collected_recommends = {guest: DependencyCollection(guests=[guest]) for guest in guests}

        # For each guest, check everything that can run, and if enabled
        # for the given guest, add requirements into the correct
        # collection.
        for guest in guests:
            # First, check phases - plugins have their own requirements,
            # the essential requirements.
            for phase in phases:
                if not phase.enabled_by_when:
                    continue
                if not phase.enabled_on_guest(guest):
                    continue

                collected_essential_requires[
                    guest
                ].dependencies += tmt.base.assert_simple_dependencies(
                    # ignore[attr-defined]: mypy thinks that phase is Phase type, while its
                    # actually PluginClass
                    phase.essential_requires(),  # type: ignore[attr-defined]
                    'After beakerlib processing, tests may have only simple requirements',
                    self._logger,
                )

            # The `discover` step is different: no phases, just query tests
            # collected by the step itself. Maybe we could iterate over
            # `discover` phases, but I think re-runs and workdir reuse would
            # use what the step loads from its storage, `tests.yaml`. Which
            # means, there probably would be no phases to inspect from time to
            # time, therefore going after the step itself.
            for test_origin in self.plan.discover.tests(enabled=True):
                test = test_origin.test

                if not test.enabled_on_guest(guest):
                    continue

                collected_requires[guest].dependencies += tmt.base.assert_simple_dependencies(
                    test.require,
                    'After beakerlib processing, tests may have only simple requirements',
                    self._logger,
                )

                collected_recommends[guest].dependencies += tmt.base.assert_simple_dependencies(
                    test.recommend,
                    'After beakerlib processing, tests may have only simple requirements',
                    self._logger,
                )

                collected_essential_requires[
                    guest
                ].dependencies += test.test_framework.get_requirements(test, self._logger)

                for check in test.check:
                    collected_essential_requires[
                        guest
                    ].dependencies += check.plugin.essential_requires(guest, test, self._logger)

        # 2. Now we have guests and all their requirements. There can be
        # duplicities, multiple tests requesting the same package, but also
        # some guests may share the set of packages to be installed on them.
        # Let's say N guests share a `role`, all their tests would add the same
        # requirements to these guests.
        #
        # So the final 2 steps:
        #
        # 1. make the list of requirements unique,
        # 2. group guests with same requirements.
        def _prune_collections(
            collections: dict[Guest, DependencyCollection],
        ) -> list[DependencyCollection]:
            pruned: dict[DependencyCollectionKey, DependencyCollection] = {}

            for guest, collection in collections.items():
                collection.dependencies = uniq(collection.dependencies)

                if collection.as_key in pruned:
                    pruned[collection.as_key].guests.append(guest)

                else:
                    pruned[collection.as_key] = collection

            return list(pruned.values())

        pruned_essential_requires = _prune_collections(collected_essential_requires)
        pruned_requires = _prune_collections(collected_requires)
        pruned_recommends = _prune_collections(collected_recommends)

        # 3. for each collection, which now groups a set of packages and
        # all guests they need to be installed on, add new phase that
        # would take care of installation.
        def _emit_phase(
            pruned_collections: list[DependencyCollection],
            name: str,
            summary: str,
            order: int,
            missing: Literal['skip', 'fail'] = 'fail',
        ) -> None:
            from tmt.steps.prepare.install import PrepareInstallData

            for collection in pruned_collections:
                if not collection.dependencies:
                    continue

                data = PrepareInstallData(
                    name=name,
                    how='install',
                    summary=summary,
                    order=order,
                    where=[guest.name for guest in collection.guests],
                    package=collection.dependencies,
                    missing=missing,
                )

                self._phases.append(PreparePlugin.delegate(self, data=data))

        _emit_phase(
            pruned_essential_requires,
            'essential-requires',
            'Install essential required packages',
            tmt.steps.PHASE_ORDER_PREPARE_INSTALL_ESSENTIAL_REQUIRES,
        )

        _emit_phase(
            pruned_requires,
            'requires',
            'Install required packages',
            tmt.steps.PHASE_ORDER_PREPARE_INSTALL_REQUIRES,
        )

        _emit_phase(
            pruned_recommends,
            'recommends',
            'Install recommended packages',
            tmt.steps.PHASE_ORDER_PREPARE_INSTALL_RECOMMENDS,
            missing='skip',
        )

        # Prepare guests (including workdir sync)
        guest_copies: list[Guest] = []

        for guest in self.plan.provision.ready_guests:
            # Create a guest copy and change its parent so that the
            # operations inside prepare plugins on the guest use the
            # prepare step config rather than provision step config.
            guest_copy = copy.copy(guest)
            guest_copy.inject_logger(
                guest._logger.clone().apply_verbosity_options(**self._cli_options)
            )
            guest_copy.parent = self

            guest_copies.append(guest_copy)

        if guest_copies:
            sync_with_guests(
                self, 'push', PushTask(logger=self._logger, guests=guest_copies), self._logger
            )

            # To separate "push" from "prepare" queue visually
            self.info('')

        queue: PhaseQueue[PrepareStepData, PluginOutcome] = PhaseQueue(
            'prepare', self._logger.descend(logger_name=f'{self}.queue')
        )

        for prepare_phase in self.phases(classes=(Action, PreparePlugin)):
            if isinstance(prepare_phase, Action):
                queue.enqueue_action(phase=prepare_phase)

            elif prepare_phase.enabled_by_when:
                queue.enqueue_plugin(
                    phase=prepare_phase,  # type: ignore[arg-type]
                    guests=[
                        guest for guest in guest_copies if prepare_phase.enabled_on_guest(guest)
                    ],
                )

        results: list[PhaseResult] = []
        exceptions: list[Exception] = []

        def _record_exception(
            outcome: PluginTask[PrepareStepData, PluginOutcome], exc: Exception
        ) -> None:
            outcome.logger.fail(str(exc))

            exceptions.append(exc)

        for outcome in queue.run():
            if not isinstance(outcome.phase, PreparePlugin):
                continue

            # Possible outcomes: plugin crashed, raised an exception,
            # and that exception has been delivered to the top of the
            # phase's thread and propagated to us in the task outcome.
            #
            # Log the failure, save the exception, and add an error
            # result to represent the crash. Plugin did not return any
            # usable results, otherwise it would not have ended with
            # an exception...
            if outcome.exc:
                _record_exception(outcome, outcome.exc)

                results.append(
                    PhaseResult(
                        name=outcome.phase.name,
                        result=ResultOutcome.ERROR,
                        note=['Plugin raised an unhandled exception.'],
                    )
                )

                continue

            # Or, plugin finished successfully - not necessarily after
            # achieving its goals successfully. Save results, and if
            # plugin returned also some exceptions, do the same as above:
            # log them and save them, but do not emit any special result.
            # Plugin was alive till the very end, and returned results.
            if outcome.result:
                results += outcome.result.results

                if outcome.result.exceptions:
                    for exc in outcome.result.exceptions:
                        _record_exception(outcome, exc)

                    continue

            self.preparations_applied += 1

        self._save_results(results)

        if exceptions:
            # TODO: needs a better message...
            raise tmt.utils.PrepareError(
                'prepare step failed',
                causes=exceptions,
            )

        self.info('')

        # Pull artifacts created in the plan data directory
        # if there was at least one plugin executed
        if self.phases() and guest_copies:
            sync_with_guests(
                self,
                'pull',
                PullTask(
                    logger=self._logger, guests=guest_copies, source=self.plan.data_directory
                ),
                self._logger,
            )

            # To separate "prepare" from "pull" queue visually
            self.info('')

        # Give a summary, update status and save
        self.summary()
        self.status('done')
        self.save()

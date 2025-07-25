import asyncio
import dataclasses
import datetime
import importlib.metadata
import logging
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from functools import wraps
from typing import Any, Callable, Optional, TypedDict, Union, cast

import packaging.version

import tmt
import tmt.config
import tmt.hardware
import tmt.log
import tmt.options
import tmt.steps
import tmt.steps.provision
import tmt.utils
import tmt.utils.signals
import tmt.utils.wait
from tmt.config.models.hardware import MrackTranslation
from tmt.container import container, field, simple_field
from tmt.utils import (
    Command,
    Path,
    ProvisionError,
    ShellScript,
    UpdatableMessage,
)
from tmt.utils.templates import render_template
from tmt.utils.wait import Deadline, Waiting

MRACK_VERSION: Optional[str] = None

mrack: Any
providers: Any
ProvisioningError: Any
NotAuthenticatedError: Any
BEAKER: Any
BeakerProvider: Any
BeakerTransformer: Any
TmtBeakerTransformer: Any

_MRACK_IMPORTED: bool = False

DEFAULT_ARCH = 'x86_64'
DEFAULT_IMAGE = 'fedora'
DEFAULT_PROVISION_TIMEOUT = 3600  # 1 hour timeout at least
DEFAULT_PROVISION_TICK = 60  # poll job each minute

#: How often Beaker session should be refreshed to pick up up-to-date
#: Kerberos ticket.
DEFAULT_API_SESSION_REFRESH = 3600


def mrack_constructs_ks_pre() -> bool:
    """
    Kickstart construction has been improved in 1.21.0
    """

    assert MRACK_VERSION is not None

    return packaging.version.Version(MRACK_VERSION) >= packaging.version.Version('1.21.0')


def _get_constraint_translations(logger: tmt.log.Logger) -> list[MrackTranslation]:
    """
    Load the list of hardware requirement translations from configuration.

    :returns: translations loaded from configuration, or an empty list if
        the configuration is missing.
    """
    config = tmt.config.Config(logger)
    return (
        config.hardware.beaker.translations if config.hardware and config.hardware.beaker else []
    )


# Type annotation for "data" package describing a guest instance. Passed
# between load() and save() calls


class GuestInspectType(TypedDict):
    status: str
    system: str
    address: Optional[str]


# Mapping of HW requirement operators to their Beaker representation.
OPERATOR_SIGN_TO_OPERATOR = {
    tmt.hardware.Operator.EQ: '==',
    tmt.hardware.Operator.NEQ: '!=',
    tmt.hardware.Operator.GT: '>',
    tmt.hardware.Operator.GTE: '>=',
    tmt.hardware.Operator.LT: '<',
    tmt.hardware.Operator.LTE: '<=',
}


def operator_to_beaker_op(operator: tmt.hardware.Operator, value: str) -> tuple[str, str, bool]:
    """
    Convert constraint operator to Beaker "op".

    :param operator: operator to convert.
    :param value: value operator works with. It shall be a string representation
        of the the constraint value, as converted for the Beaker job XML.
    :returns: tuple of three items: Beaker operator, fit for ``op`` attribute
        of XML filters, a value to go with it instead of the input one, and
        a boolean signalizing whether the filter, constructed by the caller,
        should be negated.
    """

    if operator in OPERATOR_SIGN_TO_OPERATOR:
        return OPERATOR_SIGN_TO_OPERATOR[operator], value, False

    # MATCH has special handling - convert the pattern to a wildcard form -
    # and that may be weird :/
    if operator == tmt.hardware.Operator.MATCH:
        return 'like', value.replace('.*', '%').replace('.+', '%'), False

    if operator == tmt.hardware.Operator.NOTMATCH:
        return 'like', value.replace('.*', '%').replace('.+', '%'), True

    raise ProvisionError(f"Hardware requirement operator '{operator}' is not supported.")


# Transcription of our HW constraints into Mrack's own representation. It's based
# on dictionaries, and it's slightly weird. There is no distinction between elements
# that do not have attributes, like <and/>, and elements that must have them, like
# <memory/> and other binary operations. Also, there is no distinction between
# element attribute and child element, both are specified as dictionary key, just
# the former would be a string, the latter another, nested, dictionary.
#
# This makes it harder for us to enforce correct structure of the transcribed tree.
# Therefore adding a thin layer of containers that describe what Mrack is willing
# to accept, but with strict type annotations; the layer is aware of how to convert
# its components into dictionaries.
@container
class MrackBaseHWElement:
    """
    Base for Mrack hardware requirement elements
    """

    # Only a name is defined, as it's the only property shared across all element
    # types.
    name: str

    def to_mrack(self) -> dict[str, Any]:
        """
        Convert the element to Mrack-compatible dictionary tree
        """

        raise NotImplementedError


@container
class MrackHWElement(MrackBaseHWElement):
    """
    An element with name and attributes.

    This type of element is not allowed to have any child elements.
    """

    attributes: dict[str, str] = simple_field(default_factory=dict)

    def to_mrack(self) -> dict[str, Any]:
        return {self.name: self.attributes}


@container(init=False)
class MrackHWBinOp(MrackHWElement):
    """
    An element describing a binary operation, a "check"
    """

    def __init__(self, name: str, operator: str, value: str) -> None:
        super().__init__(name)

        self.attributes = {'_op': operator, '_value': value}


@container(init=False)
class MrackHWKeyValue(MrackHWElement):
    """
    A key-value element
    """

    def __init__(self, name: str, operator: str, value: str) -> None:
        super().__init__('key_value')

        self.attributes = {'_key': name, '_op': operator, '_value': value}


BeakerizedConstraint = Union[MrackBaseHWElement, dict[str, Any]]


@container
class MrackHWGroup(MrackBaseHWElement):
    """
    An element with child elements.

    This type of element is not allowed to have any attributes.
    """

    children: list[BeakerizedConstraint] = simple_field(default_factory=list)

    def to_mrack(self) -> dict[str, Any]:
        # Another unexpected behavior of mrack dictionary tree: if there is just
        # a single child, it is "packed" into its parent as a key/dict item.
        if (
            len(self.children) == 1
            and self.name not in ('and', 'or')
            and not isinstance(self.children[0], dict)
        ):
            return {self.name: self.children[0].to_mrack()}

        return {
            self.name: [
                child.to_mrack() if isinstance(child, MrackBaseHWElement) else child
                for child in self.children
            ]
        }


@container
class MrackHWAndGroup(MrackHWGroup):
    """
    Represents ``<and/>`` element
    """

    name: str = 'and'


@container
class MrackHWOrGroup(MrackHWGroup):
    """
    Represents ``<or/>`` element
    """

    name: str = 'or'


@container
class MrackHWNotGroup(MrackHWGroup):
    """
    Represents ``<not/>`` element
    """

    name: str = 'not'


def _transform_unsupported(constraint: tmt.hardware.Constraint) -> dict[str, Any]:
    # We have to return something, return an {}- no harm done, composable with other elements.

    return {}


def _translate_constraint_by_config(
    constraint: tmt.hardware.Constraint,
    logger: tmt.log.Logger,
) -> dict[str, Any]:
    """
    Translate hardware constraints to Mrack-compatible dictionary tree with config.
    """

    config = _get_constraint_translations(logger)

    if not config:
        return _transform_unsupported(constraint)

    suitable_translations = [
        translation
        for translation in config
        if translation.requirement == constraint.printable_name
    ]

    if not suitable_translations:
        return _transform_unsupported(constraint)

    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return tmt.utils.yaml_to_dict(
        render_template(
            suitable_translations[0].template,
            CONSTRAINT=constraint,
            BEAKER_OPERATOR=beaker_operator,
            BEAKER_VALUE=actual_value,
            BEAKER_NEGATE=negate,
        )
    )


def _translate_constraint_by_transformer(
    constraint: tmt.hardware.Constraint,
    logger: tmt.log.Logger,
) -> BeakerizedConstraint:
    """
    Translate hardware constraints to Mrack-compatible dictionary tree with transformer.
    """

    name, _, child_name = constraint.expand_name()

    if child_name:
        transformer = _CONSTRAINT_TRANSFORMERS.get(f'{name}.{child_name}')

    else:
        transformer = _CONSTRAINT_TRANSFORMERS.get(name)
    if not transformer:
        return _transform_unsupported(constraint)
    return transformer(constraint, logger)


def _transform_beaker_pool(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(constraint.operator, constraint.value)

    return MrackHWBinOp('pool', beaker_operator, actual_value)


def _transform_cpu_family(
    constraint: tmt.hardware.IntegerConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup('cpu', children=[MrackHWBinOp('family', beaker_operator, actual_value)])


def _transform_cpu_flag(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator = (
        OPERATOR_SIGN_TO_OPERATOR[tmt.hardware.Operator.EQ]
        if constraint.operator is tmt.hardware.Operator.CONTAINS
        else OPERATOR_SIGN_TO_OPERATOR[tmt.hardware.Operator.NEQ]
    )
    actual_value = str(constraint.value)

    return MrackHWGroup('cpu', children=[MrackHWBinOp('flag', beaker_operator, actual_value)])


def _transform_cpu_model(
    constraint: tmt.hardware.IntegerConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup('cpu', children=[MrackHWBinOp('model', beaker_operator, actual_value)])


def _transform_cpu_processors(
    constraint: tmt.hardware.IntegerConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup(
        'cpu', children=[MrackHWBinOp('processors', beaker_operator, actual_value)]
    )


def _transform_cpu_cores(
    constraint: tmt.hardware.IntegerConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup('cpu', children=[MrackHWBinOp('cores', beaker_operator, actual_value)])


def _transform_cpu_model_name(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, constraint.value
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'cpu', children=[MrackHWBinOp('model_name', beaker_operator, actual_value)]
                )
            ]
        )

    return MrackHWGroup(
        'cpu', children=[MrackHWBinOp('model_name', beaker_operator, actual_value)]
    )


def _transform_cpu_frequency(
    constraint: tmt.hardware.NumberConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(float(constraint.value.to('MHz').magnitude))
    )

    return MrackHWGroup('cpu', children=[MrackHWBinOp('speed', beaker_operator, actual_value)])


def _transform_cpu_stepping(
    constraint: tmt.hardware.IntegerConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup('cpu', children=[MrackHWBinOp('stepping', beaker_operator, actual_value)])


def _transform_cpu_vendor_name(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'cpu', children=[MrackHWBinOp('vendor', beaker_operator, actual_value)]
                )
            ]
        )

    return MrackHWGroup('cpu', children=[MrackHWBinOp('vendor', beaker_operator, actual_value)])


def _transform_cpu_hyper_threading(
    constraint: tmt.hardware.FlagConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup('cpu', children=[MrackHWBinOp('hyper', beaker_operator, actual_value)])


def _transform_disk_driver(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, constraint.value
    )

    if negate:
        return MrackHWNotGroup(
            children=[MrackHWKeyValue('BOOTDISK', beaker_operator, actual_value)]
        )

    return MrackHWKeyValue('BOOTDISK', beaker_operator, actual_value)


def _transform_disk_size(
    constraint: tmt.hardware.SizeConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(int(constraint.value.to('B').magnitude))
    )

    return MrackHWGroup('disk', children=[MrackHWBinOp('size', beaker_operator, actual_value)])


def _transform_disk_model_name(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, constraint.value
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'disk', children=[MrackHWBinOp('model', beaker_operator, actual_value)]
                )
            ]
        )

    return MrackHWGroup('disk', children=[MrackHWBinOp('model', beaker_operator, actual_value)])


def _transform_disk_physical_sector_size(
    constraint: tmt.hardware.SizeConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup(
        'disk', children=[MrackHWBinOp('phys_sector_size', beaker_operator, actual_value)]
    )


def _transform_disk_logical_sector_size(
    constraint: tmt.hardware.SizeConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup(
        'disk', children=[MrackHWBinOp('sector_size', beaker_operator, actual_value)]
    )


def _transform_hostname(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, constraint.value
    )

    if negate:
        return MrackHWNotGroup(children=[MrackHWBinOp('hostname', beaker_operator, actual_value)])

    return MrackHWBinOp('hostname', beaker_operator, actual_value)


def _transform_memory(
    constraint: tmt.hardware.SizeConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(int(constraint.value.to('MiB').magnitude))
    )

    return MrackHWGroup('system', children=[MrackHWBinOp('memory', beaker_operator, actual_value)])


def _transform_tpm_version(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(constraint.operator, constraint.value)

    return MrackHWKeyValue('TPM', beaker_operator, actual_value)


def _transform_virtualization_is_virtualized(
    constraint: tmt.hardware.FlagConstraint, logger: tmt.log.Logger
) -> BeakerizedConstraint:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    test = (constraint.operator, constraint.value)

    if test in [(tmt.hardware.Operator.EQ, True), (tmt.hardware.Operator.NEQ, False)]:
        return MrackHWGroup('system', children=[MrackHWBinOp('hypervisor', '!=', '')])

    if test in [(tmt.hardware.Operator.EQ, False), (tmt.hardware.Operator.NEQ, True)]:
        return MrackHWGroup('system', children=[MrackHWBinOp('hypervisor', '==', '')])

    return _transform_unsupported(constraint)


def _transform_virtualization_hypervisor(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'system', children=[MrackHWBinOp('hypervisor', beaker_operator, actual_value)]
                )
            ]
        )

    return MrackHWGroup(
        'system', children=[MrackHWBinOp('hypervisor', beaker_operator, actual_value)]
    )


def _transform_zcrypt_adapter(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, constraint.value
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'system',
                    children=[MrackHWKeyValue('ZCRYPT_MODEL', beaker_operator, actual_value)],
                )
            ]
        )

    return MrackHWGroup(
        'system', children=[MrackHWKeyValue('ZCRYPT_MODEL', beaker_operator, actual_value)]
    )


def _transform_zcrypt_mode(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, constraint.value
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'system',
                    children=[MrackHWKeyValue('ZCRYPT_MODE', beaker_operator, actual_value)],
                )
            ]
        )

    return MrackHWGroup(
        'system', children=[MrackHWKeyValue('ZCRYPT_MODE', beaker_operator, actual_value)]
    )


def _transform_iommu_is_supported(
    constraint: tmt.hardware.FlagConstraint, logger: tmt.log.Logger
) -> BeakerizedConstraint:
    test = (constraint.operator, constraint.value)

    if test in [(tmt.hardware.Operator.EQ, True), (tmt.hardware.Operator.NEQ, False)]:
        return MrackHWKeyValue('VIRT_IOMMU', '==', '1')

    if test in [(tmt.hardware.Operator.EQ, False), (tmt.hardware.Operator.NEQ, True)]:
        return MrackHWKeyValue('VIRT_IOMMU', '==', '0')

    return _transform_unsupported(constraint)


def _transform_location_lab_controller(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    if constraint.operator not in [tmt.hardware.Operator.EQ, tmt.hardware.Operator.NEQ]:
        raise ProvisionError(
            f"Cannot apply hardware requirement '{constraint}', operator not supported."
        )
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, constraint.value
    )

    if negate:
        return MrackHWNotGroup(
            children=[MrackHWBinOp('labcontroller', beaker_operator, actual_value)]
        )

    return MrackHWBinOp('labcontroller', beaker_operator, actual_value)


def _transform_system_numa_nodes(
    constraint: tmt.hardware.IntegerConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, _ = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    return MrackHWGroup(
        'system', children=[MrackHWBinOp('numanodes', beaker_operator, actual_value)]
    )


def _transform_system_model_name(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'system', children=[MrackHWBinOp('model', beaker_operator, actual_value)]
                )
            ]
        )

    return MrackHWGroup('system', children=[MrackHWBinOp('model', beaker_operator, actual_value)])


def _transform_system_vendor_name(
    constraint: tmt.hardware.TextConstraint, logger: tmt.log.Logger
) -> MrackBaseHWElement:
    beaker_operator, actual_value, negate = operator_to_beaker_op(
        constraint.operator, str(constraint.value)
    )

    if negate:
        return MrackHWNotGroup(
            children=[
                MrackHWGroup(
                    'system', children=[MrackHWBinOp('vendor', beaker_operator, actual_value)]
                )
            ]
        )

    return MrackHWGroup('system', children=[MrackHWBinOp('vendor', beaker_operator, actual_value)])


ConstraintTransformer = Callable[[tmt.hardware.Constraint, tmt.log.Logger], MrackBaseHWElement]

_CONSTRAINT_TRANSFORMERS: Mapping[str, ConstraintTransformer] = {
    'beaker.pool': _transform_beaker_pool,  # type: ignore[dict-item]
    'cpu.cores': _transform_cpu_cores,  # type: ignore[dict-item]
    'cpu.family': _transform_cpu_family,  # type: ignore[dict-item]
    'cpu.flag': _transform_cpu_flag,  # type: ignore[dict-item]
    'cpu.frequency': _transform_cpu_frequency,  # type: ignore[dict-item]
    'cpu.hyper_threading': _transform_cpu_hyper_threading,  # type: ignore[dict-item]
    'cpu.model': _transform_cpu_model,  # type: ignore[dict-item]
    'cpu.model_name': _transform_cpu_model_name,  # type: ignore[dict-item]
    'cpu.processors': _transform_cpu_processors,  # type: ignore[dict-item]
    'cpu.stepping': _transform_cpu_stepping,  # type: ignore[dict-item]
    'cpu.vendor_name': _transform_cpu_vendor_name,  # type: ignore[dict-item]
    'disk.driver': _transform_disk_driver,  # type: ignore[dict-item]
    'disk.model_name': _transform_disk_model_name,  # type: ignore[dict-item]
    'disk.size': _transform_disk_size,  # type: ignore[dict-item]
    'disk.physical_sector_size': _transform_disk_physical_sector_size,  # type: ignore[dict-item]
    'disk.logical_sector_size': _transform_disk_logical_sector_size,  # type: ignore[dict-item]
    'hostname': _transform_hostname,  # type: ignore[dict-item]
    'location.lab_controller': _transform_location_lab_controller,  # type: ignore[dict-item]
    'memory': _transform_memory,  # type: ignore[dict-item]
    'tpm.version': _transform_tpm_version,  # type: ignore[dict-item]
    'virtualization.is_virtualized': _transform_virtualization_is_virtualized,  # type: ignore[dict-item]
    'virtualization.hypervisor': _transform_virtualization_hypervisor,  # type: ignore[dict-item]
    'zcrypt.adapter': _transform_zcrypt_adapter,  # type: ignore[dict-item]
    'zcrypt.mode': _transform_zcrypt_mode,  # type: ignore[dict-item]
    'system.numa_nodes': _transform_system_numa_nodes,  # type: ignore[dict-item]
    'system.model_name': _transform_system_model_name,  # type: ignore[dict-item]
    'system.vendor_name': _transform_system_vendor_name,  # type: ignore[dict-item]
    'iommu.is_supported': _transform_iommu_is_supported,  # type: ignore[dict-item]
}


def constraint_to_beaker_filter(
    constraint: tmt.hardware.BaseConstraint, logger: tmt.log.Logger
) -> BeakerizedConstraint:
    """
    Convert a hardware constraint into a Mrack-compatible filter
    """

    if isinstance(constraint, tmt.hardware.And):
        return MrackHWAndGroup(
            children=[
                constraint_to_beaker_filter(child_constraint, logger)
                for child_constraint in constraint.constraints
            ]
        )

    if isinstance(constraint, tmt.hardware.Or):
        return MrackHWOrGroup(
            children=[
                constraint_to_beaker_filter(child_constraint, logger)
                for child_constraint in constraint.constraints
            ]
        )

    assert isinstance(constraint, tmt.hardware.Constraint)

    transformed = _translate_constraint_by_config(
        constraint, logger
    ) or _translate_constraint_by_transformer(constraint, logger)

    if not transformed:
        # Unsupported constraint has been already logged via report_support().
        # Make sure user is aware it would have no effect.
        logger.warning(f"Hardware requirement '{constraint.printable_name}' will have no effect.")
    return transformed


def import_and_load_mrack_deps(workdir: Any, name: str, logger: tmt.log.Logger) -> None:
    """
    Import mrack module only when needed
    """

    global _MRACK_IMPORTED

    if _MRACK_IMPORTED:
        return

    global MRACK_VERSION
    global mrack
    global providers
    global ProvisioningError
    global NotAuthenticatedError
    global BEAKER
    global BeakerProvider
    global BeakerTransformer
    global TmtBeakerTransformer

    try:
        import mrack
        from mrack.errors import NotAuthenticatedError, ProvisioningError
        from mrack.providers import providers
        from mrack.providers.beaker import PROVISIONER_KEY as BEAKER
        from mrack.providers.beaker import BeakerProvider
        from mrack.transformers.beaker import BeakerTransformer

        MRACK_VERSION = importlib.metadata.version('mrack')

        # hack: remove mrack stdout and move the logfile to /tmp
        mrack.logger.removeHandler(mrack.console_handler)
        mrack.logger.removeHandler(mrack.file_handler)

        with suppress(OSError):
            os.remove("mrack.log")

        logging.FileHandler(str(f"{workdir}/{name}-mrack.log"))

        providers.register(BEAKER, BeakerProvider)

    except ImportError:
        raise ProvisionError("Install 'tmt+provision-beaker' to provision using this method.")

    # ignore the misc because mrack sources are not typed and result into
    # error: Class cannot subclass "BeakerTransformer" (has type "Any")
    # as mypy does not have type information for the BeakerTransformer class
    class TmtBeakerTransformer(BeakerTransformer):  # type: ignore[misc]
        def _translate_tmt_hw(self, hw: tmt.hardware.Hardware) -> dict[str, Any]:
            """
            Return hw requirements from given hw dictionary
            """

            assert hw.constraint
            # Beaker, unlike instance-type-based infrastructures like AWS, does
            # have the actual filtering, and can express `or` and `and`
            # groups. And our `constraint_to_beaker_filter()` does that,
            # even for groups nested deeper in the tree.
            transformed = constraint_to_beaker_filter(hw.constraint, logger)
            if isinstance(transformed, MrackBaseHWElement):
                transformed = transformed.to_mrack()

            logger.debug('Transformed hardware', tmt.utils.dict_to_yaml(transformed))

            # Mrack does not handle well situation when the filter
            # consists of just a single filtering element, e.g. just
            # `hostname`. In that case, the element is converted into
            # XML element incorrectly. Therefore wrapping our filter
            # with `<and/>` group, even if it has just a single child,
            # it works around the problem.
            # See https://github.com/teemtee/tmt/issues/3442
            return {'hostRequires': MrackHWAndGroup(children=[transformed]).to_mrack()}

        def create_host_requirement(self, host: CreateJobParameters) -> dict[str, Any]:
            """
            Create single input for Beaker provisioner
            """

            req: dict[str, Any] = super().create_host_requirement(host.to_mrack())

            if host.hardware and host.hardware.constraint:
                req.update(self._translate_tmt_hw(host.hardware))

            if host.beaker_job_owner:
                req['job_owner'] = host.beaker_job_owner

            if host.kickstart:
                if 'kernel-options' in host.kickstart:
                    req['kernel_options'] = host.kickstart['kernel-options']

                if 'kernel-options-post' in host.kickstart:
                    req['kernel_options_post'] = host.kickstart['kernel-options-post']

                if not mrack_constructs_ks_pre():
                    ks_components: list[str] = [
                        host.kickstart[ks_section]
                        for ks_section in ('pre-install', 'script', 'post-install')
                        if ks_section in host.kickstart
                    ]

                    if ks_components:
                        req['ks_append'] = ['\n'.join(ks_components)]

            # Whiteboard must be added *after* request preparation, to overwrite the default one.
            req['whiteboard'] = host.whiteboard

            logger.debug('mrack request', req, level=4)

            logger.info('whiteboard', host.whiteboard, 'green')

            return req

    _MRACK_IMPORTED = True


def async_run(func: Any) -> Any:
    """
    Decorate click actions to run as async
    """

    @wraps(func)
    def update_wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(func(*args, **kwargs))

    return update_wrapper


@container
class BeakerGuestData(tmt.steps.provision.GuestSshData):
    # Guest request properties
    whiteboard: Optional[str] = field(
        default=None,
        option=('-w', '--whiteboard'),
        metavar='WHITEBOARD',
        help='Text description of the beaker job which is displayed in the list of jobs.',
    )
    arch: str = field(
        default=DEFAULT_ARCH,
        option='--arch',
        metavar='ARCH',
        help='Architecture to provision.',
    )
    image: Optional[str] = field(
        default=DEFAULT_IMAGE,
        option=('-i', '--image'),
        metavar='COMPOSE',
        help='Image (distro or "compose" in Beaker terminology) to provision.',
    )

    # Provided in Beaker job
    job_id: Optional[str] = field(
        default=None,
        internal=True,
    )

    # Timeouts and deadlines
    provision_timeout: int = field(
        default=DEFAULT_PROVISION_TIMEOUT,
        option='--provision-timeout',
        metavar='SECONDS',
        help=f"""
             How long to wait for provisioning to complete,
             {DEFAULT_PROVISION_TIMEOUT} seconds by default.
             """,
        normalize=tmt.utils.normalize_int,
    )
    provision_tick: int = field(
        default=DEFAULT_PROVISION_TICK,
        option='--provision-tick',
        metavar='SECONDS',
        help=f"""
             How often check Beaker for provisioning status,
             {DEFAULT_PROVISION_TICK} seconds by default.
             """,
        normalize=tmt.utils.normalize_int,
    )
    api_session_refresh_tick: int = field(
        default=DEFAULT_API_SESSION_REFRESH,
        option='--api-session-refresh-tick',
        metavar='SECONDS',
        help=f"""
             How often should Beaker session be refreshed to pick up-to-date Kerberos ticket,
             {DEFAULT_API_SESSION_REFRESH} seconds by default.
             """,
        normalize=tmt.utils.normalize_int,
    )
    kickstart: dict[str, str] = field(
        default_factory=dict,
        option='--kickstart',
        metavar='KEY=VALUE',
        help='Optional Beaker kickstart to use when provisioning the guest.',
        multiple=True,
        normalize=tmt.utils.normalize_string_dict,
    )

    beaker_job_owner: Optional[str] = field(
        default=None,
        option='--beaker-job-owner',
        metavar='USERNAME',
        help="""
             If set, Beaker jobs will be submitted on behalf of ``USERNAME``.
             Submitting user must be a submission delegate for the ``USERNAME``.
             """,
    )

    public_key: list[str] = field(
        default_factory=list,
        option='--public-key',
        metavar='PUBKEY',
        help="""
             Public keys to add among authorized SSH keys.
             """,
        multiple=True,
        normalize=tmt.utils.normalize_string_list,
    )

    beaker_job_group: Optional[str] = field(
        default=None,
        option='--beaker-job-group',
        metavar='GROUPNAME',
        help="""
             If set, Beaker jobs will be submitted on behalf of ``GROUPNAME``.
             """,
    )


@container
class ProvisionBeakerData(BeakerGuestData, tmt.steps.provision.ProvisionStepData):
    pass


GUEST_STATE_COLOR_DEFAULT = 'green'

GUEST_STATE_COLORS = {
    "New": "blue",
    "Scheduled": "blue",
    "Queued": "cyan",
    "Processed": "cyan",
    "Waiting": "magenta",
    "Installing": "magenta",
    "Running": "magenta",
    "Cancelled": "yellow",
    "Aborted": "yellow",
    "Reserved": "green",
    "Completed": "green",
}


@container
class CreateJobParameters:
    """
    Collect all parameters for a future Beaker job
    """

    tmt_name: str
    name: str
    os: str
    arch: str
    hardware: Optional[tmt.hardware.Hardware]
    kickstart: dict[str, str]
    whiteboard: Optional[str]
    beaker_job_owner: Optional[str]
    public_key: list[str]
    group: Optional[str]

    def to_mrack(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)

        data['beaker'] = {}

        if self.kickstart:
            kickstart = self.kickstart.copy()

            if 'metadata' in kickstart:
                data['beaker']['ks_meta'] = kickstart.pop('metadata')

            # Mrack does not handle metadata-only kickstart nicely, ends
            # up with just an empty string. Don't tempt it, don't let it
            # see kickstart if it was just metadata.
            if kickstart:
                data['beaker']['ks_append'] = kickstart
        if self.public_key:
            data['beaker']['pubkeys'] = self.public_key

        return data


class BeakerAPI:
    # req is a requirement passed to Beaker mrack provisioner
    mrack_requirement: dict[str, Any] = {}
    dsp_name: str = "Beaker"

    # wrapping around the __init__ with async wrapper does mangle the method
    # and mypy complains as it no longer returns None but the coroutine
    @async_run
    async def __init__(self, guest: 'GuestBeaker') -> None:  # type: ignore[misc]
        """
        Initialize the API class with defaults and load the config
        """

        self._guest = guest

        # use global context class
        global_context = mrack.context.global_context

        mrack_config_locations = [
            Path(__file__).parent / "mrack/mrack.conf",
            Path("/etc/tmt/mrack.conf"),
            Path("~/.mrack/mrack.conf").expanduser(),
            Path.cwd() / "mrack.conf",
        ]

        mrack_config: Optional[Path] = None

        for potential_location in mrack_config_locations:
            if potential_location.exists():
                mrack_config = potential_location

        if not mrack_config:
            raise ProvisionError("Configuration file 'mrack.conf' not found.")

        try:
            global_context.init(str(mrack_config))
        except mrack.errors.ConfigError as mrack_conf_err:
            raise ProvisionError(mrack_conf_err)

        self._mrack_transformer = TmtBeakerTransformer()
        try:
            await self._mrack_transformer.init(global_context.PROV_CONFIG, {})
        except NotAuthenticatedError as kinit_err:
            raise ProvisionError(kinit_err) from kinit_err
        except AttributeError as hub_err:
            raise ProvisionError(
                f"Can not use current kerberos ticket to authenticate: {hub_err}"
            ) from hub_err
        except FileNotFoundError as missing_conf_err:
            raise ProvisionError(
                f"Configuration file missing: {missing_conf_err.filename}"
            ) from missing_conf_err

        self._mrack_provider = self._mrack_transformer._provider
        self._mrack_provider.poll_sleep = DEFAULT_PROVISION_TICK

        if guest.job_id:
            self._bkr_job_id = guest.job_id

    @async_run
    async def create(self, data: CreateJobParameters) -> Any:
        """
        Create - or request creation of - a resource using mrack up.

        :param data: describes the provisioning request.
        """

        mrack_requirement = self._mrack_transformer.create_host_requirement(data)
        log_msg_start = f"{self.dsp_name} [{self.mrack_requirement.get('name')}]"
        self._bkr_job_id, self._req = await self._mrack_provider.create_server(mrack_requirement)
        return self._mrack_provider._get_recipe_info(self._bkr_job_id, log_msg_start)

    @async_run
    async def inspect(
        self,
    ) -> Any:
        """
        Inspect a resource (kinda wait till provisioned)
        """

        log_msg_start = f"{self.dsp_name} [{self.mrack_requirement.get('name')}]"
        return self._mrack_provider._get_recipe_info(self._bkr_job_id, log_msg_start)

    @async_run
    async def delete(  # destroy
        self,
    ) -> Any:
        """
        Delete - or request removal of - a resource
        """

        return await self._mrack_provider.delete_host(self._bkr_job_id, None)


class GuestBeaker(tmt.steps.provision.GuestSsh):
    """
    Beaker guest instance
    """

    _data_class = BeakerGuestData

    # Guest request properties
    whiteboard: Optional[str]
    arch: str
    image: str = "fedora-latest"
    hardware: Optional[tmt.hardware.Hardware] = None
    kickstart: dict[str, str]

    beaker_job_owner: Optional[str] = None
    beaker_job_group: Optional[str] = None

    # Provided in Beaker response
    job_id: Optional[str]

    # Timeouts and deadlines
    provision_timeout: int
    provision_tick: int
    public_key: list[str]
    api_session_refresh_tick: int

    _api: Optional[BeakerAPI] = None
    _api_timestamp: Optional[datetime.datetime] = None

    def __init__(self, *args: Any, **kwargs: Any):
        """
        Make sure that the mrack module is available and imported
        """

        super().__init__(*args, **kwargs)

        assert isinstance(self.parent, tmt.steps.provision.Provision)
        import_and_load_mrack_deps(self.parent.workdir, self.parent.name, self._logger)

    @property
    def api(self) -> BeakerAPI:
        """
        Create BeakerAPI leveraging mrack
        """

        def _construct_api() -> tuple[BeakerAPI, datetime.datetime]:
            return BeakerAPI(self), datetime.datetime.now(datetime.timezone.utc)

        if self._api is None:
            self._api, self._api_timestamp = _construct_api()

        else:
            assert self._api_timestamp is not None

            delta = datetime.datetime.now(datetime.timezone.utc) - self._api_timestamp

            if delta.total_seconds() >= self.api_session_refresh_tick:
                self.debug(f'Refresh Beaker API client as it is too old, {delta}.')

                self._api, self._api_timestamp = _construct_api()

        return self._api

    @property
    def is_ready(self) -> bool:
        """
        Check if provisioning of machine is done
        """

        if self.job_id is None:
            return False

        assert mrack is not None

        try:
            response = self.api.inspect()

            if response["status"] == "Aborted":
                return False

            current = cast(GuestInspectType, response)
            state = current["status"]
            if state in {"Error, Aborted", "Cancelled"}:
                return False

            if state == 'Reserved':
                return True
            return False

        except mrack.errors.MrackError:
            return False

    def _create(self, tmt_name: str) -> None:
        """
        Create beaker job xml request and submit it to Beaker hub
        """

        data = CreateJobParameters(
            tmt_name=tmt_name,
            hardware=self.hardware,
            kickstart=self.kickstart,
            arch=self.arch,
            os=self.image,
            name=f'{self.image}-{self.arch}',
            whiteboard=self.whiteboard or tmt_name,
            beaker_job_owner=self.beaker_job_owner,
            public_key=self.public_key,
            group=self.beaker_job_group,
        )

        if self.is_dry_run:
            mrack_requirement = self.api._mrack_transformer.create_host_requirement(data)
            job = self.api._mrack_provider._req_to_bkr_job(mrack_requirement)

            # Mrack indents XML with tabs, tmt indents with spaces, let's make sure we
            # don't mix these two in tmt output & modify mrack's output to use spaces as well.
            self.print(
                re.sub(
                    r'^\t+',
                    lambda match: '  ' * len(match.group()),
                    job.toxml(prettyxml=True),
                    flags=re.MULTILINE,
                ),
            )
            return

        with tmt.utils.signals.PreventSignals(self._logger):
            try:
                response = self.api.create(data)

            except ProvisioningError as exc:
                import xmlrpc.client

                cause = exc.__cause__

                if isinstance(cause, xmlrpc.client.Fault):
                    if 'is not a valid user name' in cause.faultString:
                        raise ProvisionError(
                            f"Failed to create Beaker job, job owner '{self.beaker_job_owner}' "
                            "was refused as unknown."
                        ) from exc

                    if 'is not a valid submission delegate' in cause.faultString:
                        raise ProvisionError(
                            f"Failed to create Beaker job, job owner '{self.beaker_job_owner}' "
                            "is not a valid submission delegate."
                        ) from exc

                    if 'is not a valid group' in cause.faultString:
                        raise ProvisionError(
                            f"Failed to create Beaker job, job group '{self.beaker_job_group}' "
                            "was refused as unknown."
                        ) from exc

                    if 'is not a member of group' in cause.faultString:
                        raise ProvisionError(
                            "Failed to create Beaker job, submitting user is not "
                            "a member of group '{self.beaker_job_group}'"
                        ) from exc

                raise ProvisionError('Failed to create Beaker job') from exc

            if not response:
                raise ProvisionError(f"Failed to create, response: '{response}'.")

            self.info('guest', 'has been requested', 'green')
            self.job_id = f'J:{response["id"]}'

        self.info('job id', self.job_id, 'green')

        with UpdatableMessage("status", indent_level=self._level()) as progress_message:

            def get_new_state() -> GuestInspectType:
                response = self.api.inspect()

                if response["status"] == "Aborted":
                    raise ProvisionError(
                        f"Failed to create, unhandled API response '{response['status']}'."
                    )

                current = cast(GuestInspectType, response)
                state = current["status"]
                state_color = GUEST_STATE_COLORS.get(state, GUEST_STATE_COLOR_DEFAULT)

                progress_message.update(state, color=state_color)

                if state in {"Error, Aborted", "Cancelled"}:
                    raise ProvisionError('Failed to create, provisioning failed.')

                if state == 'Reserved':
                    for key in response.get('logs', []):
                        self.guest_logs.append(
                            GuestLogBeaker(key.replace('.log', ''), self, response["logs"][key])
                        )
                    # console.log contains dmesg, and accessible even when the system is dead.
                    if response.get('logs', []).get('console.log'):
                        self.guest_logs.append(
                            GuestLogBeaker('dmesg', self, response.get('logs').get('console.log'))
                        )
                    else:
                        self.warn('No console.log available.')
                    return current

                raise tmt.utils.wait.WaitingIncompleteError

            try:
                guest_info = Waiting(
                    Deadline.from_seconds(self.provision_timeout), tick=self.provision_tick
                ).wait(get_new_state, self._logger)

            except tmt.utils.wait.WaitingTimedOutError:
                response = self.api.delete()
                raise ProvisionError(
                    f'Failed to provision in the given amount '
                    f'of time (--provision-timeout={self.provision_timeout}).'
                )

        self.primary_address = self.topology_address = guest_info['system']

    def start(self) -> None:
        """
        Start the guest

        Get a new guest instance running. This should include preparing
        any configuration necessary to get it started. Called after
        load() is completed so all guest data should be available.
        """

        if self.job_id is None or self.primary_address is None:
            self._create(self._tmt_name())

        self.verbose('primary address', self.primary_address, 'green')
        self.verbose('topology address', self.topology_address, 'green')

    def remove(self) -> None:
        """
        Remove the guest
        """

        if self.job_id is None:
            return

        self.api.delete()

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

            Plugin will use ``bkr system-power`` command to trigger the
            hard reboot. Unlike ``command``, this command would be
            executed on the runner, **not** on the guest.
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

        waiting = waiting or tmt.steps.provision.default_reboot_waiting()

        if hard:
            self.debug("Hard reboot using the reboot command 'bkr system-power --action reboot'.")

            reboot_script = ShellScript(f'bkr system-power --action reboot {self.primary_address}')

            return self.perform_reboot(
                lambda: self._run_guest_command(reboot_script.to_shell_command()),
                waiting,
                fetch_boot_time=False,
            )

        return super().reboot(
            hard=False,
            command=command,
            waiting=waiting,
        )


@tmt.steps.provides_method('beaker')
class ProvisionBeaker(tmt.steps.provision.ProvisionPlugin[ProvisionBeakerData]):
    """
    Provision guest on Beaker system using mrack.

    Reserve a machine from the Beaker pool using the ``mrack``
    plugin. ``mrack`` is a multicloud provisioning library
    supporting multiple cloud services including Beaker.

    The following two files are used for configuration:

    ``/etc/tmt/mrack.conf`` for basic configuration

    ``/etc/tmt/provisioning-config.yaml`` configuration per supported provider

    Beaker installs distribution specified by the ``image``
    key. If the image can not be translated using the
    ``provisioning-config.yaml`` file mrack passes the image
    value to Beaker hub and tries to request distribution
    based on the image value. This way we can bypass default
    translations and use desired distribution specified like
    the one in the example below.

    Minimal configuration could look like this:

    .. code-block:: yaml

        provision:
            how: beaker
            image: fedora

    To trigger a hard reboot of a guest, ``bkr system-power --action reboot``
    command is executed.

    .. warning::

        ``bkr system-power`` command is executed on the runner, not
        on the guest.

    .. code-block:: yaml

        # Specify the distro directly
        provision:
            how: beaker
            image: Fedora-37%

    .. code-block:: yaml

        # Set custom whiteboard description (added in 1.30)
        provision:
            how: beaker
            whiteboard: Just a smoke test for now
    """

    _data_class = ProvisionBeakerData
    _guest_class = GuestBeaker

    # _thread_safe = True

    # Guest instance
    _guest = None

    # data argument should be a "Optional[GuestData]" type but we would like to use
    # BeakerGuestData created here ignoring the override will make mypy calm
    def wake(self, data: Optional[BeakerGuestData] = None) -> None:  # type: ignore[override]
        """
        Wake up the plugin, process data, apply options
        """

        super().wake(data=data)

        if data:
            self._guest = GuestBeaker(
                data=data,
                name=self.name,
                parent=self.step,
                logger=self._logger,
            )

    def go(self, *, logger: Optional[tmt.log.Logger] = None) -> None:
        """
        Provision the guest
        """

        super().go(logger=logger)

        data = BeakerGuestData.from_plugin(self)

        data.show(verbose=self.verbosity_level, logger=self._logger)

        if data.hardware:
            data.hardware.report_support(
                names=list(_CONSTRAINT_TRANSFORMERS.keys()), logger=self._logger
            )

        self._guest = GuestBeaker(
            data=data,
            name=self.name,
            parent=self.step,
            logger=self._logger,
        )
        self._guest.start()
        self._guest.setup()


@container
class GuestLogBeaker(tmt.steps.provision.GuestLog):
    guest: GuestBeaker
    url: str

    def fetch(self, logger: tmt.log.Logger) -> Optional[str]:
        """
        Fetch and return content of a log.

        :returns: content of the log, or ``None`` if the log cannot be retrieved.
        """
        try:
            return tmt.utils.get_url_content(self.url)
        except Exception as error:
            tmt.utils.show_exception_as_warning(
                exception=error,
                message=f"Failed to fetch '{self.url}' log.",
                logger=self.guest._logger,
            )

            return None

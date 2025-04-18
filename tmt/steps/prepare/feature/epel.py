from typing import Any, Optional

import tmt.log
import tmt.steps.prepare
import tmt.utils
from tmt.container import container, field
from tmt.steps.prepare.feature import PrepareFeatureData, ToggleableFeature, provides_feature
from tmt.steps.provision import Guest


@container
class EpelStepData(PrepareFeatureData):
    epel: Optional[str] = field(
        default=None,
        option='--epel',
        metavar='enabled|disabled',
        help='Whether EPEL repository should be installed & enabled or disabled.',
    )


@provides_feature('epel')
class Epel(ToggleableFeature):
    NAME = "epel"

    _data_class = EpelStepData

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    @classmethod
    def enable(cls, guest: Guest, logger: tmt.log.Logger) -> None:
        cls._run_playbook('enable', "epel-enable.yaml", guest, logger)

    @classmethod
    def disable(cls, guest: Guest, logger: tmt.log.Logger) -> None:
        cls._run_playbook('disable', "epel-disable.yaml", guest, logger)

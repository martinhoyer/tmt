from tmt.container import MetadataContainer


class MrackTranslation(MetadataContainer):
    """Configuration for MrackTranslation model."""

    requirement: str
    template: str


class MrackHardware(MetadataContainer):
    translations: list[MrackTranslation]


class HardwareConfig(MetadataContainer):
    beaker: MrackHardware

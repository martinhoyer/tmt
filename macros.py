import dataclasses
import importlib
from typing import Any, Optional

# For tmt.container.FieldMetadata, but we access it via f.metadata.get('tmt')


def get_class_from_string(class_path_str: str) -> Optional[type]:
    try:
        module_path, class_name = class_path_str.rsplit('.', 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except Exception as e:
        print(f"Error importing/introspecting class {class_path_str}: {e}")
        return None


def define_env(env: Any) -> None:
    """Hook function for mkdocs-macros."""

    @env.macro
    def render_plugin_options(
        class_path_str: str, inherited_from_path_str: Optional[str] = None
    ) -> str:
        data_class = get_class_from_string(class_path_str)
        if data_class is None:
            return f"<p>Error: Macro could not load class {class_path_str} due to an internal error (likely an import issue).</p>"  # noqa: E501

        inherited_fields = set()
        if inherited_from_path_str:
            inherited_from_class = get_class_from_string(inherited_from_path_str)
            if inherited_from_class is None:
                return f"<p>Error: Macro could not load inherited_from class {inherited_from_path_str} due to an internal error (likely an import issue).</p>"  # noqa: E501
            inherited_fields = {f.name for f in dataclasses.fields(inherited_from_class)}

        markdown_output = []
        fields = dataclasses.fields(data_class)

        for f_obj in fields:  # Renamed f to f_obj to avoid conflict with f-string
            if f_obj.name in inherited_fields:
                continue

            markdown_output.append(f"#### `{f_obj.name}`")

            type_str = str(f_obj.type)
            if 'typing.Optional[' in type_str:
                type_str = type_str.replace('typing.Optional[', 'Optional[').replace(
                    'NoneType', 'None'
                )
            type_str = type_str.replace('typing.', '')
            markdown_output.append(f"-   **Type:** `{type_str}`")

            tmt_field_meta = f_obj.metadata.get('tmt')
            description = 'No description available.'
            if tmt_field_meta and hasattr(tmt_field_meta, 'help') and tmt_field_meta.help:
                description = tmt_field_meta.help

            if description:
                description_md = (
                    description.strip().replace('\n', '<br/>').replace('\n\n', '<br/><br/>')
                )
                markdown_output.append(f"-   **Description:** {description_md}")

            if f_obj.default is not dataclasses.MISSING:
                markdown_output.append(f"-   **Default:** `{f_obj.default}`")
            elif f_obj.default_factory is not dataclasses.MISSING:
                try:
                    default_val = f_obj.default_factory()
                    markdown_output.append(f"-   **Default:** `{default_val}` (from factory)")
                except TypeError:
                    markdown_output.append("-   **Default:** (complex factory)")

            actual_cli_options = []
            if (
                tmt_field_meta
                and hasattr(tmt_field_meta, 'cli_option')
                and tmt_field_meta.cli_option
            ):
                if isinstance(tmt_field_meta.cli_option, str):
                    actual_cli_options.append(tmt_field_meta.cli_option)
                else:
                    actual_cli_options.extend(tmt_field_meta.cli_option)

            if actual_cli_options:
                markdown_output.append(
                    f"-   **CLI Options:** {', '.join(f'`{opt}`' for opt in actual_cli_options)}"
                )

            step_name_upper = "DISCOVER"
            class_name_for_plugin = data_class.__name__
            plugin_name_upper = (
                class_name_for_plugin.replace('StepData', '').upper()
                if 'StepData' in class_name_for_plugin
                else class_name_for_plugin.replace('Data', '').upper()
            )

            if 'fmf' in plugin_name_upper:
                plugin_name_upper = "fmf"
            elif 'SHELL' in plugin_name_upper:
                plugin_name_upper = "SHELL"

            env_var_name = f"TMT_PLUGIN_{step_name_upper}_{plugin_name_upper}_{f_obj.name.upper().replace('-', '_')}"  # noqa: E501
            if (
                "DISCOVERSTEP" in plugin_name_upper or data_class.__name__ == 'DiscoverStepData'
            ):  # Common keys for discover
                env_var_name = (
                    f"TMT_PLUGIN_{step_name_upper}_{f_obj.name.upper().replace('-', '_')}"
                )

            markdown_output.append(f"-   **Env Variable:** `{env_var_name}` (convention-based)")
            markdown_output.append("")

        return "\n".join(markdown_output)

# Discover Plugins

This page documents the available discover plugins and their configuration options.

## Common Keys

{{ render_plugin_options('tmt.steps.discover.DiscoverStepData') }}

## fmf Plugin

::: tmt.steps.discover.fmf.DiscoverFmf
    options:
      show_root_heading: false
      show_bases: true
      heading_level: 3

### fmf Configuration

{{ render_plugin_options('tmt.steps.discover.fmf.DiscoverFmfStepData', inherited_from_path_str='tmt.steps.discover.DiscoverStepData') }}

## Shell Plugin

::: tmt.steps.discover.shell.DiscoverShell
    options:
      show_root_heading: false
      show_bases: true
      heading_level: 3

### Shell Configuration
{# Assuming DiscoverShellData exists in tmt.steps.discover.shell #}
{{ render_plugin_options('tmt.steps.discover.shell.DiscoverShellData', inherited_from_path_str='tmt.steps.discover.DiscoverStepData') }}

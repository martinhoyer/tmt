# Discover Plugins

This page documents the available discover plugins and their configuration options.

## Common Keys

::: tmt.steps.discover.DiscoverStepData
    options:
      show_root_heading: true
      heading_level: 3
      show_bases: false
      show_source: false

## fmf Plugin

::: tmt.steps.discover.fmf.DiscoverFmf
    options:
      show_root_heading: false
      show_bases: true
      heading_level: 3

### fmf Configuration

::: tmt.steps.discover.fmf.DiscoverFmfStepData
    options:
      show_root_heading: true
      heading_level: 4
      show_bases: false
      show_source: false

## Shell Plugin

{# ::: tmt.steps.discover.shell.DiscoverShell
    options:
      show_root_heading: false
      show_bases: true
      heading_level: 3 #}

### Shell Configuration
{# Assuming DiscoverShellData exists in tmt.steps.discover.shell #}
{# {{ render_plugin_options('tmt.steps.discover.shell.DiscoverShellData', inherited_from_path_str='tmt.steps.discover.DiscoverStepData') }} #}

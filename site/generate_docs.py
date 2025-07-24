import dataclasses
import enum
import sys
import textwrap
from typing import Any

import tmt.checks
import tmt.container
import tmt.log
import tmt.plugins
import tmt.steps
import tmt.steps.discover
import tmt.steps.execute
import tmt.steps.finish
import tmt.steps.prepare
import tmt.steps.prepare.feature
import tmt.steps.provision
import tmt.steps.report
import tmt.utils
import tmt.utils.hints
from tmt.container import ContainerClass
from tmt.utils import Path

def generate_step_docs(step_name, step_class, header):
    """Generate documentation for a given step."""
    with open(f"docs_md/plugins/{step_name}.md", "w") as f:
        f.write(f"# {step_name.capitalize()}\n\n")
        f.write(f"{header}\n\n")
        registry = step_class._supported_methods
        for plugin_id in sorted(registry.iter_plugin_ids()):
            plugin = registry.get_plugin(plugin_id).class_
            f.write(f"## {plugin_id}\n\n")
            f.write(f"::: {plugin.__module__}.{plugin.__name__}\n")
            f.write("\n")

def generate_plugins_index():
    """Generate the index page for plugins."""
    with open("docs_md/plugins/index.md", "w") as f:
        f.write("# Plugins\n\n")
        f.write("This section documents the available plugins for each step.\n\n")
        f.write("* [Discover](discover.md)\n")
        f.write("* [Provision](provision.md)\n")
        f.write("* [Prepare](prepare.md)\n")
        f.write("* [Execute](execute.md)\n")
        f.write("* [Report](report.md)\n")
        f.write("* [Finish](finish.md)\n")


def main():
    """Generate all documentation."""
    Path("docs_md/plugins").mkdir(exist_ok=True)
    logger = tmt.log.Logger.create()
    logger.add_console_handler()
    tmt.plugins.explore(logger)

    generate_plugins_index()
    generate_step_docs("discover", tmt.steps.discover.DiscoverPlugin, "Gather information about test cases to be executed.")
    generate_step_docs("provision", tmt.steps.provision.ProvisionPlugin, "Provision an environment for testing.")
    generate_step_docs("prepare", tmt.steps.prepare.PreparePlugin, "Prepare the environment for testing.")
    generate_step_docs("execute", tmt.steps.execute.ExecutePlugin, "Run tests using the specified executor.")
    generate_step_docs("report", tmt.steps.report.ReportPlugin, "Provide test results overview and send reports.")
    generate_step_docs("finish", tmt.steps.finish.FinishPlugin, "Perform the finishing tasks and clean up provisioned guests.")


if __name__ == "__main__":
    main()

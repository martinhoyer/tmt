from typing import Any, Callable, Dict
import pytest
from tmt._compat.pathlib import Path
import pytest_container # type: ignore[import-untyped]
from ruamel.yaml import YAML
from tmt.log import Logger
# import toml # Would be needed for more complex TOML generation


@pytest.fixture(scope="session")
def root_logger() -> Logger:
    """ Root logger fixture """
    # Keeping this simple as its definition might vary or be in another conftest
    # For the purpose of this task, a basic logger is fine.
    return Logger.create(verbose=0, debug=0, quiet=False)


@pytest.fixture(scope="session")
def tmt_mini_container(
    docker_client, container_runtime: pytest_container.ContainerRuntime, tmp_path_factory
) -> pytest_container.Container:
    """
    Builds and runs a container with tmt installed from the current repository,
    using the minimal Containerfile.
    """
    image_name = "tmt/mini-test-fixture:latest"
    # tests/conftest.py, so repo_root is its parent.
    repo_root = Path(__file__).parent.parent.resolve()
    containerfile_path = repo_root / "containers" / "Containerfile.mini"

    assert containerfile_path.is_file(), f"Containerfile not found at {containerfile_path}"

    # Build the image
    docker_client.images.build(
        path=str(repo_root),
        dockerfile=str(containerfile_path.relative_to(repo_root)),
        tag=image_name,
        rm=True,  # Remove intermediate containers
    )

    # Run the container
    container = container_runtime.run(
        image_name,
        volumes=[f"{repo_root}:/src:z"],  # ':z' for SELinux compatibility
        detach=True,
        tty=True,
        interactive=True,
        working_dir="/src", # Set working directory to where the repo is mounted
    )

    yield container

    # Cleanup: stop and remove the container
    try:
        container.stop()
        container.remove(force=True)
    except Exception as e:
        # Log error during cleanup, but don't fail the test run
        print(f"Error during container cleanup: {e}")


@pytest.fixture(scope="session")
def basic_maelstrom_config_for_tmt_tests(tmp_path_factory, worker_id) -> Path:
    """
    Creates a basic Maelstrom configuration directory for running tmt tests.
    """
    # Create a unique temporary directory for this worker
    # tmp_path_factory from pytest returns pathlib.Path, which is fine here.
    # The return type of this fixture is `tmt.utils.Path`, so the conversion is handled by pytest or the fixture code.
    # The actual `Path` object from `tmp_path_factory.mktemp` is a standard `pathlib.Path`.
    # We are changing the `-> Path` annotation to `-> TmtUtilsPath` (effectively) if we were using `tmt.utils.Path`
    # but since the fixture is now returning a standard pathlib.Path from tmp_path_factory,
    # and the type hint for the fixture is `-> Path` (which will now be `tmt._compat.pathlib.Path`),
    # we should ensure that the returned object `maelstrom_cfg_dir` is compatible or explicitly converted if needed.
    # However, `tmp_path_factory.mktemp` returns `pathlib.Path`.
    # The fixture's return type is `Path` (now `tmt._compat.pathlib.Path`).
    # This is fine, as `tmt._compat.pathlib.Path` is essentially `pathlib.Path`.
    maelstrom_cfg_dir_stdlib = tmp_path_factory.mktemp(f"maelstrom_config_{worker_id}")
    maelstrom_cfg_dir = Path(str(maelstrom_cfg_dir_stdlib)) # Explicitly cast to our Path

    # Define maelstrom-pytest.toml content
    # Note: Paths in `added_layers` are relative to the project root if maelstrom-pytest
    # is run from there, or need to be absolute.
    # For this prototype, we assume it's run from the repo root.
    maelstrom_toml_content = """\
# Example maelstrom-pytest.toml for tmt
[[directives]]
image = "docker://python:3.11-slim" # Base image

added_layers = [
    # Mount the whole tmt repository (parent of 'tests' dir) into /src in the container
    { local_path = ".", remote_path = "/src" },
    # Example: If specific test files or directories were needed from 'tests/integration'
    # { local_path = "tests/integration", remote_path = "/tests_src/integration" }
]

# Example of how environment variables or other configurations could be set:
# env = { "PYTHONPATH = "/src" } # Setting PYTHONPATH might be one way to make `import tmt` work
# working_directory = "/src/tests" # If tests need to be run from a specific directory
"""
    with open(maelstrom_cfg_dir / "maelstrom-pytest.toml", "w") as f:
        f.write(maelstrom_toml_content)

    # Define test-requirements.txt content
    test_requirements_content = """\
pytest
# Assuming tmt is used from the mounted source in /src, so not installing via pip.
# If pip install was desired:
# ../ # This would point to the setup.py/pyproject.toml in the repo root.
# or specific version:
# tmt==1.28.0
"""
    with open(maelstrom_cfg_dir / "test-requirements.txt", "w") as f:
        f.write(test_requirements_content)

    print(f"Maelstrom config generated at: {maelstrom_cfg_dir}")
    yield maelstrom_cfg_dir
    # tmp_path_factory handles cleanup of the directory


@pytest.fixture(scope="session")
def fmf_tree(tmp_path_factory, root_logger: Logger) -> Callable[[Dict[str, Any]], Path]:
    """
    Provides a factory function to create FMF tree structures for tests.
    Each call to the factory function creates a new, unique FMF tree.
    """

    def _create_fmf_tree(content: Dict[str, Any]) -> Path:
        """
        Inner factory function to create a specific FMF tree.
        `content` is a dictionary where keys are relative file paths
        and values are either strings (for direct write) or dicts (for YAML dump).
        """
        # Each call to mktemp will create a new unique directory
        fmf_root = tmp_path_factory.mktemp("fmf_tree_instance_")
        root_logger.debug(f"Created FMF root for test at: {fmf_root}")

        # Create .fmf directory and version file, unless explicitly provided in content
        # to allow overriding or testing scenarios without it.
        if ".fmf/version" not in content:
            fmf_meta_dir = fmf_root / ".fmf"
            fmf_meta_dir.mkdir(parents=True, exist_ok=True)
            (fmf_meta_dir / "version").write_text("1")
            root_logger.debug(f"Created default .fmf/version in {fmf_root}")


        yaml_writer = YAML()
        yaml_writer.indent(mapping=2, sequence=4, offset=2) # Common YAML formatting

        for relative_file_path_str, file_content_data in content.items():
            abs_file_path = fmf_root / relative_file_path_str
            abs_file_path.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(file_content_data, str):
                abs_file_path.write_text(file_content_data)
                root_logger.debug(f"Wrote string content to {abs_file_path}")
            elif isinstance(file_content_data, dict):
                with open(abs_file_path, "w") as f:
                    yaml_writer.dump(file_content_data, f)
                root_logger.debug(f"Wrote YAML content to {abs_file_path}")
            else:
                # Should not happen based on type hint, but good to be defensive
                raise ValueError(
                    f"Unsupported content type for {relative_file_path_str}: "
                    f"{type(file_content_data)}"
                )
        return fmf_root

    return _create_fmf_tree

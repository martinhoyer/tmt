import pytest

import tmt
from tmt.log import Logger
from tmt.utils import Path


@pytest.fixture(scope="module")
def root_logger() -> Logger:
    """Root logger fixture."""
    return Logger.create()


@pytest.fixture
def sample_fmf_root(tmp_path: Path) -> Path:
    """Create a sample FMF root with a few tests and a plan."""
    fmf_root = tmp_path / "sample_fmf_root"
    fmf_root.mkdir()

    # .fmf directory and version
    fmf_dir = fmf_root / ".fmf"
    fmf_dir.mkdir()
    (fmf_dir / "version").write_text("1")

    # Plans directory
    plans_dir = fmf_root / "plans"
    plans_dir.mkdir()
    plan1_content = """
discover:
    how: fmf
    url: .
    tests:
        - /tests/test1
        - /tests/test2
execute:
    how: shell
    script: echo "Executing plan"
"""
    (plans_dir / "plan1.fmf").write_text(plan1_content)

    # Tests directory
    tests_dir = fmf_root / "tests"
    tests_dir.mkdir()

    # Test 1
    test1_dir = tests_dir / "test1"
    test1_dir.mkdir()
    (test1_dir / "main.fmf").write_text("test: ./test.sh\nsummary: Test 1")
    (test1_dir / "test.sh").write_text("#!/bin/bash\necho TEST 1")
    (test1_dir / "test.sh").chmod(0o755)

    # Test 2
    test2_dir = tests_dir / "test2"
    test2_dir.mkdir()
    (test2_dir / "main.fmf").write_text("test: ./test.sh\nsummary: Test 2")
    (test2_dir / "test.sh").write_text("#!/bin/bash\necho TEST 2")
    (test2_dir / "test.sh").chmod(0o755)

    return fmf_root


def test_benchmark_tree_tests(benchmark, sample_fmf_root: Path, root_logger: Logger):
    """Benchmark the tmt.Tree.tests() method."""
    tree = tmt.Tree(path=sample_fmf_root, logger=root_logger)
    benchmark(tree.tests)

"""Every workflow test gets its own repository and stand-ins."""

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from workflow_harness import Workflow, make_workflow


@pytest.fixture
def programs_folder() -> Iterator[Path]:
    """A folder of the test's own on a drive that allows running programs.

    `tmp_path` is not one in CI, which gives pytest a folder in `/dev/shm`.
    """
    folder = Path(tempfile.mkdtemp(prefix="workflow-programs-"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def workflow(tmp_path: Path) -> Iterator[Workflow]:
    bin_folder = Path(tempfile.mkdtemp(prefix="workflow-bin-"))
    try:
        yield make_workflow(tmp_path, bin_folder)
    finally:
        shutil.rmtree(bin_folder, ignore_errors=True)

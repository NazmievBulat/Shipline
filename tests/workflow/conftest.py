"""Every workflow test gets its own repository and stand-ins."""

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from workflow_harness import Workflow, make_workflow


@pytest.fixture
def workflow(tmp_path: Path) -> Iterator[Workflow]:
    bin_folder = Path(tempfile.mkdtemp(prefix="workflow-bin-"))
    try:
        yield make_workflow(tmp_path, bin_folder)
    finally:
        shutil.rmtree(bin_folder, ignore_errors=True)

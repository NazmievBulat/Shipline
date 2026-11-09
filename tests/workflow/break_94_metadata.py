"""Opt-in live metadata audit for PR 94. Run separately from the offline break cases."""

import json
import os
import re
import subprocess


def test_public_description_omits_installation_specific_incident_history():
    done = subprocess.run(
        [os.environ.get("SHIPLINE_BREAK_GH", "gh"), "pr", "view", "94", "--repo",
         "NazmievBulat/Shipline", "--json", "body"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert done.returncode == 0, "GitHub metadata could not be read"
    # Inspect in memory; never print or save the description in the test output.
    description = json.loads(done.stdout)["body"]
    has_incident = re.search(r"(?m)^One installation,", description) is not None
    assert not has_incident, "PR description exposes installation-specific incident history"

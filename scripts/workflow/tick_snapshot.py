"""One paginated GraphQL read for the tick's open pull requests and their checks."""

import json
import subprocess
import sys
from pathlib import Path

from github_budget import Limited, call, save

# The description is for the automatic ticket start, which reads the files a draft names.
FIELDS = "number,isDraft,baseRefName,headRefOid,headRefName,statusCheckRollup,mergeable,mergeStateStatus,state,labels,body"


def create(path: Path) -> None:
    done = call("tick", "pr", "list", "--state", "open", "--limit", "1000", "--json", FIELDS)
    done.check_returncode()
    pulls = json.loads(done.stdout)
    if not isinstance(pulls, list):
        raise TypeError("invalid pull request list")
    save(path, {str(pull["number"]): pull for pull in pulls})


def view(path: Path, number: str) -> None:
    pulls = json.loads(path.read_text())
    if number in pulls:
        print(json.dumps(pulls[number]))
    else:
        # Audits may refer to an unlabelled or just-merged pull request. An absent
        # entry is never evidence that a pull request merged or closed.
        done = call("tick", "pr", "view", number, "--json", FIELDS)
        done.check_returncode()
        print(done.stdout)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "create":
            create(Path(sys.argv[2]))
        else:
            view(Path(sys.argv[2]), sys.argv[3])
    except Limited:
        sys.exit(75)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Cannot read pull requests: {error}", file=sys.stderr)
        sys.exit(1)

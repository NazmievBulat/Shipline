# The priority of a pull request, read from its labels: the most urgent `priority:` label it
# carries, medium without one. `priority.py` says the same for the merge gate.
def priority_rank:
  [.labels[]?.name | strings
    | {"priority: blocker": 0, "priority: high": 1, "priority: medium": 2, "priority: low": 3}[.]
    // empty]
  | min // 2;
def priority_name: ["blocker", "high", "medium", "low"][priority_rank];

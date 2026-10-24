# The priority of a pull request, read from its labels: the most urgent priority label it
# carries, medium without one. `priority.py` says the same for the merge gate.
#
# The label names are those of `shipline.json`, which the caller passes whole in the
# environment as SHIPLINE_CONFIG (`workflow_config.py shell` prints it); a name it leaves out
# keeps its default, as in `workflow_config.py`.
def shipline_labels:
  $ENV.SHIPLINE_CONFIG // "" | if . == "" then {} else fromjson end | .labels // {};
def workflow_label: shipline_labels.workflow // "workflow";
def priority_rank:
  (shipline_labels.priority // {}) as $named
  | (["blocker", "high", "medium", "low"] | to_entries
    | map({key: ($named[.value] // "priority: \(.value)"), value: .key}) | from_entries) as $rank
  | [.labels[]?.name | strings | $rank[.] // empty]
  | min // 2;
def priority_name: ["blocker", "high", "medium", "low"][priority_rank];

"""mesh_ctl.py — DEPRECATED as of v5.6.

Reason:
    Claude Web Pro/Max can execute bash commands (curl) directly.
    This wrapper added no value beyond building the JSON envelope and
    polling — both trivial operations for Claude to do inline.

Replacement pattern (Claude uses curl directly):

    # Execute an action
    curl -s -X POST "$GATEWAY/action" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{
        "target_node_id": "node-1",
        "payload": {
          "action": "execute_command",
          "params": {"command": "df -h", "timeout_seconds": 30}
        }
      }'

    # Poll async result
    curl -s "$GATEWAY/result/$JOB_ID" \
      -H "Authorization: Bearer $TOKEN"

See SYSTEM_PROMPT.md for the full curl-native workflow.
Original implementation preserved in mesh_ctl.py.deprecated.
"""

# This file intentionally left as a stub.
# See mesh_ctl.py.deprecated for the original implementation.
raise SystemExit(
    "mesh_ctl.py is deprecated since v5.6.\n"
    "Claude Web uses curl directly — see SYSTEM_PROMPT.md.\n"
    "Original code: mesh_ctl.py.deprecated"
)

"""Wraps the CockroachDB Cloud `ccloud` CLI (agent-ready control plane access:
https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-reference) so
AgentOps can pull cluster-level context that a plain SQL connection can't see.

Two things it's used for:
  - `ccloud cluster info <name> -o json`   -- plan/region/node/state, so a
    slow query can be told apart from an undersized cluster plan (an
    app-level fix like CREATE INDEX won't help a capacity problem).
  - `ccloud audit list -o json`            -- the Cloud-side control-plane
    audit trail, folded into the incident report for defense-in-depth
    explainability alongside AgentOps' own agent_trace.

Best-effort by design, matching tools/embeddings.py's fallback philosophy: if
`ccloud` isn't installed, the caller isn't logged in, or the command fails
for any reason, every function returns None instead of raising, so the
DB-only pipeline still runs end-to-end without it.
"""
import json
import os
import shutil
import subprocess

CCLOUD_CLUSTER_NAME = os.getenv("CCLOUD_CLUSTER_NAME")
CCLOUD_TIMEOUT_S = int(os.getenv("CCLOUD_TIMEOUT_S", "10"))


def available() -> bool:
    return shutil.which("ccloud") is not None


def _run(args):
    if not available():
        return None
    try:
        result = subprocess.run(
            ["ccloud", *args, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=CCLOUD_TIMEOUT_S,
            check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def cluster_info(cluster_name=None):
    """`ccloud cluster info <name>` -- plan, region, node count, current
    state. Used as cluster-level context alongside CockroachDB-internal query
    metrics: a query getting slower could be app-level (missing index / stale
    stats) or cluster-level (undersized plan) -- this is how the agent tells
    them apart.
    """
    name = cluster_name or CCLOUD_CLUSTER_NAME
    if not name:
        return None
    return _run(["cluster", "info", name])


def recent_audit_events(limit=5):
    """`ccloud audit list --limit N` -- the CockroachDB Cloud control-plane
    audit trail (who/what/when at the account level). AgentOps folds a slice
    of this into its own explainability report so a human reviewing an
    incident sees both "what the agent did" (agent_trace) and "what happened
    on the account around that time" (Cloud audit log) side by side.
    """
    data = _run(["audit", "list", "--limit", str(limit)])
    if isinstance(data, list):
        return data
    return None

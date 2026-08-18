"""boto3 wrappers for the AWS side of AgentOps: reading ECS service CPU from
CloudWatch, and scaling the ECS service when the database fix alone isn't
enough to bring load back down."""
import os
from datetime import datetime, timedelta

import boto3

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


def _cloudwatch():
    return boto3.client("cloudwatch", region_name=AWS_REGION)


def _ecs():
    return boto3.client("ecs", region_name=AWS_REGION)


def get_ecs_avg_cpu(cluster_name, service_name, minutes=5):
    """Average CPUUtilization %% for an ECS service over the last N minutes.
    Returns None if there are no datapoints yet (e.g. brand-new service)."""
    cw = _cloudwatch()
    end = datetime.utcnow()
    start = end - timedelta(minutes=minutes)
    resp = cw.get_metric_statistics(
        Namespace="AWS/ECS",
        MetricName="CPUUtilization",
        Dimensions=[
            {"Name": "ClusterName", "Value": cluster_name},
            {"Name": "ServiceName", "Value": service_name},
        ],
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=["Average"],
    )
    points = resp.get("Datapoints", [])
    if not points:
        return None
    return sum(p["Average"] for p in points) / len(points)


def get_current_desired_count(cluster_name, service_name):
    ecs = _ecs()
    resp = ecs.describe_services(cluster=cluster_name, services=[service_name])
    services = resp.get("services", [])
    if not services:
        return None
    return services[0]["desiredCount"]


def scale_ecs_service(cluster_name, service_name, desired_count):
    ecs = _ecs()
    ecs.update_service(
        cluster=cluster_name,
        service=service_name,
        desiredCount=desired_count,
    )
    return desired_count


# --- Optional: CockroachDB Cloud cluster scaling -----------------------
# CockroachDB Cloud exposes a REST API for scaling dedicated clusters
# (compute units / node count). Wire this up if your demo cluster is on
# CockroachDB Cloud and you want a second, DB-side autoscaling story
# alongside ECS. Left as a stub since it needs your org/cluster IDs.
def scale_cockroachdb_cluster(cluster_id, new_node_count, api_key):
    raise NotImplementedError(
        "Wire this up with requests.patch() against the CockroachDB Cloud "
        "API (https://www.cockroachlabs.com/docs/cockroachcloud/cloud-api) "
        "once you have your cluster_id and API key."
    )

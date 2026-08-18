# Deploying AgentOps to AWS for the hackathon submission

Fastest path to a public "functional demo app" URL that also satisfies the
challenge's "deployed on AWS" requirement. Uses EC2 + systemd (survives SSH
disconnects and instance reboots, unlike a bare `python dashboard.py` in a
terminal) rather than ECS/Fargate/App Runner, which need a Docker build +
ECR push + task definitions you likely don't have time for before the
deadline.

## 1. Launch the instance

EC2 console -> Launch instance:
- AMI: **Ubuntu Server 22.04 LTS**
- Type: **t3.small** (2GB RAM; `sentence-transformers`/torch can OOM on
  t3.micro -- or skip it entirely, see step 4)
- Storage: **20GB** if you plan to install `sentence-transformers` (torch
  alone is ~2GB downloaded, more unpacked); 8GB is fine if you use
  `requirements-lite.txt` instead
- Key pair: create/download one for SSH

## 2. Attach an IAM role instead of hardcoding AWS keys

"Advanced details" -> IAM instance profile -> create a role with:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "ecs:DescribeServices",
        "ecs:UpdateService"
      ],
      "Resource": "*"
    }
  ]
}
```

`boto3` in `tools/aws_client.py` then picks up credentials automatically
from the instance metadata service -- no AWS keys in `.env` at all.

## 3. Security group

- Inbound: TCP **8888** from `0.0.0.0/0` (dashboard, for judges) and TCP
  **22** from just your IP (SSH).
- Outbound: default (allow all) -- needed for CockroachDB Cloud and your LLM
  provider.

## 4. SSH in and set up the code

```bash
ssh -i your-key.pem ubuntu@<ec2-public-ip>

sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone <your-repo-url>
cd Aws-X-CockroachDB-updt
python3 -m venv venv
source venv/bin/activate

# Skip sentence-transformers to save setup time/RAM -- tools/embeddings.py
# already has a deterministic offline fallback that works fine for the demo:
pip install -r requirements-lite.txt
```

## 5. Configure `.env` on the server only

```bash
cp .env.example .env
nano .env
```

Fill in your real `DATABASE_URL` (CockroachDB Cloud), `LLM_BACKEND` + its
key, and `ECS_CLUSTER_NAME` / `ECS_SERVICE_NAME` so CloudWatch/ECS calls are
live rather than stubbed. This `.env` never leaves the box -- it's
gitignored and was never part of the repo you pushed.

## 6. Verify before you trust it for judging

```bash
python demo_scenario.py
python demo_scenario.py   # run it twice -- second run should show
                           # "diagnosis (recalled) — LLM skipped" in the trace
python demo_stale_stats.py --customer-id <same-or-new-uuid>
pytest tests/ -v           # sanity check, no DB/network needed
```

## 7. Run the dashboard as a systemd service

```bash
sudo cp deploy/agentops-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentops-dashboard
sudo systemctl status agentops-dashboard   # should show "active (running)"
```

Optional, for the "watch it happen live" demo instead of running
`demo_scenario.py` manually:

```bash
python load_generator.py --customer-id <id> &      # simulate traffic
# edit deploy/agentops-orchestrator.service, replace CHANGE_ME with <id>
sudo cp deploy/agentops-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentops-orchestrator
```

## 8. Get a URL that won't change

Allocate an **Elastic IP** and associate it with the instance so the public
IP doesn't change if the instance stops/restarts. Your demo URL is then
`http://<elastic-ip>:8888` -- stable for the whole judging period.

## 9. Cost / cleanup

A t3.small for a day or two is a few cents. Terminate the instance (and
release the Elastic IP) once judging closes.

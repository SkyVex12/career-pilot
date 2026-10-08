"""Deploy (or roll back) Stage 0 to an image tag: migrations first, then the services.

    uv run --no-project --with boto3 python infra/scripts/deploy.py --tag <git sha>
    uv run --no-project --with boto3 python infra/scripts/deploy.py --tag <older sha>   # rollback
    uv run --no-project --with boto3 python infra/scripts/deploy.py --status

CI runs it after pushing the images (.github/workflows/deploy.yml); a person runs it the same
way to roll back. For each workload it registers a new revision of the task definition
Terraform made, with only the image tag changed, then:

1. runs the migration task (copilot-migrate, then copilot-db-logins) and fails on a non-zero
   exit, printing its log;
2. updates the services (proxy, gateway, API, worker) and waits until they are stable. On the
   one instance, each service's old task stops before the new one starts: the desktop client
   reconnects and resumes its session meanwhile;
3. records the tag in the SSM parameter Terraform reads, so a later `terraform apply` keeps it.

Migrations follow expand then contract (ARCHITECTURE.md, "Conventions"), so an older image
runs against a newer schema: a rollback is a deploy of the older tag. Images are immutable
and ECR keeps the last 15 per repository.
"""

import argparse
import sys
import time
from typing import Any

import boto3

SERVICES = ("proxy", "gateway", "api", "worker")
ONE_OFF = ("migrate", "scheduler")
REPOS = ("proxy", "gateway", "api", "worker")

# What register_task_definition accepts from a describe_task_definition result.
TASK_DEF_FIELDS = (
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
    "runtimePlatform", "pidMode", "ipcMode",
)  # fmt: skip


def log(msg: str) -> None:
    print(msg, flush=True)


class Stage:
    def __init__(self, name: str, region: str) -> None:
        self.name = name
        session = boto3.session.Session(region_name=region)
        self.ecs: Any = session.client("ecs")
        self.ecr: Any = session.client("ecr")
        self.ssm: Any = session.client("ssm")
        self.logs: Any = session.client("logs")
        self.parameter = f"/{name}/image-tag"

    def family(self, workload: str) -> str:
        return f"{self.name}-{workload}"

    def current_tag(self) -> str:
        return self.ssm.get_parameter(Name=self.parameter)["Parameter"]["Value"]

    def check_images(self, tag: str) -> None:
        missing = []
        for repo in REPOS:
            try:
                self.ecr.describe_images(
                    repositoryName=f"{self.name}/{repo}", imageIds=[{"imageTag": tag}]
                )
            except self.ecr.exceptions.ImageNotFoundException:
                missing.append(repo)
        if missing:
            sys.exit(f"no image {tag} in ECR for: {', '.join(missing)}")

    def check_capacity(self) -> None:
        arns = self.ecs.list_container_instances(cluster=self.name, status="ACTIVE")
        if not arns["containerInstanceArns"]:
            sys.exit(
                f"no instance in cluster {self.name}: is it stopped? Start it first "
                "(README.md, 'Deploying Stage 0')"
            )

    def register(self, workload: str, tag: str) -> str:
        """A new revision of the workload's task definition with every image moved to `tag`."""
        current = self.ecs.describe_task_definition(taskDefinition=self.family(workload))
        td = current["taskDefinition"]
        spec = {k: td[k] for k in TASK_DEF_FIELDS if td.get(k)}
        changed = False
        for c in spec["containerDefinitions"]:
            repo, _, old = c["image"].rpartition(":")
            if f"/{self.name}/" in repo and old != tag:
                c["image"] = f"{repo}:{tag}"
                changed = True
        if not changed:
            return td["taskDefinitionArn"]
        tags = self.ecs.list_tags_for_resource(resourceArn=td["taskDefinitionArn"]).get("tags")
        new = self.ecs.register_task_definition(**spec, **({"tags": tags} if tags else {}))
        return new["taskDefinition"]["taskDefinitionArn"]

    def migrate(self, task_def: str) -> None:
        run = self.ecs.run_task(
            cluster=self.name, taskDefinition=task_def, launchType="EC2", count=1,
            startedBy="deploy.py",
        )  # fmt: skip
        if run.get("failures"):
            sys.exit(f"migration task did not start: {run['failures']}")
        task_arn = run["tasks"][0]["taskArn"]
        log(f"migrations: task {task_arn.rsplit('/', 1)[-1]}")
        self.ecs.get_waiter("tasks_stopped").wait(
            cluster=self.name, tasks=[task_arn], WaiterConfig={"Delay": 5, "MaxAttempts": 120}
        )
        task = self.ecs.describe_tasks(cluster=self.name, tasks=[task_arn])["tasks"][0]
        container = task["containers"][0]
        self.print_task_log("migrate", task_arn)
        if container.get("exitCode") != 0:
            reason = container.get("reason") or task.get("stoppedReason")
            sys.exit(f"migrations failed (exit {container.get('exitCode')}): {reason}")

    def print_task_log(self, workload: str, task_arn: str) -> None:
        stream = f"{workload}/{workload}/{task_arn.rsplit('/', 1)[-1]}"
        try:
            events = self.logs.get_log_events(
                logGroupName=f"/{self.name}/{workload}", logStreamName=stream, startFromHead=True
            )["events"]
        except self.logs.exceptions.ResourceNotFoundException:
            return
        for e in events:
            log(f"  | {e['message']}")

    def update_services(self, task_defs: dict[str, str]) -> None:
        for svc in SERVICES:
            self.ecs.update_service(
                cluster=self.name, service=svc, taskDefinition=task_defs[svc], desiredCount=1,
                forceNewDeployment=False,
            )  # fmt: skip
            log(f"{svc}: deploying {task_defs[svc].rsplit('/', 1)[-1]}")
        log("waiting for the services to be stable (each stops its old task first)")
        self.ecs.get_waiter("services_stable").wait(
            cluster=self.name, services=list(SERVICES),
            WaiterConfig={"Delay": 10, "MaxAttempts": 90},
        )  # fmt: skip

    def status(self) -> None:
        log(f"recorded tag: {self.current_tag()}")
        found = self.ecs.describe_services(cluster=self.name, services=list(SERVICES))
        for s in found["services"]:
            image_rev = s["taskDefinition"].rsplit("/", 1)[-1]
            log(
                f"{s['serviceName']:8} {s['runningCount']}/{s['desiredCount']} running, {image_rev}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", help="the image tag (git SHA) to deploy")
    parser.add_argument("--name", default="copilot-stage0")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--skip-migrations", action="store_true")
    parser.add_argument("--status", action="store_true", help="show what runs, change nothing")
    args = parser.parse_args()
    stage = Stage(args.name, args.region)
    if args.status:
        stage.status()
        return
    if not args.tag:
        parser.error("--tag is required")
    started = time.monotonic()
    previous = stage.current_tag()
    log(f"deploying {args.tag} to {args.name} (was {previous})")
    stage.check_images(args.tag)
    stage.check_capacity()
    task_defs = {w: stage.register(w, args.tag) for w in (*ONE_OFF, *SERVICES)}
    if not args.skip_migrations:
        stage.migrate(task_defs["migrate"])
    stage.update_services(task_defs)
    stage.ssm.put_parameter(Name=stage.parameter, Value=args.tag, Overwrite=True)
    log(f"deployed {args.tag} in {time.monotonic() - started:.0f} s; rollback: --tag {previous}")


if __name__ == "__main__":
    main()

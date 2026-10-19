#!/bin/bash
# copilotctl: the Stage 0 VM's control script (ARCHITECTURE.md, "Stage 0 on GCP"). It does on
# one Container-Optimized OS VM what ECS does on AWS: every workload is a Docker container
# with host networking, run by a systemd unit (copilot-<name>.service) that calls
# `copilotctl run <name>`. cloud-init writes this file and the units on every boot (COS keeps
# /etc in memory) and runs `copilotctl boot`. Everything runs as root.
#
#   copilotctl boot                  mount the data disk, firewall, units; start everything
#   copilotctl deploy <tag>          migrations, then each app service on images <tag>
#   copilotctl rollback              deploy the tag before the current one
#   copilotctl status                tag, containers and their health
#   copilotctl restart [name...]     restart services (picks up new config and secrets)
#   copilotctl scheduler             one copilot-scheduler --once tick (the 5-minute timer)
#   copilotctl backup                pg_dump to the backups bucket (the nightly timer)
#   copilotctl restore <object>      restore a dump from the backups bucket (app stopped meanwhile)
#   copilotctl run <name>            the units' ExecStart (not for people)
#
# Configuration comes from instance metadata, so `terraform apply` changes it in place and a
# restart picks it up: `copilot-env` holds the environment per workload, with secret values
# written as {{secret:<name>}} and fetched from Secret Manager each time a container starts.
# They go to /run/copilot/<name>.env (tmpfs, root only) and never reach the journal.
set -euo pipefail

DATA=/mnt/disks/data
STATE=$DATA/copilot
RUN=/run/copilot
MD=http://metadata.google.internal/computeMetadata/v1
# COS's /root is read-only; docker keeps its registry credentials under this HOME.
export HOME=/var/lib/copilot/home

APP_SERVICES=(proxy gateway api worker)
DATA_SERVICES=(postgres valkey elasticmq)

log() { echo "copilotctl: $*" >&2; }
die() { log "$*"; exit 1; }

md() { curl -sfS -H "Metadata-Flavor: Google" "$MD/$1"; }
attr() { md "instance/attributes/$1"; }
token() { md "instance/service-accounts/default/token" | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p'; }

project() { md project/project-id; }

secret() {
  # The latest version of a Secret Manager secret; fails (naming it) if it has no value yet.
  local name=$1 body code data
  body=$(curl -sS -w '\n%{http_code}' -H "Authorization: Bearer $(token)" \
    "https://secretmanager.googleapis.com/v1/projects/$(project)/secrets/$name/versions/latest:access") ||
    die "secret $name: request failed"
  code=${body##*$'\n'}
  [ "$code" != 404 ] || die "secret $name has no value yet (README.md, \"Deploying Stage 0\": Secrets)"
  [ "$code" = 200 ] || die "secret $name: HTTP $code"
  data=$(printf '%s' "${body%$'\n'*}" | tr -d '\n' | sed -n 's/.*"data" *: *"\([^"]*\)".*/\1/p')
  printf '%s' "$data" | base64 -d
}

env_section() {
  # The [common] lines, then the [<name>] lines, of the copilot-env metadata value.
  attr copilot-env | awk -v want="$1" '
    /^\[.*\]$/ { section = substr($0, 2, length($0) - 2); next }
    /^[[:space:]]*(#|$)/ { next }
    section == "common" || section == want { print }'
}

write_env() {
  # /run/copilot/<name>.env with every {{secret:x}} replaced by the secret's value.
  local name=$1 out=$RUN/$1.env line s value
  mkdir -p "$RUN" && chmod 700 "$RUN"
  umask 077
  : > "$out.tmp"
  while IFS= read -r line; do
    while [[ $line =~ \{\{secret:([a-z0-9-]+)\}\} ]]; do
      s=${BASH_REMATCH[1]}
      value=$(secret "$s")
      line=${line//"{{secret:$s}}"/$value}
    done
    printf '%s\n' "$line" >> "$out.tmp"
  done < <(env_section "$name")
  mv "$out.tmp" "$out"
}

tag() { cat "$STATE/image-tag" 2>/dev/null || echo none; }
registry() { attr copilot-registry; }
image() {
  case $1 in
    postgres|valkey|elasticmq) attr "copilot-image-$1" ;;
    migrate|scheduler) echo "$(registry)/worker:$(tag)" ;;
    *) echo "$(registry)/$1:$(tag)" ;;
  esac
}

# --- Workloads (the task definitions of infra/envs/stage0 on AWS) ------------------------------
# Container uids: the app images run as non-root users, and the host firewall lets only the
# API (10001) and the workers (10002) reach the metadata server, for Cloud KMS. The rest
# (gateway 10003, one-off tasks 10004, ElasticMQ 10005, and the images' own users) cannot.

python_health() {
  HEALTH=(--health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:$1/healthz', timeout=3)\""
    --health-interval 15s --health-timeout 5s --health-retries 3 --health-start-period 30s)
}

workload() {
  # ARGS: docker run options; CMD: the command (empty: the image's own).
  local name=$1
  ARGS=() CMD=() HEALTH=()
  case $name in
    postgres)
      ARGS=(--memory 512m --stop-timeout 90 -v "$DATA/postgres:/pgroot"
        --health-cmd "pg_isready -h 127.0.0.1 -U postgres -d copilot" --health-interval 10s)
      CMD=(sh -c "test -f /pgroot/.copilot-data-volume || { echo 'data disk not mounted' >&2; exit 1; }; exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c max_connections=60 -c shared_buffers=128MB -c effective_cache_size=512MB -c work_mem=4MB")
      ;;
    valkey)
      # Session state and packs must not be evicted: a full Valkey refuses writes (the
      # gateway falls back to memory) instead of dropping a pack.
      ARGS=(--memory 320m --stop-timeout 30 -v "$DATA/valkey:/data"
        --health-cmd "valkey-cli -h 127.0.0.1 ping" --health-interval 10s)
      CMD=(sh -c "test -f /data/.copilot-data-volume || { echo 'data disk not mounted' >&2; exit 1; }; exec docker-entrypoint.sh valkey-server --bind 127.0.0.1 --port 6379 --protected-mode yes --dir /data --appendonly yes --appendfsync everysec --save '300 1' --maxmemory 192mb --maxmemory-policy noeviction")
      ;;
    elasticmq)
      ARGS=(--memory 128m --user 10005 -v "$DATA/elasticmq:/data"
        -v /var/lib/copilot/elasticmq.conf:/opt/elasticmq.conf:ro)
      ;;
    proxy)
      ARGS=(--memory 96m --health-cmd "wget -q -O /dev/null http://127.0.0.1/healthz" --health-interval 15s)
      ;;
    gateway) python_health 8081; ARGS=(--memory 512m --stop-timeout 30 --user 10003 "${HEALTH[@]}"); CMD=(copilot-gateway) ;;
    api) python_health 8080; ARGS=(--memory 384m --user 10001 "${HEALTH[@]}"); CMD=(copilot-api) ;;
    worker) python_health 8082; ARGS=(--memory 640m --stop-timeout 110 --user 10002 "${HEALTH[@]}"); CMD=(copilot-worker) ;;
    migrate) ARGS=(--memory 384m --user 10004); CMD=(sh -c "copilot-migrate && copilot-db-logins") ;;
    scheduler) ARGS=(--memory 256m --user 10004); CMD=(copilot-scheduler --once) ;;
    *) die "unknown workload $name" ;;
  esac
}

docker_run() {
  # docker run for one workload; `exec` keeps it the unit's main process.
  local name=$1 mode=$2
  write_env "$name"
  workload "$name"
  local -a run=(docker run --rm --name "copilot-$name" --network host --env-file "$RUN/$name.env"
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 "${ARGS[@]}"
    "$(image "$name")" "${CMD[@]}")
  if [ "$mode" = exec ]; then exec "${run[@]}"; else "${run[@]}"; fi
}

# --- Boot ------------------------------------------------------------------------------------

mount_data() {
  local dev=/dev/disk/by-id/google-copilot-data
  for _ in $(seq 1 60); do [ -e $dev ] && break; sleep 2; done
  [ -e $dev ] || die "the data disk never appeared"
  blkid $dev >/dev/null 2>&1 || mkfs.ext4 -q -m 0 -L copilot-data -E lazy_itable_init=0,lazy_journal_init=0,discard $dev
  mkdir -p $DATA
  mountpoint -q $DATA || mount -o discard,defaults $dev $DATA
  mkdir -p "$STATE" "$DATA/postgres" "$DATA/valkey" "$DATA/elasticmq" "$DATA/backups"
  # The data services refuse to start without this file, so they never write to the boot
  # disk if the data disk is missing.
  touch "$DATA/postgres/.copilot-data-volume" "$DATA/valkey/.copilot-data-volume"
  chown 10005:10005 "$DATA/elasticmq"
  chmod 700 "$DATA/backups"
}

firewall() {
  # COS drops inbound traffic except SSH; the VPC firewall decides who reaches port 80.
  iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -A INPUT -p tcp --dport 80 -j ACCEPT
  # Metadata server tokens: root, the API and the workers only (see the uids above). OS Login
  # users (large uids) keep it for their name lookups. Only its HTTP port is closed: the same
  # address is the VM's DNS resolver (port 53), which every container needs.
  local rule
  for rule in "-m owner --uid-owner 100-999 -j REJECT" "-m owner --uid-owner 10003-10099 -j REJECT"; do
    # shellcheck disable=SC2086
    iptables -C OUTPUT -d 169.254.169.254/32 -p tcp --dport 80 $rule 2>/dev/null ||
      iptables -I OUTPUT 1 -d 169.254.169.254/32 -p tcp --dport 80 $rule
  done
}

swap() {
  # A memory spike slows the VM down instead of killing Postgres.
  if ! swapon --show=NAME --noheadings 2>/dev/null | grep -q "$DATA/swapfile"; then
    [ -f "$DATA/swapfile" ] || { fallocate -l 1G "$DATA/swapfile" && chmod 600 "$DATA/swapfile" && mkswap "$DATA/swapfile" >/dev/null; }
    swapon "$DATA/swapfile" || log "swap not available on this image; continuing without it"
  fi
  sysctl -qw vm.swappiness=10 vm.overcommit_memory=1
}

boot() {
  mkdir -p "$HOME"
  mount_data
  firewall
  swap || true
  docker-credential-gcr configure-docker --registries="$(registry | cut -d/ -f1)" >/dev/null
  systemctl daemon-reload
  systemctl start copilot-postgres copilot-valkey copilot-elasticmq
  systemctl start copilot-scheduler.timer copilot-backup.timer
  if [ "$(tag)" != none ]; then
    systemctl start "${APP_SERVICES[@]/#/copilot-}"
  else
    log "no image tag deployed yet: the app services wait for the first deploy"
  fi
}

# --- Deploys ---------------------------------------------------------------------------------

healthy() {
  # Wait until the container reports healthy (or runs, for those without a health check).
  local name=copilot-$1 state
  for _ in $(seq 1 60); do
    state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo missing)
    case $state in healthy|running) [ "$state" = running ] && sleep 5; return 0 ;; unhealthy) return 1 ;; esac
    sleep 3
  done
  return 1
}

wait_data() {
  for s in "${DATA_SERVICES[@]}"; do systemctl is-active -q "copilot-$s" || systemctl start "copilot-$s"; done
  healthy postgres || die "postgres is not healthy"
}

deploy() {
  local new=$1 old
  old=$(tag)
  [ -n "$new" ] || die "usage: copilotctl deploy <tag>"
  log "deploy $new (was $old)"
  echo "$new" > "$STATE/image-tag.next"
  for s in gateway api worker proxy; do
    docker pull -q "$(registry)/$s:$new" >/dev/null || { rm -f "$STATE/image-tag.next"; die "no image $s:$new in the registry"; }
  done
  mv "$STATE/image-tag.next" "$STATE/image-tag"
  wait_data
  if ! docker_run migrate wait; then
    echo "$old" > "$STATE/image-tag"
    die "migrations failed on $new; still on $old"
  fi
  # One host, shared ports: each service's old container stops before the new one starts.
  # The desktop app resumes its session meanwhile.
  local s
  for s in api worker gateway proxy; do
    systemctl restart "copilot-$s"
    if ! healthy "$s"; then
      log "$s did not get healthy on $new; rolling back to $old"
      if [ "$old" != none ]; then
        echo "$old" > "$STATE/image-tag"
        systemctl restart "${APP_SERVICES[@]/#/copilot-}"
      fi
      die "deploy of $new failed"
    fi
  done
  [ "$old" != none ] && [ "$old" != "$new" ] && echo "$old" >> "$STATE/tag-history"
  log "deployed $new; rollback: copilotctl rollback (to $old)"
}

rollback() {
  local prev
  prev=$(tail -n 1 "$STATE/tag-history" 2>/dev/null || true)
  [ -n "$prev" ] || die "no earlier tag to roll back to"
  sed -i '$ d' "$STATE/tag-history"
  local cur
  cur=$(tag)
  deploy "$prev"
  # deploy() appended the tag it replaced; a rollback should not make it the next rollback.
  [ "$(tail -n 1 "$STATE/tag-history" 2>/dev/null)" = "$cur" ] && sed -i '$ d' "$STATE/tag-history"
  return 0
}

status() {
  echo "image tag: $(tag)"
  echo "earlier:   $(tail -n 3 "$STATE/tag-history" 2>/dev/null | tr '\n' ' ')"
  docker ps -a --filter name=copilot- --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  systemctl list-timers 'copilot-*' --no-pager | head -n 4
  df -h "$DATA" | tail -n 1
  free -m | sed -n 2,3p
}

# --- Backups ---------------------------------------------------------------------------------

backup() {
  local bucket stamp file
  bucket=$(attr copilot-backup-bucket)
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  file=$DATA/backups/copilot-$stamp.dump
  docker exec copilot-postgres pg_dump -U postgres -d copilot -Fc > "$file.tmp"
  mv "$file.tmp" "$file"
  curl -sfS -X POST -H "Authorization: Bearer $(token)" -H "Content-Type: application/octet-stream" \
    --data-binary "@$file" \
    "https://storage.googleapis.com/upload/storage/v1/b/$bucket/o?uploadType=media&name=postgres/copilot-$stamp.dump" >/dev/null
  # Only the newest dump stays on the disk; the bucket keeps 7 days.
  find "$DATA/backups" -name 'copilot-*.dump' ! -name "copilot-$stamp.dump" -delete
  log "backup gs://$bucket/postgres/copilot-$stamp.dump ($(du -h "$file" | cut -f1))"
}

restore() {
  local object=$1 bucket file
  [ -n "$object" ] || die "usage: copilotctl restore postgres/copilot-<stamp>.dump"
  bucket=$(attr copilot-backup-bucket)
  file=$DATA/backups/restore.dump
  curl -sfS -H "Authorization: Bearer $(token)" -o "$file" \
    "https://storage.googleapis.com/storage/v1/b/$bucket/o/$(printf '%s' "$object" | sed 's|/|%2F|g')?alt=media" ||
    die "could not download gs://$bucket/$object"
  systemctl stop copilot-proxy copilot-gateway copilot-api copilot-worker copilot-scheduler.timer
  wait_data
  # Roles first (a new disk has none), then the dump over the current schema, then the
  # roles' logins again.
  [ "$(tag)" != none ] && docker_run migrate wait
  docker exec -i copilot-postgres pg_restore -U postgres -d copilot --clean --if-exists < "$file" ||
    log "pg_restore reported problems (above); check before using the app"
  rm -f "$file"
  [ "$(tag)" != none ] && docker_run migrate wait
  systemctl start copilot-scheduler.timer
  [ "$(tag)" != none ] && systemctl start "${APP_SERVICES[@]/#/copilot-}"
  log "restored gs://$bucket/$object"
}

case ${1:-} in
  boot) boot ;;
  run) docker rm -f "copilot-$2" >/dev/null 2>&1 || true; docker_run "$2" exec ;;
  deploy) deploy "${2:-}" ;;
  rollback) rollback ;;
  status) status ;;
  restart) shift; systemctl restart "${@/#/copilot-}" ;;
  scheduler)
    [ "$(tag)" = none ] && { log "no deploy yet; skipping the tick"; exit 0; }
    docker_run scheduler wait ;;
  backup) backup ;;
  restore) restore "${2:-}" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac

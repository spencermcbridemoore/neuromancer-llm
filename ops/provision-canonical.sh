#!/usr/bin/env bash
# ops/provision-canonical.sh
# =============================================================================
# CANONICAL Jetstream2 Postgres VM — infrastructure provisioning / DR rebuild.
#
# WHAT THIS IS (ADR-0041 "docs-that-cannot-rot", and the X8 runbook's own TODO
# "Capture flavor/image/key in a provisioning script (ADR-0041)"): the procedure
# to (re)create the canonical box, RECONSTRUCTED as code from the execution
# records. The *state* of the box was documented; the *procedure* was not. This
# file closes that gap.
#
# SOURCES OF TRUTH — every value below traces to one of these (cited inline):
#   - scratch/a1-execution-record-2026-07-02.md   §1 (PRIMARY: as-provisioned values)
#   - scratch/x8-execution-record-2026-07-02.md    §5 (OpenStack ordering gotchas)
#   - scratch/a1-canonical-pg-runbook-2026-07-02.md    (the [OWNER] prose steps)
#   - scratch/x8-checkpoint-runbook-2026-06-29.md      (the staging rehearsal)
#   - docs/adr/0041 (native PGDG PG on the VM) · docs/adr/0042 (PGDATA+WAL on Cinder)
# Reconstructed 2026-07-04 from branch phase4-stage1 @ HEAD 9931e43. No value here
# is invented; where the records do not capture a step it is marked GAP and the
# script FAILS CLOSED rather than guessing.
#
# ------------------------------------------------------------------------------
# TWO PROVISIONING LAYERS — THIS SCRIPT COVERS ONLY LAYER (1).
#
#   (1) INFRASTRUCTURE  <-- THIS SCRIPT
#       openstack VM + Cinder volume + network + SG + keypair + floating IP,
#       then mount the volume, install/seat native PGDG Postgres 18, Tailscale,
#       loopback lockdown, and the /etc/neuro/env secrets file.
#
#   (2) IDENTITY        <-- DELIBERATELY NOT IN THIS SCRIPT
#       `neuro db provision --lane canonical`. Once-only, fail-closed: provision()
#       refuses a populated database_identity row (a1-canonical-pg-runbook Posture;
#       provision.py:45-49). The instance_uuid is minted AT PROVISION TIME and is
#       repo-pinned in db/canonical_instance.py. A REBUILD IS A RESTORE, NEVER A
#       RE-PROVISION. See the loud STOP block in Section 9.
#
# ------------------------------------------------------------------------------
# TWO DR SCENARIOS — you MUST pick one via SCENARIO=A or SCENARIO=B.
#
#   Scenario A — the Cinder volume survived.
#     `neuro-pgdata` has Delete-on-Termination=False (a1-execution-record §1), so
#     it outlives the VM. Recreate the VM, RE-ATTACH the existing volume, remount
#     it by its recorded ext4 UUID, ADOPT the retained cluster. The pinned data
#     (incl. the identity row) is already intact — NO restore, NO re-provision.
#
#   Scenario B — the volume was also lost.
#     Recreate the VM + a FRESH volume, seat an empty PGDG 18 cluster, then RESTORE
#     from the pgbackrest backup / desktop-NVMe copy (Stage A Part 2 / A2-7), which
#     brings back the pinned uuid. Still NO `neuro db provision`.
#
# ------------------------------------------------------------------------------
# HOW TO RUN: this is a SECTIONED, HUMAN-DRIVEN runbook-as-code — NOT a
# fire-and-forget button. Read it top to bottom, then run one section at a time,
# copy-pasting the on-VM sections into an SSH session on the box. Each `### RUN
# ON: ... ###` banner says where a section executes (laptop OpenStack CLI vs the
# VM). Infra creates are existence-guarded so an accidental re-run is a no-op.
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# 0. PARAMETERS  (owner/account-specific values are variables; recorded defaults
#    are shown; a rebuild mints FRESH resource UUIDs so creates key off NAMES.)
# -----------------------------------------------------------------------------
# Which DR scenario — REQUIRED. "A" = volume survived, "B" = volume lost too.
SCENARIO="${SCENARIO:-}"

# OpenStack app-credential auth (a1-canonical-pg-runbook §1: app-credential auth
# via ~/.config/openstack/clouds.yaml chmod 600). Name of the clouds.yaml entry.
OS_CLOUD="${OS_CLOUD:-}"

# Recorded IU allocation (a1-execution-record §1). Override for a different project.
PROJECT_ID="${PROJECT_ID:-3d787857d84043fb898b5162eb5cf786}"

# Owner's current admin source /32 — REQUIRED, no default (do NOT hardcode a home IP).
# Get it via:  curl https://ifconfig.me   (HTTPS is load-bearing: CenturyLink plain-HTTP
# interception returns an empty body -> an "Invalid CIDR /32" SG error — X8 §5 / a1 Preflight.)
ADMIN_CIDR="${ADMIN_CIDR:-}"

# Names (a1-execution-record §1 / a1-canonical-pg-runbook §1 — never the throwaway x8-* names).
VM_NAME="${VM_NAME:-neuro-canonical-pg}"
VOLUME_NAME="${VOLUME_NAME:-neuro-pgdata}"
SG_NAME="${SG_NAME:-neuro-ssh}"
KEY_NAME="${KEY_NAME:-neuro-key}"
KEY_FILE="${KEY_FILE:-$HOME/.ssh/neuro_canonical}"   # laptop-side private key, passphrase-protected (a1-exec §1)

# Flavor / image / network / volume (a1-execution-record §1; ADR-0001/0042 — NO VM resize).
FLAVOR="${FLAVOR:-m3.small}"                 # 2 vCPU / 6 GB / 20 GB root
IMAGE="${IMAGE:-Featured-Ubuntu24}"          # Ubuntu 24.04
NETWORK="${NETWORK:-auto_allocated_network}" # pre-existing, pre-routed to public — DO NOT create a network (X8 §5)
VOLUME_SIZE_GB="${VOLUME_SIZE_GB:-150}"      # 150 GB zero-SU Cinder (ADR-0042; §6 #9 CONFIRMED)

# Postgres / storage layout (a1-execution-record §1; ADR-0042 PGDATA+WAL on Cinder).
PG_MAJOR="${PG_MAJOR:-18}"                    # PGDG 18.x — HARD precondition (Ubuntu 24.04 default is 16, X8 §5 #1)
PGDATA="${PGDATA:-/pgdata}"                   # mount point; datadir = ${PGDATA}/${PG_MAJOR}/main
# Recorded ext4 UUID of the RETAINED volume — used to remount in Scenario A (a1-execution-record §1).
PGDATA_FS_UUID="${PGDATA_FS_UUID:-a9ae45fb-7da0-4be7-a084-2733e0579230}"

# Code pin. A1 provisioned at 19c75c9 then moved the checkout to 6083a9a — the A1-15
# commit that ADDS db/canonical_instance.py (the repo pin). A rebuild's verify (Section 10)
# resolves that pin, so the checkout MUST contain it: use 6083a9a or a later audited HEAD
# (e.g. current phase4-stage1 HEAD 9931e43). (a1-execution-record §8; a1-canonical-pg-runbook §10.)
NEURO_REF="${NEURO_REF:-6083a9a}"
REPO_URL="${REPO_URL:-https://github.com/<owner>/neuromancer-llm.git}"   # set <owner>

# REFERENCE ONLY — the pinned canonical identity. This script NEVER mints or writes it;
# it is repo-pinned in db/canonical_instance.py and returns with the data (A) or the restore (B).
CANONICAL_UUID="c7a1b953-485a-4127-a2e6-a18f7423742a"   # a1-execution-record §1 / A1-12 deliverable

# --- Recorded LIVE resource identities (REFERENCE COMMENTS ONLY; a rebuild is fresh) ---
#   VM      neuro-canonical-pg   b72118fd-7062-48a2-b8d5-e5b5707bf240   (a1-execution-record §1)
#   Volume  neuro-pgdata         8b8a4fd8-fcd6-4f7c-8a68-67acb9144004   (a1-execution-record §1)
#   VM fixed IP 10.3.235.14 · bring-up floating IP 149.165.174.93 (RELEASED) · tailnet node 100.86.240.4
#   The create commands below key off NAMES + flavor/image/size, not these UUIDs.

# -----------------------------------------------------------------------------
# Preflight guards — refuse to run without an explicit scenario + admin CIDR.
# (Keeps an accidental `bash provision-canonical.sh` a safe no-op.)
# -----------------------------------------------------------------------------
case "${SCENARIO}" in
  A|B) : ;;
  *)
    cat >&2 <<'USAGE'
provision-canonical.sh — sectioned DR runbook, run one section at a time.

  SCENARIO=A  ->  Cinder volume `neuro-pgdata` survived (re-attach + adopt cluster)
  SCENARIO=B  ->  volume lost too (fresh volume + RESTORE from A2-7 backup)

Set the scenario and (for the OpenStack sections) an admin CIDR, then read and
run the relevant sections by hand:

  export SCENARIO=A
  export ADMIN_CIDR="$(curl -s https://ifconfig.me)/32"   # HTTPS! (X8 §5)
  export OS_CLOUD=<your-clouds.yaml-entry>

This is NOT fire-and-forget. Do not pipe the whole file to bash.
USAGE
    exit 2
    ;;
esac

[[ -n "${OS_CLOUD}" ]] && export OS_CLOUD   # openstack CLI reads $OS_CLOUD automatically

echo "== canonical provisioning :: SCENARIO=${SCENARIO} :: VM=${VM_NAME} vol=${VOLUME_NAME} =="
echo "== reminder: this is LAYER 1 (infrastructure) only. Identity is a RESTORE, never a re-provision. =="


# =============================================================================
# 1. VM + network + security group + keypair + floating IP     ### RUN ON: laptop (OpenStack CLI) ###
#    (a1-canonical-pg-runbook §1 steps 1-2; a1-execution-record §1)
# =============================================================================
if [[ -z "${ADMIN_CIDR}" ]]; then
  echo "ERROR: ADMIN_CIDR is required for the OpenStack sections (owner's /32 via https://ifconfig.me)." >&2
  exit 2
fi

# Sanity: confirm we are pointed at the recorded allocation (soft check).
openstack project show "${PROJECT_ID}" >/dev/null 2>&1 \
  || echo "warn: project ${PROJECT_ID} not visible under OS_CLOUD='${OS_CLOUD}' — check clouds.yaml."

# Keypair — fresh ed25519, passphrase-protected, laptop-side ${KEY_FILE} (a1-execution-record §1).
# NOTE: once Tailscale SSH is up (Section 6), admin is keyless over the tailnet (tailnet identity,
# a1-execution-record §4 #3); this keypair only bootstraps the first floating-IP SSH.
if openstack keypair show "${KEY_NAME}" >/dev/null 2>&1; then
  echo "keypair ${KEY_NAME} already registered — skipping."
else
  if [[ ! -f "${KEY_FILE}.pub" ]]; then
    echo "generate the laptop key first (prompts for a passphrase — set one):"
    echo "    ssh-keygen -t ed25519 -f '${KEY_FILE}'"
    exit 2
  fi
  openstack keypair create --public-key "${KEY_FILE}.pub" "${KEY_NAME}"
fi

# Security group — TCP/22 from ${ADMIN_CIDR} ONLY (a1-execution-record §1: owner /32 only).
if openstack security group show "${SG_NAME}" >/dev/null 2>&1; then
  echo "security group ${SG_NAME} exists — skipping (verify the /32 rule by hand if unsure)."
else
  openstack security group create "${SG_NAME}" --description "canonical admin SSH (owner /32 only)"
  openstack security group rule create --proto tcp --dst-port 22 --remote-ip "${ADMIN_CIDR}" "${SG_NAME}"
fi

# NETWORK: reuse the pre-existing, pre-routed auto_allocated_network — do NOT create one (X8 §5).
# VM: flavor m3.small, image Featured-Ubuntu24 (a1-execution-record §1; ADR-0001/0042 — no resize).
if openstack server show "${VM_NAME}" >/dev/null 2>&1; then
  echo "server ${VM_NAME} exists — skipping create."
else
  openstack server create \
    --flavor "${FLAVOR}" \
    --image "${IMAGE}" \
    --network "${NETWORK}" \
    --security-group "${SG_NAME}" \
    --key-name "${KEY_NAME}" \
    --wait \
    "${VM_NAME}"
fi

# Floating IP — for BRING-UP ONLY; released in Section 11 (admin is tailnet-only, a1-execution-record §1).
# The addresses field is "network=<fixed>[, <floating>]"; a comma means a floating IP is already attached.
ADDR="$(openstack server show "${VM_NAME}" -f value -c addresses)"
if printf '%s' "${ADDR}" | grep -q ','; then
  FIP="$(printf '%s' "${ADDR}" | awk -F', ' '{print $2}')"
  echo "server already has floating IP ${FIP} — skipping allocation."
else
  FIP="$(openstack floating ip create public -f value -c floating_ip_address)"
  openstack server add floating ip "${VM_NAME}" "${FIP}"
fi
echo "bring-up floating IP: ${FIP:-<unknown — see: openstack floating ip list>}  (temporary; released in Section 11)"
echo "SSH in with:  ssh -i '${KEY_FILE}' ubuntu@${FIP:-<fip>}"


# =============================================================================
# 2. Attach the Cinder volume                                  ### RUN ON: laptop (OpenStack CLI) ###
#    Attach device is /dev/sdb exactly as reported by `openstack server add volume` (X8 §5).
# =============================================================================
if [[ "${SCENARIO}" == "A" ]]; then
  # Scenario A: the RETAINED volume `neuro-pgdata` still exists (its recorded STATE is
  # Delete-on-Termination=False — a1-execution-record §1). Re-attach it — DO NOT create,
  # DO NOT delete, DO NOT reformat.
  if ! openstack volume show "${VOLUME_NAME}" >/dev/null 2>&1; then
    echo "ERROR: Scenario A expects the retained volume ${VOLUME_NAME} to exist, but it does not." >&2
    echo "       If the volume is truly gone, this is Scenario B (fresh volume + restore)." >&2
    exit 1
  fi
else
  # Scenario B: the volume was lost. Create a FRESH ${VOLUME_SIZE_GB} GB Cinder volume.
  openstack volume show "${VOLUME_NAME}" >/dev/null 2>&1 \
    || openstack volume create --size "${VOLUME_SIZE_GB}" "${VOLUME_NAME}"
fi

# Attach only if not already attached (guard on volume status, not the id-formatted attach list).
VOL_STATUS="$(openstack volume show "${VOLUME_NAME}" -f value -c status 2>/dev/null || echo missing)"
if [[ "${VOL_STATUS}" == "in-use" ]]; then
  echo "volume ${VOLUME_NAME} already attached (status in-use) — skipping."
else
  openstack server add volume "${VM_NAME}" "${VOLUME_NAME}"    # expect /dev/sdb (X8 §5)
fi

# --- Retain-on-detach is a STATE the records attest, NOT a command they capture ---
# a1-execution-record §1: "Delete On Termination: False — retain-on-detach, never delete".
# The records document only the resulting STATE, not the attach-time flag that produced it, so this
# script does NOT assert one. A standalone Cinder volume attached this way is retain-on-detach by
# default; the exact CLI flag to FORCE it (should a client/cloud default ever differ) is a GAP —
# uncaptured in any record. VERIFY the state by hand here, and if it does not read False, set it per
# your OpenStack client BEFORE trusting the volume to outlive the VM. This retain property (however
# it is set) is what makes a future Scenario-A recovery possible — so it must hold on EVERY rebuild.
echo "VERIFY retain-on-detach — the 'Delete On Termination' column for ${VOLUME_NAME} MUST read False:"
openstack server volume list "${VM_NAME}"
echo "verify on the VM:  lsblk   # the ${VOLUME_SIZE_GB} GB device should be /dev/sdb"


# =============================================================================
# 3. Mount /pgdata (by ext4 UUID, noatime)                     ### RUN ON: the VM (over SSH) ###
#    fstab-by-UUID survives reboots incl. the JS2 maintenance window (a1-canonical-pg-runbook §1).
# =============================================================================
if [[ "${SCENARIO}" == "A" ]]; then
  # Scenario A — the filesystem (and all data) is already on the retained volume. DO NOT mkfs.
  # Mount by the RECORDED ext4 UUID (a1-execution-record §1).
  echo "sanity: /dev/sdb UUID should equal the recorded ${PGDATA_FS_UUID}:"
  sudo blkid -s UUID -o value /dev/sdb || true
  sudo mkdir -p "${PGDATA}"
  grep -q "${PGDATA_FS_UUID}" /etc/fstab \
    || echo "UUID=${PGDATA_FS_UUID} ${PGDATA} ext4 defaults,noatime 0 2" | sudo tee -a /etc/fstab
  sudo systemctl daemon-reload
  mountpoint -q "${PGDATA}" || sudo mount "${PGDATA}"
else
  # Scenario B — fresh volume: format ext4, then fstab by its NEW UUID (blkid), noatime.
  if ! sudo blkid /dev/sdb >/dev/null 2>&1; then
    sudo mkfs.ext4 /dev/sdb
  fi
  sudo mkdir -p "${PGDATA}"
  NEW_UUID="$(sudo blkid -s UUID -o value /dev/sdb)"
  grep -q "${NEW_UUID}" /etc/fstab \
    || echo "UUID=${NEW_UUID} ${PGDATA} ext4 defaults,noatime 0 2" | sudo tee -a /etc/fstab
  sudo systemctl daemon-reload
  mountpoint -q "${PGDATA}" || sudo mount "${PGDATA}"
fi
echo "mounted: $(findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS "${PGDATA}" 2>/dev/null || echo "${PGDATA} (check mount)")"


# =============================================================================
# 4. Native PGDG Postgres 18 + seat the cluster on /pgdata     ### RUN ON: the VM (over SSH) ###
#    PGDG not distro (X8 §5 #1), native not containerized (ADR-0041),
#    pg_createcluster not raw initdb (X8 §5 #2), PGDATA+WAL on Cinder (ADR-0042).
# =============================================================================
# Repo + package list (fresh cloud images may have stale apt lists — a1-canonical-pg-runbook §2):
sudo apt update && sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
sudo apt install -y "postgresql-${PG_MAJOR}"
# ^ Installing postgresql-${PG_MAJOR} AUTO-CREATES an empty '${PG_MAJOR} main' cluster on the ROOT disk.

if [[ "${SCENARIO}" == "B" ]]; then
  # Scenario B — seat a FRESH empty cluster ON THE CINDER MOUNT (the PGDG/Debian-clean way).
  # data-page checksums are ON by default on PG 18 / PGDG (a1-execution-record §1; X8-confirmed) — verify below.
  sudo pg_dropcluster --stop "${PG_MAJOR}" main
  sudo pg_createcluster "${PG_MAJOR}" main --datadir="${PGDATA}/${PG_MAJOR}/main" --start
  sudo systemctl enable postgresql
else
  # Scenario A — ADOPT the RETAINED cluster at ${PGDATA}/${PG_MAJOR}/main. Drop ONLY the empty
  # auto-created root-disk cluster, then register the retained datadir WITHOUT re-initdb.
  sudo pg_dropcluster --stop "${PG_MAJOR}" main   # removes the EMPTY root-disk cluster only (retained datadir untouched)
  cat >&2 <<GAP
  ############################################################################
  # !!! GAP / UNEXERCISED — STOP AND VERIFY AGAINST A2-9 BEFORE CONTINUING !!!
  #
  # The exact command to REGISTER an existing/retained datadir as cluster
  # '${PG_MAJOR} main' WITHOUT re-initdb is NOT captured in ANY execution
  # record. Both a1-canonical-pg-runbook (Rollback) and a1-execution-record
  # (§5) flag the "cluster-adoption over a retained datadir" step as
  # UNEXERCISED — X8 deleted its volume, so recovery was never performed.
  # This is exactly what A2-9's restore drill exists to prove.
  #
  # DO NOT GUESS a pg_createcluster/initdb invocation here — a wrong one
  # will CLOBBER the pinned data on the retained volume. Bring the retained
  # cluster online per the (to-be-proven) A2-9 procedure, confirm
  # pg_lsclusters shows it 'online' on ${PGDATA}/${PG_MAJOR}/main, then
  # resume at Section 6 (Tailscale). Section 5 (restore) is Scenario B only.
  ############################################################################
GAP
  exit 1   # fail closed: refuse to fabricate the unexercised adoption step
fi

# Substrate gates (a1-canonical-pg-runbook §2 / a1-execution-record §3):
pg_lsclusters                                               # ${PG_MAJOR} main 5432 online postgres ${PGDATA}/${PG_MAJOR}/main
cat "/etc/postgresql/${PG_MAJOR}/main/start.conf"           # MUST say: auto  (reboot self-heal)
sudo -u postgres psql -tAc "show server_version"            # MUST print ${PG_MAJOR}.x
sudo -u postgres psql -tAc "show data_checksums"            # expect: on (PG 18 / PGDG default)
# Strongest reboot check while the DB is otherwise idle: `sudo reboot`, then confirm pg_lsclusters
# shows 'online' and ${PGDATA} is mounted (a1-canonical-pg-runbook §2 — cheap now, impossible later).


# =============================================================================
# 5. Scenario B ONLY — RESTORE the pinned data                 ### RUN ON: the VM (over SSH) ###
# =============================================================================
if [[ "${SCENARIO}" == "B" ]]; then
  cat >&2 <<RESTORE
  ############################################################################
  # !!! DEPENDENCY / NOT-YET-BUILT — RESTORE IS A2-7 (Stage A Part 2) !!!
  #
  # The fresh cluster from Section 4 is EMPTY and carries a NEW (wrong)
  # identity. It MUST be OVERWRITTEN by a RESTORE from the pgbackrest backup /
  # desktop-NVMe copy (a1-canonical-pg-runbook §11; go-remote plan A2-7), which
  # brings back the PINNED instance_uuid ${CANONICAL_UUID} together with all
  # roles and data. THIS IS A RESTORE, NOT A RE-PROVISION.
  #
  # As of HEAD 9931e43 the pgbackrest restore path (A2-7) is UNBUILT and no
  # restore command is captured in any record. Placeholder shape only:
  #
  #     sudo -u postgres pgbackrest --stanza=<stanza-name-TBD-in-A2-7> restore   # per the FUTURE A2-7 procedure
  #
  # After the restore, the cluster carries the pinned uuid; verify in Section 10.
  # DO NOT run 'neuro db provision'.
  ############################################################################
RESTORE
  exit 1   # fail closed until A2-7 (restore) exists
fi


# =============================================================================
# 6. Tailscale admin path                                      ### RUN ON: the VM (over SSH) ###
#    (a1-canonical-pg-runbook §3; a1-execution-record §1 — tailscale 1.98.8, node 100.86.240.4)
# =============================================================================
command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh          # Tailscale SSH (keyless, tailnet identity); auth via the owner's tailnet
tailscale status                 # acceptance: this node listed as connected
echo "Now prove tailnet SSH FROM THE LAPTOP over the tailnet (not the floating IP):"
echo "    ssh ubuntu@<tailnet-ip>     # a1-execution-record recorded 100.86.240.4 — a rebuild gets a fresh tailnet IP"
echo "PG is NOT exposed on the tailnet in Stage A (loopback only — Section 7; remote logins are Stage B)."


# =============================================================================
# 7. Lock PG to loopback (VERIFY — Debian/PGDG default)        ### RUN ON: the VM (over SSH) ###
#    (a1-canonical-pg-runbook §4; a1-execution-record §1 — verify, don't assume, on a permanent box.)
# =============================================================================
sudo -u postgres psql -tAc "show listen_addresses"                              # must be 'localhost' (or '127.0.0.1, ::1')
sudo grep -vE '^\s*(#|$)' "/etc/postgresql/${PG_MAJOR}/main/pg_hba.conf"        # local peer + host scram, loopback only
ss -tlnp | grep 5432 || true                                                    # listening on 127.0.0.1/::1 ONLY
# Do NOT add tailnet/pg_hba rows in Stage A.


# =============================================================================
# 8. Secrets file /etc/neuro/env                               ### RUN ON: the VM (over SSH) ###
#    root:root 0600 (§6 #5 posture; a1-execution-record §1 / a1-canonical-pg-runbook §7).
#    In a REBUILD the roles + identity RETURN with the cluster (A) or the restore (B) —
#    do NOT CREATE ROLE here. We only re-seat the env pointing at the EXISTING neuro_boot login.
# =============================================================================
# --- Silent-read secret pattern (a1-execution-record §4 wrinkle #1/#2, §5) ---
# `read` on its OWN line; the value lands in a shell VAR only — never argv, never shell history,
# never a command-line arg. Passwords are 32-char ALPHANUMERIC (URL-safe rule), so there are no
# quoting hazards in the DSN. Paste the password when prompted (nothing echoes).
printf 'existing neuro_boot password (from the password manager): '
read -rs BOOT_PW
printf '\n'

BOOT_DSN="postgresql+psycopg://neuro_boot:${BOOT_PW}@127.0.0.1:5432/neuro"
sudo install -d -m 0755 -o root -g root /etc/neuro
# The DSN reaches the file via tee's STDIN (a pipe), not argv/history — the file IS the secret store.
sudo tee /etc/neuro/env >/dev/null <<EOF
NEURO_DATABASE_URL=${BOOT_DSN}
NEURO_MIGRATION_EXPECTED_LANE=canonical
# Part-2 orchestrator DSN (commented until needed) — a1-execution-record §1 Secrets, minted via A1-10:
# NEURO_DATABASE_URL=postgresql+psycopg://neuro_orch:<orch-pw>@127.0.0.1:5432/neuro
EOF
sudo chown root:root /etc/neuro/env
sudo chmod 600 /etc/neuro/env
unset BOOT_PW BOOT_DSN
echo "seated /etc/neuro/env (root:root 0600)."
# When orchestration becomes a daemon (Part 2, A2-16), migrate this to systemd LoadCredential.


# =============================================================================
# 9. !!! STOP — IDENTITY PROVISIONING IS FORBIDDEN IN A REBUILD !!!
# =============================================================================
cat <<STOP
###############################################################################
#                                                                             #
#   STOP. This is where the ORIGINAL A1 bring-up ran, exactly once:           #
#                                                                             #
#       neuro db migrate                                                       #
#       neuro db provision --lane canonical    <-- MINTS a NEW instance_uuid   #
#       neuro db roles                                                         #
#                                                                             #
#   IN A REBUILD YOU RUN NONE OF THESE.                                        #
#                                                                             #
#   The canonical identity + roles + data return via the RETAINED volume      #
#   (Scenario A) or the A2-7 RESTORE (Scenario B). Running 'provision' here    #
#   would MINT A FRESH, DIFFERENT uuid, which then FAILS                       #
#   'neuro db verify --lane canonical' against the repo pin                    #
#   ${CANONICAL_UUID} in db/canonical_instance.py (fail closed).              #
#                                                                             #
#   A REBUILD IS A RESTORE, NEVER A RE-PROVISION.                             #
#   (a1-canonical-pg-runbook Posture/Rollback; §10 A1-15 repo pin.)           #
#                                                                             #
#   The ONLY neuro command below is READ-ONLY 'verify'. Proceed to Section 10.#
#                                                                             #
###############################################################################
STOP


# =============================================================================
# 10. Code checkout to the pin + READ-ONLY verify              ### RUN ON: the VM (over SSH) ###
#     Closing acceptance (a1-execution-record §8): verify resolves the repo pin flaglessly.
# =============================================================================
# Source the bootstrap DSN for the verify connection (env = infra-config only, L13).
set -a
# shellcheck disable=SC1090  # sourcing the runtime-generated /etc/neuro/env (the recorded pattern)
. <(sudo cat /etc/neuro/env)
set +a

if [[ ! -d "${HOME}/neuromancer-llm/.git" ]]; then
  git clone "${REPO_URL}" "${HOME}/neuromancer-llm"
fi
cd "${HOME}/neuromancer-llm"
git fetch --all --tags
git checkout "${NEURO_REF}"        # DETACHED; MUST contain db/canonical_instance.py (the pin)

# shellcheck disable=SC1091  # ${HOME}/.local/bin/env is written by the uv installer at runtime
command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; . "${HOME}/.local/bin/env"; }
uv sync
uv run neuro --help >/dev/null     # smoke: CLI resolves through uv

# POSITIVE — resolves the repo pin with NO flag (a1-execution-record §8):
echo "expect:  verified: lane=canonical instance_uuid=${CANONICAL_UUID}"
uv run neuro db verify --lane canonical

# NEGATIVE — a wrong pin must fail closed (a1-execution-record §8):
echo "expect:  error (fail closed): instance_uuid mismatch: ... (fail closed)."
uv run neuro db verify --lane canonical --expected-uuid 00000000-0000-4000-8000-000000000000 || true


# =============================================================================
# 11. Release the bring-up floating IP                         ### RUN ON: laptop (OpenStack CLI) ###
#     Admin is tailnet-only (a1-execution-record §1). SG stays /32-pinned as break-glass.
# =============================================================================
# Only after tailnet SSH (Section 6) is PROVEN. If $FIP was lost between sessions,
# find it via: openstack floating ip list
if [[ -n "${FIP:-}" ]]; then
  openstack server remove floating ip "${VM_NAME}" "${FIP}" || true
  openstack floating ip delete "${FIP}" || true
  echo "released floating IP ${FIP}. Admin path is now tailnet-only; ${SG_NAME} stays /32-pinned as break-glass."
else
  echo "no \$FIP in this shell — release manually: openstack floating ip list; openstack floating ip delete <ip>"
fi

echo "== canonical infrastructure (layer 1) complete for SCENARIO=${SCENARIO}. Identity was NOT provisioned. =="


# =============================================================================
# APPENDIX — decommission ordering (COMMENTED; do NOT run casually)
#   Only if the canonical VM is ever FULLY decommissioned. Ordering gotcha from
#   X8 §4/§5 + a1-canonical-pg-runbook Rollback. NEVER delete the PGDATA volume.
# =============================================================================
#   openstack server remove volume "${VM_NAME}" "${VOLUME_NAME}"   # DETACH — retain-on-detach
#   openstack server delete --wait "${VM_NAME}"                    # --wait is load-bearing: delete is
#        # async; the VM can briefly land in ERROR and hold the SG, 409'ing an early SG delete (X8 §4 #1)
#   openstack security group delete "${SG_NAME}"                   # ONLY after the server is fully gone
#   openstack keypair delete "${KEY_NAME}"                         # floating IP already released (Section 11)
#   # NEVER:  openstack volume delete "${VOLUME_NAME}"   # it holds the pinned data (a1-execution-record §1)
# =============================================================================

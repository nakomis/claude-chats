#!/usr/bin/env bash
# Restore the claude_chats database from a monthly-archive backup (CHATS-41):
# the newest nightly dump, then every archived month from before its cutoff.
#
# Usage: scripts/restore.sh [--local] [DIR | FILE.sql.gz] [--yes] [--no-backfill]
#        scripts/restore.sh --s3 [NIGHTLY-KEY] [--yes] [--no-backfill]
#
#   --local  (default) a backup made by scripts/backup.sh: DIR (default
#            $CLAUDE_CHATS_BACKUP_DIR, else ~/.claude-chats/backups) holds
#            nightly/ and archive/. Give a FILE to restore an older nightly
#            than the newest; its archive is still DIR/archive. A dump from
#            before CHATS-41 (a whole database, no archive) restores on its own.
#   --s3     the S3 target, as Luke writes it: the newest (or the named) object
#            under s3://nakomis-taiga-backups/${BACKUP_PREFIX}conversation-memory/
#            and the months in s3://nakomis-convmem-archive/${BACKUP_PREFIX}messages/,
#            read as $CONVMEM_RESTORE_PROFILE (default nakom.is-admin: the
#            backup identity cannot read objects, by design).
#   --yes        do not ask before wiping the database
#   --no-backfill  leave messages without a vector unembedded
#   --allow-missing  restore even if archived months the nightly relies on
#                  cannot be found (otherwise nothing is changed)
#
# Into the claude-chats-db container (CONVMEM_CONTAINER), through `docker exec`:
# the host needs no Postgres client. The nightly says which month it covers
# from (`-- messages from YYYY-MM-01`) and which archived months it relies on;
# those come from the archive, each from its newest intact object, and every
# one is found, fetched and tested before the database is touched. Row counts
# are checked against each name as it loads.
#
# WARNING: this drops and recreates the claude_chats database.
#          Run backup.sh first if the current contents matter.
set -euo pipefail

SRC="local"
ARG=""
YES=""
ALLOW_MISSING=""
BACKFILL="yes"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local)       SRC="local"; shift ;;
        --s3)          SRC="s3"; shift ;;
        --yes|-y)      YES="yes"; shift ;;
        --no-backfill) BACKFILL=""; shift ;;
        --allow-missing) ALLOW_MISSING="yes"; shift ;;
        -h|--help)     sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --*)           echo "[restore] unknown option '$1'" >&2; exit 2 ;;
        *)             ARG="$1"; shift ;;
    esac
done

# shellcheck disable=SC1091
[ -r /etc/default/luke-backup ] && . /etc/default/luke-backup
BACKUP_PREFIX="${BACKUP_PREFIX:-}"

BACKUP_DIR="${CLAUDE_CHATS_BACKUP_DIR:-${HOME}/.claude-chats/backups}"
NIGHTLY_BUCKET="${CONVMEM_NIGHTLY_BUCKET:-nakomis-taiga-backups}"
NIGHTLY_PREFIX="${BACKUP_PREFIX}conversation-memory/"
ARCHIVE_BUCKET="${CONVMEM_ARCHIVE_BUCKET:-nakomis-convmem-archive}"
ARCHIVE_PREFIX="${BACKUP_PREFIX}messages/"
RESTORE_PROFILE="${CONVMEM_RESTORE_PROFILE:-nakom.is-admin}"
REGION="${AWS_REGION_BACKUP:-eu-west-2}"

CONTAINER="${CONVMEM_CONTAINER:-${CLAUDE_CHATS_CONTAINER:-claude-chats-db}}"
DOCKER="${CONVMEM_DOCKER:-docker}"
DB_NAME="${CONVMEM_DB_NAME:-claude_chats}"
DB_USER="${CONVMEM_DB_USER:-claude}"
DB_URL="${CLAUDE_CHATS_DB_URL:-postgresql://claude:claude@localhost:5433/${DB_NAME}}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "[restore] ERROR: $*" >&2; exit 1; }
say() { echo "[restore] $*"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/claude-chats-restore-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

psql_in() {
    "$DOCKER" exec -i "$CONTAINER" psql -h /var/run/postgresql -U "$DB_USER" "$@"
}

s3() {
    AWS_PROFILE="$RESTORE_PROFILE" aws s3 "$@" --region "$REGION"
}

# ── 1. Which nightly ────────────────────────────────────────────────────────

ARCHIVE_DIR=""
if [[ "$SRC" == "local" ]]; then
    if [[ -n "$ARG" && -f "$ARG" ]]; then
        NIGHTLY="$ARG"
        # A nightly in DIR/nightly/ belongs with DIR/archive/.
        parent="$(cd "$(dirname "$ARG")" && pwd)"
        if [[ "$(basename "$parent")" == "nightly" ]]; then
            BACKUP_DIR="$(dirname "$parent")"
        fi
    else
        [[ -n "$ARG" ]] && BACKUP_DIR="$ARG"
        [[ -d "$BACKUP_DIR" ]] || die "no such directory: ${BACKUP_DIR}"
        NIGHTLY="$(ls -1 "${BACKUP_DIR}"/nightly/claude_chats_*.sql.gz 2>/dev/null | sort | tail -1 || true)"
        if [[ -z "$NIGHTLY" ]]; then
            # Before CHATS-41 backup.sh wrote whole-database dumps straight
            # into the directory.
            NIGHTLY="$(ls -1 "${BACKUP_DIR}"/claude_chats_*.sql.gz 2>/dev/null | sort | tail -1 || true)"
        fi
        [[ -n "$NIGHTLY" ]] || die "no backups in ${BACKUP_DIR}"
    fi
    ARCHIVE_DIR="${CONVMEM_ARCHIVE_DIR:-${BACKUP_DIR%/}/archive}"
    LABEL="$NIGHTLY"
else
    if [[ -n "$ARG" ]]; then
        KEY="$ARG"
    else
        KEY="$(s3 ls "s3://${NIGHTLY_BUCKET}/${NIGHTLY_PREFIX}" | awk '{print $4}' \
               | grep '\.sql\.gz$' | sort | tail -1 || true)"
        [[ -n "$KEY" ]] || die "no nightly dumps under s3://${NIGHTLY_BUCKET}/${NIGHTLY_PREFIX}"
        KEY="${NIGHTLY_PREFIX}${KEY}"
    fi
    LABEL="s3://${NIGHTLY_BUCKET}/${KEY}"
    NIGHTLY="${WORK}/nightly.sql.gz"
    say "downloading ${LABEL}"
    s3 cp "s3://${NIGHTLY_BUCKET}/${KEY}" "$NIGHTLY" --no-progress >/dev/null
fi

gzip -t "$NIGHTLY" || die "${LABEL} is not a complete gzip stream"

# `|| true`: head closing the pipe early is not a failure.
(gunzip -c "$NIGHTLY" || true) | head -6 >"${WORK}/header"
CUTOFF="$(sed -n 's/^-- messages from \([0-9]\{4\}-[0-9]\{2\}\)-01;.*/\1/p' "${WORK}/header" | head -1)"
# Nightlies since CHATS-41's review name every month they rely on the archive
# for; older ones do not, and then only "none at all" can be caught.
EXPECTED_MONTHS="$(sed -n 's/^-- archived months: *//p' "${WORK}/header" | head -1)"
HAS_EXPECTED="$(grep -c '^-- archived months:' "${WORK}/header" || true)"

# ── 2. Which archived months, all checked before anything is wiped ──────────
#
# Listed as "date time size name", oldest first. For each month the newest
# INTACT object is used (its size equals the `b` in its name): a local archive
# keeps one file per month, but S3 can hold several, because a re-cut there
# cannot delete what it replaces, and a bad newest one must not hide a good one.

listing() {
    if [[ "$SRC" == "local" ]]; then
        local f
        for f in "${ARCHIVE_DIR%/}/messages/"messages_*.csv.gz; do
            [[ -f "$f" ]] || continue
            # The file's age, as a sortable stamp, stands in for S3's LastModified.
            printf '%s %s %s %s\n' "$(date -r "$f" +%Y-%m-%d)" "$(date -r "$f" +%H:%M:%S)" \
                "$(wc -c <"$f" | tr -d ' ')" "$(basename "$f")"
        done
    else
        s3 ls "s3://${ARCHIVE_BUCKET}/${ARCHIVE_PREFIX}"
    fi
}

: >"${WORK}/months"
if [[ -n "$CUTOFF" ]]; then
    # A listing that fails is not an empty archive. (`aws s3 ls` also fails on
    # a prefix with no objects, which is only fine if no month is needed.)
    listing >"${WORK}/listing" 2>"${WORK}/listing.err" || LISTING_FAILED=1
    sort -k1,2 "${WORK}/listing" | awk -v cutoff="$CUTOFF" '
        $4 ~ /^messages_[0-9][0-9][0-9][0-9]-[0-9][0-9]\..*\.csv\.gz$/ {
            m = substr($4, 10, 7)
            if (m >= cutoff) next
            claimed = $4; sub(/^.*\.b/, "", claimed); sub(/\.csv\.gz$/, "", claimed)
            if (claimed == $3) { newest[m] = $3 " " $4 }
            else { print "[restore] skipping " $4 ": " $3 " bytes, its name says " claimed > "/dev/stderr" }
        }
        END { for (m in newest) print m, newest[m] }
    ' | sort >"${WORK}/months"

    found="$(awk '{ print $1 }' "${WORK}/months" | tr '\n' ' ')"
    missing=""
    if [[ "$HAS_EXPECTED" -gt 0 ]]; then
        for m in $EXPECTED_MONTHS; do
            [[ " $found " == *" $m "* ]] || missing="${missing} ${m}"
        done
    elif [[ -z "$found" ]]; then
        missing=" (this nightly does not list its months, and none were found)"
    fi
    if [[ -n "$missing" ]]; then
        [[ -n "${LISTING_FAILED:-}" ]] && cat "${WORK}/listing.err" >&2
        msg="archived months before ${CUTOFF} are missing:${missing}"
        [[ -n "$ALLOW_MISSING" ]] || die "${msg}. Nothing was changed. Check the listing (profile, prefix), or pass --allow-missing to restore without them."
        echo "[restore] WARNING: ${msg}; restoring without them (--allow-missing)" >&2
    fi
fi
MONTHS="$(wc -l <"${WORK}/months" | tr -d ' ')"

# Fetch (S3) and test every month now, so a bad one stops the restore before
# the database is touched rather than half-way through loading it.
while read -r month size name <&3; do
    if [[ "$SRC" == "local" ]]; then
        file="${ARCHIVE_DIR%/}/messages/${name}"
    else
        file="${WORK}/${name}"
        s3 cp "s3://${ARCHIVE_BUCKET}/${ARCHIVE_PREFIX}${name}" "$file" --no-progress >/dev/null
        [[ "$(wc -c <"$file" | tr -d ' ')" == "$size" ]] || die "${name}: the download is not ${size} bytes"
    fi
    gzip -t "$file" || die "${name} is not a complete gzip stream. Nothing was changed."
done 3<"${WORK}/months"

echo
echo "Restoring from: ${LABEL}"
if [[ -z "$CUTOFF" ]]; then
    echo "  a whole-database dump (no archive needed)"
else
    echo "  messages from ${CUTOFF}-01 in the nightly; ${MONTHS} archived month(s) before it:"
    awk '{ print "    " $1 "  " $3 }' "${WORK}/months"
fi
if [[ -z "$YES" ]]; then
    read -rp "This will WIPE the ${DB_NAME} database in ${CONTAINER}. Type 'yes' to continue: " CONFIRM
    [[ "$CONFIRM" == "yes" ]] || { echo "Aborted."; exit 1; }
fi

# ── 3. The nightly ──────────────────────────────────────────────────────────

say "dropping and recreating ${DB_NAME}"
psql_in -d postgres -q -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE)" </dev/null
psql_in -d postgres -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}" </dev/null

say "loading the nightly"
if [[ -n "$CUTOFF" ]]; then
    gunzip -c "$NIGHTLY" | psql_in -d "$DB_NAME" -q -v ON_ERROR_STOP=1 >/dev/null
else
    # A pre-CHATS-41 dump, loaded as it always was.
    gunzip -c "$NIGHTLY" | psql_in -d "$DB_NAME" -q >/dev/null
fi

# ── 4. The archived months ──────────────────────────────────────────────────

# fd 3, so nothing in the loop can swallow the list from stdin.
while read -r month size name <&3; do
    [[ -n "$month" ]] || continue
    rows="${name#*.r}"; rows="${rows%%.*}"
    if [[ "$SRC" == "local" ]]; then
        file="${ARCHIVE_DIR%/}/messages/${name}"
    else
        file="${WORK}/${name}"   # fetched and tested above
    fi
    # The file's own header is the column list, so a month cut before a column
    # existed still loads; HEADER match makes Postgres check it line for line.
    cols="$( (gunzip -c "$file" || true) | head -1 | sed 's/,/, /g')"
    loaded="$(gunzip -c "$file" | psql_in -d "$DB_NAME" -v ON_ERROR_STOP=1 \
        -c "COPY public.messages (${cols}) FROM stdin WITH (FORMAT csv, HEADER match)")"
    [[ "$loaded" == "COPY ${rows}" ]] || die "${name}: expected ${rows} rows, Postgres said '${loaded}'"
    say "${month}: ${rows} messages"
    [[ "$SRC" == "s3" ]] && rm -f "$file"
done 3<"${WORK}/months"

# ── 5. Schema, index, counts ────────────────────────────────────────────────

# A dump taken before a schema change lacks the newer columns; init.sql adds
# them, idempotently, exactly as it does on a live database.
if [[ -f "${REPO_DIR}/init.sql" ]]; then
    say "bringing the schema up to date"
    psql_in -d "$DB_NAME" -q -v ON_ERROR_STOP=1 <"${REPO_DIR}/init.sql" >"${WORK}/init.log" 2>&1 \
        || { cat "${WORK}/init.log" >&2; die "init.sql did not apply cleanly"; }
fi

# The schema dump creates the ivfflat index on an empty table, so its lists
# were fitted to nothing; rebuilding it on the restored rows is what makes
# search work again (minutes, where a re-embed would be hours).
say "rebuilding messages_embedding_idx"
psql_in -d "$DB_NAME" -q -v ON_ERROR_STOP=1 </dev/null \
    -c "DO \$\$ BEGIN
            IF to_regclass('public.messages_embedding_idx') IS NOT NULL THEN
                REINDEX INDEX public.messages_embedding_idx;
            END IF;
        END \$\$" >"${WORK}/reindex.log" 2>&1 \
    || { cat "${WORK}/reindex.log" >&2; die "rebuilding messages_embedding_idx failed (the data is loaded)"; }

say "row counts"
psql_in -d "$DB_NAME" </dev/null -c "
    SELECT (SELECT count(*) FROM conversations) AS conversations,
           (SELECT count(*) FROM messages) AS messages,
           (SELECT count(embedding) FROM messages) AS with_embedding,
           (SELECT min(created_at)::date FROM messages) AS first,
           (SELECT max(created_at)::date FROM messages) AS last,
           (SELECT count(*) FROM facts) AS facts"

# The backups carry the vectors, so this normally has nothing to do. It
# re-embeds what has none (a pre-CHATS-41 dump excluded them all), with the
# installed provider, via the same hook.embed the recorder uses.
MISSING="$(psql_in -d "$DB_NAME" -At </dev/null \
    -c "SELECT count(*) FROM messages WHERE embedding IS NULL")"
if [[ "$MISSING" -gt 0 && -n "$BACKFILL" ]]; then
    say "${MISSING} messages have no vector; backfilling (expect minutes, not seconds)"
    CLAUDE_CHATS_DB_URL="$DB_URL" uv run --project "${REPO_DIR}/hook" backfill-embeddings
elif [[ "$MISSING" -gt 0 ]]; then
    say "${MISSING} messages have no vector; run backfill-embeddings when ready"
fi

say "complete"

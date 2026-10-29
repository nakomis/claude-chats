#!/usr/bin/env bash
#
# Rolling archive of conversation-memory's old months, and the windowed nightly
# dump that replaces the full one (HOME-458).
#
#     convmem-archive.sh archive            # export complete months not yet archived
#     convmem-archive.sh archive 2026-04    # re-archive one month, even if current
#     convmem-archive.sh cutoff             # print the timestamp the nightly must cover from
#     convmem-archive.sh dump               # emit a restorable .sql stream for >= cutoff
#     convmem-archive.sh expect             # rows the dump should contain, for a size floor
#
# ── One implementation, two targets (CHATS-41) ──────────────────────────────
#
# The source of truth is nakomis/claude-chats (scripts/convmem-archive.sh).
# home-infra vendors it, byte for byte, as scripts/luke/convmem-archive.sh, and
# both repos pin the same sha256 (claude-chats' tests/test_convmem_backup.py,
# home-infra's scripts/check-convmem-archive.sh), so the copies cannot drift.
#
#   CONVMEM_TARGET=s3     (default) Luke. Months go to s3://nakomis-convmem-archive/;
#                         every default below is Luke's. Its nightly wrapper
#                         (home-infra luke/deployment/claude-chats/backup.sh)
#                         streams `dump` to S3 and owns alerting.
#   CONVMEM_TARGET=local  the single-machine edition. Months go to
#                         $CONVMEM_ARCHIVE_DIR/messages/; scripts/backup.sh
#                         is the nightly wrapper and writes `dump` to disk.
#
# Everything else (the cutoff, the facts in the key, the verification, the
# floor, the dump format) is the same code for both, which is the point.
# Restore with scripts/restore.sh from either.
#
# ── Why ──────────────────────────────────────────────────────────────────────
#
# The nightly dump was re-uploading ~600 MB to capture ~10 MB of new data, and
# ~85% of those bytes are embedding vectors that barely compress. Measured on
# 15 Oct 2026: 1,848 MB database, 118,233 rows, 44.7% of them older than three
# months, nightly dump 643 MB growing ~10 MB/day.
#
# So old months are written ONCE to a permanent archive bucket and the nightly
# dump covers only what is not yet archived. The database keeps everything —
# this changes the backup, not the data.
#
# Deliberately NOT partitioned. Partitioning `messages` by month was the first
# design and was rejected on measurement: Postgres refuses storage parameters
# on a partitioned parent (`cannot specify storage parameters for a partitioned
# table`), which would have orphaned the autovacuum tuning that exists because
# drift made row estimates ~35x too low and degraded pgvector's plans; pgvector
# warns `ivfflat index created with little data` on every freshly created
# partition, so recall on the current month would silently degrade; and the
# partition key would have had to join every unique constraint, softening the
# `ON CONFLICT (message_uuid)` idempotency guarantee into one contingent on
# `created_at` being derived identically on every redelivery. A date-ranged
# COPY gets the same bytes off the nightly path with no DDL at all.
#
# ── The cutoff is derived from the archive, not the calendar ─────────────────
#
# The nightly dump covers everything from the start of the earliest month that
# has rows and is NOT verified present in the archive bucket. That one rule is
# what makes a one-month live window safe: if the archiver fails, months stay
# unarchived, the cutoff does not advance, and the nightly dump quietly gets
# BIGGER. The failure mode is "backups take longer", never "a month is in
# neither place". A calendar-derived cutoff would have the opposite failure
# mode, and it would be silent.
#
# ── Verification without GetObject ──────────────────────────────────────────
#
# The backup identity has PutObject and ListBucket and deliberately no
# GetObject (HOME-335), so a compromise of Luke cannot read years of
# conversations. It also has no DeleteObject on the archive bucket, so a bad
# object cannot be cleaned up in place.
#
# Both constraints are met by putting the facts in the KEY:
#
#     messages/messages_2026-04.r19426.c15112988.e9f3a1c02.a5d0e7b31.b102315429.csv.gz
#                               ^rows  ^content  ^embedders ^authors  ^object bytes
#
# A listing alone then proves the object is intact (does the size S3 reports
# equal the `b` in its own name?) and whether it is still current (do `r`, `c`
# and `e` still match the database?). A month whose facts have moved stops
# matching its key, the cutoff falls back to cover it, and the next archive run
# re-cuts it.
#
# Each fact catches a different kind of rewrite:
#
#   r  rows inserted or deleted
#   c  sum(length(content)) -- a backfill that rewrites message text
#   e  a short hash of the month's embedder distribution -- a RE-EMBED, which
#      changes `embedding` and `embedder` but neither of the other two. This
#      fact was missing until HOME-471: the first version of this comment
#      claimed `c` caught the CHATS-5 embedder backfill, which it cannot, and
#      HOME-469's re-embed of the Titan-vector months would have left the
#      archive holding the old vectors for ever.
#   a  a short hash of the month's (author, tool_name, model) distribution --
#      a LABEL backfill. HOME-311 set author on 79,901 rows from Mar to Aug
#      2026 and moved none of r, c or e, so the archive kept 'unknown' for
#      every one of them until those months were re-cut by hand.
#
# `e` and `a` hash labels per row count, not the vectors. Hashing
# `embedding::text` would render ~240 MB of text per month, for every archived
# month, every night. The trade: a re-embed that keeps the same label is not
# detected -- force a re-cut with `archive YYYY-MM` for that.
#
# The byte count has to be known BEFORE the key can be named, so a month is
# staged to disk, measured, and then uploaded ONCE. The first draft of this
# streamed instead and uploaded twice — a `.partial` key to count the bytes,
# then the real one — which left an orphan the size of the archive itself in
# every month, for ever, because nothing here has DeleteObject. That silently
# doubled the storage. Staging is why the nightly path streams and this one
# does not: the nightly dump is the whole live window and would be hundreds of
# megabytes of Luke's RAM on a tmpfs, whereas one month is ~103 MB written to
# a 152 GB rw volume, once a month, and removed immediately.
#
# ── Format ──────────────────────────────────────────────────────────────────
#
# CSV with a header row, restored with `HEADER match`, so the file is
# self-describing and Postgres itself refuses a restore whose columns do not
# line up. The column list is derived from the catalogue at both ends rather
# than hard-coded, and excludes generated columns (`content_tsv`), which COPY
# can emit but cannot accept.
#
set -euo pipefail
set -o pipefail

# Key prefix for everything this host writes to S3: empty on the house boxes;
# `dr/` on an EC2 stand-in, where the playbook writes /etc/default/luke-backup
# (HOME-478), so rehearsals never push real dumps out of the retention window.
# shellcheck disable=SC1091
[ -r /etc/default/luke-backup ] && . /etc/default/luke-backup
BACKUP_PREFIX="${BACKUP_PREFIX:-}"

TARGET="${CONVMEM_TARGET:-s3}"
ARCHIVE_BUCKET="${CONVMEM_ARCHIVE_BUCKET:-nakomis-convmem-archive}"
ARCHIVE_PREFIX="${BACKUP_PREFIX}messages/"
REGION="${AWS_REGION_BACKUP:-eu-west-2}"
AWS_PROFILE_BACKUP="${AWS_PROFILE_BACKUP:-taiga-backup}"
ARCHIVE_DIR="${CONVMEM_ARCHIVE_DIR:-${HOME}/.claude-chats/backups/archive}"
CONTAINER="${CONVMEM_CONTAINER:-claude-chats-db}"
DOCKER="${CONVMEM_DOCKER:-docker}"
DB_NAME="${CONVMEM_DB_NAME:-claude_chats}"
DB_USER="${CONVMEM_DB_USER:-claude}"

case "$TARGET" in
    s3)    ARCHIVE_LOCATION="s3://${ARCHIVE_BUCKET}/${ARCHIVE_PREFIX}" ;;
    local) ARCHIVE_LOCATION="${ARCHIVE_DIR%/}/${ARCHIVE_PREFIX}" ;;
    *)     echo "CONVMEM_TARGET must be 's3' or 'local', got '${TARGET}'" >&2; exit 2 ;;
esac

# How many months stay in the nightly dump, counting the current one. 1 means
# a month becomes eligible for archiving as soon as it is complete, so the
# nightly covers only the current month — 0 bytes on the 1st, ~170 MB by the
# 31st at October 2026's rate. A month is never archived before it is complete,
# so this cannot be set low enough to archive a partial month.
LIVE_MONTHS="${CONVMEM_LIVE_MONTHS:-1}"

# Below this, a month's export is treated as truncated rather than uploaded.
# Set per-row rather than absolute: months differ by 4x (10,606 rows in June,
# 21,914 in May), so one absolute floor is either useless or a false alarm.
# Observed ~5.3 KB/row compressed; 1 KB/row is a tripwire for "the export
# stopped early", not a size assertion.
MIN_BYTES_PER_ROW="${CONVMEM_MIN_BYTES_PER_ROW:-1024}"

# Where a month is staged while it is measured. Under the playbook-provisioned,
# nakomis-owned /mnt/data/luke-backups rather than /tmp, which is a tmpfs on
# Luke — staging there would spend ~103 MB of RAM to save a disk write.
#
# A local archive stages beside itself instead, so the finished file is moved
# into place by a rename on the same filesystem, never half-copied.
if [ "$TARGET" = "local" ]; then
    STAGING_DIR="${CONVMEM_STAGING_DIR:-${ARCHIVE_DIR%/}/.staging}"
else
    STAGING_DIR="${CONVMEM_STAGING_DIR:-/mnt/data/luke-backups/staging}"
fi

export PATH="$HOME/bin:$PATH"

# Set while a month is staged on disk, cleared once it has been uploaded. A
# single EXIT trap rather than a per-function RETURN trap: a RETURN trap set
# inside a function also fires when its CALLER returns, by which point the
# local it referred to is out of scope and `set -u` turns a successful archive
# into a non-zero exit — which the nightly would read as a failed backup.
#
# It also ends dump's snapshot session if the dump failed part-way: closing
# the FIFO ends the session (and its transaction) on its own, and the session
# is then waited for so it is not left behind.
STAGED=""
cleanup() {
    [ -n "${STAGED:-}" ] && rm -f "$STAGED"
    if [ -n "${SNAP_PID:-}" ]; then
        exec 4>&-
        wait "$SNAP_PID" 2>/dev/null
    fi
    [ -n "${SNAP_DIR:-}" ] && rm -rf "$SNAP_DIR"
    return 0
}
trap cleanup EXIT

log()  { echo "$(date -Iseconds) $*" >&2; }
err()  { echo "$(date -Iseconds) $*" >&2; }
fail() { err "$*"; exit 1; }

# No `-i`, and stdin closed. Every call here passes its SQL with -c, so the
# query needs no stdin at all — and `docker exec -i` inside a `while read`
# loop CONSUMES the loop's input, so the loop runs exactly once. That cost an
# afternoon: `cutoff` reported the live window as fully archived because the
# first month's fact query had eaten every month after it.
psql_q() {
    "$DOCKER" exec "$CONTAINER" psql -h /var/run/postgresql -U "$DB_USER" -d "$DB_NAME" \
        -v ON_ERROR_STOP=1 -At "$@" </dev/null
}

require_container() {
    "$DOCKER" ps --filter "name=^/${CONTAINER}$" --filter "status=running" \
        --format '{{.Names}}' | grep -qx "$CONTAINER" \
        || fail "container '${CONTAINER}' is not running"
}

# Non-generated columns, in ordinal order. Derived rather than hard-coded so a
# new column is picked up automatically; the CSV header plus `HEADER match`
# makes a mismatch at restore time an error rather than silent corruption.
columns() {
    psql_q -c "SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
               FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'messages'
                 AND is_generated = 'NEVER'"
}

# Every month that has rows, as YYYY-MM, oldest first.
months_with_data() {
    psql_q -c "SELECT to_char(date_trunc('month', created_at), 'YYYY-MM')
               FROM messages GROUP BY 1 ORDER BY 1"
}

# The first month that must stay live. Months strictly before this are eligible
# for archiving once complete.
live_from_month() {
    psql_q -c "SELECT to_char(date_trunc('month', now()) - make_interval(months => ${LIVE_MONTHS} - 1), 'YYYY-MM')"
}

# rows|content_bytes|embedder_hash|author_hash for one month -- the fingerprint
# encoded in the key. See the header for what each fact catches. The two hashes
# read only the heap (label columns), never the TOASTed vectors, so they stay
# cheap when run for every archived month every night.
month_facts() {
    local m="$1"
    local range="created_at >= date '${m}-01'
                 AND created_at < (date '${m}-01' + interval '1 month')"
    psql_q -c "SELECT count(*) || '|' || coalesce(sum(length(content)), 0) || '|' ||
                      (SELECT left(md5(coalesce(string_agg(e || '=' || n, ',' ORDER BY e), '')), 8)
                       FROM (SELECT coalesce(embedder, '<null>') AS e, count(*) AS n
                             FROM messages WHERE ${range} GROUP BY 1) d) || '|' ||
                      (SELECT left(md5(coalesce(string_agg(l || '=' || n, ',' ORDER BY l), '')), 8)
                       FROM (SELECT author || '/' || coalesce(tool_name, '') || '/' ||
                                    coalesce(model, '') AS l, count(*) AS n
                             FROM messages WHERE ${range} GROUP BY 1) d)
               FROM messages WHERE ${range}"
}

# The key stem for a month: everything between the month and `.b`. One string
# rather than a growing list of positional arguments, so adding a fact touches
# month_facts and this, not every caller.
facts_stem() {
    local rows content emb auth
    IFS='|' read -r rows content emb auth <<<"$1"
    printf 'r%s.c%s.e%s.a%s' "$rows" "$content" "$emb" "$auth"
}

facts_rows() {
    printf '%s' "${1%%|*}"
}

# Keys currently in the archive, as "basename<TAB>size". A local archive is
# listed the same way, sizes from the file system, so everything downstream of
# this function is one code path for both targets.
archive_listing() {
    if [ "$TARGET" = "local" ]; then
        local f
        for f in "${ARCHIVE_LOCATION}"messages_*.csv.gz; do
            [ -f "$f" ] || continue
            printf '%s\t%s\n' "$(basename "$f")" "$(wc -c <"$f" | tr -d ' ')"
        done
        return 0
    fi
    AWS_PROFILE="$AWS_PROFILE_BACKUP" aws s3 ls "s3://${ARCHIVE_BUCKET}/${ARCHIVE_PREFIX}" \
        --region "$REGION" 2>/dev/null | awk '{ print $4 "\t" $3 }'
}

# Is month $1 archived, intact and current? Compares the listing against the
# facts in the key: object size must equal `b`, and the stem (r/c/e) must
# still match the database.
#
# Superseded objects are never deleted from S3 (no DeleteObject), so a month
# may have several. That is fine here: only an object whose stem matches the
# live facts counts. It matters at RESTORE time instead, where there is no
# database to compare against -- see restore.sh. A local archive can delete,
# so it keeps one file per month.
month_is_archived() {
    local m="$1" stem="$2" listing="$3"
    local want="messages_${m}.${stem}.b"
    printf '%s\n' "$listing" | awk -F'\t' -v want="$want" '
        index($1, want) == 1 {
            # bytes claimed by the key vs bytes S3 reports
            claimed = $1; sub(/^.*\.b/, "", claimed); sub(/\.csv\.gz$/, "", claimed)
            if (claimed == $2) { found = 1 }
        }
        END { exit(found ? 0 : 1) }
    '
}

# ── archive ─────────────────────────────────────────────────────────────────

do_archive() {
    require_container
    local only="${1:-}"

    local cols live_from listing months m rows stem facts
    cols="$(columns)"
    [ -n "$cols" ] || fail "could not determine the column list"
    live_from="$(live_from_month)"
    listing="$(archive_listing || true)"
    months="$(months_with_data)"

    # A named month is re-archived unconditionally — that is the recovery path
    # for a month whose r/c facts have gone stale, and the only way to force a
    # re-cut. It still refuses to touch the live window, because archiving an
    # incomplete month would bake a partial month into a key that claims to be
    # the whole of it.
    if [ -n "$only" ]; then
        [[ "$only" =~ ^[0-9]{4}-[0-9]{2}$ ]] || fail "month must be YYYY-MM, got '${only}'"
        [[ "$only" < "$live_from" ]] \
            || fail "${only} is inside the live window (from ${live_from}) — it is not complete yet"
        grep -qx "$only" <<<"$months" || fail "${only} has no rows in messages"
        facts="$(month_facts "$only")"
        archive_month "$only" "$(facts_rows "$facts")" "$(facts_stem "$facts")" "$cols"
        return 0
    fi

    log "archiving complete months before ${live_from} (live window: ${LIVE_MONTHS} month(s))"

    local archived=0 skipped=0
    while IFS= read -r m <&3; do
        [ -n "$m" ] || continue
        # Never archive the live window, and never a partial month: both are
        # the same test, because live_from is always a month start.
        if [[ "$m" > "$live_from" || "$m" == "$live_from" ]]; then
            continue
        fi

        facts="$(month_facts "$m")"
        rows="$(facts_rows "$facts")"
        stem="$(facts_stem "$facts")"
        [ "$rows" -gt 0 ] || continue

        if month_is_archived "$m" "$stem" "$listing"; then
            skipped=$((skipped + 1))
            continue
        fi

        archive_month "$m" "$rows" "$stem" "$cols"
        archived=$((archived + 1))
    done 3<<<"$months"

    log "archive run complete: ${archived} archived, ${skipped} already current"
}

archive_month() {
    local m="$1" rows="$2" stem="$3" cols="$4"
    local staged key sent sha floor listing

    floor=$((rows * MIN_BYTES_PER_ROW))
    mkdir -p "$STAGING_DIR"
    staged="${STAGING_DIR}/messages_${m}.csv.gz"
    # Removed on every exit path by the EXIT trap, including a failure part-way
    # through the export: a half-written stage left behind would be uploaded
    # verbatim by the next run if the floor check happened to pass.
    STAGED="$staged"

    log "[${m}] exporting ${rows} rows (facts ${stem}) to ${staged}"

    # ORDER BY so a re-archive of an unchanged month is byte-identical, which
    # makes the r/c/b facts in the key reproducible rather than incidental.
    "$DOCKER" exec "$CONTAINER" psql -h /var/run/postgresql -U "$DB_USER" -d "$DB_NAME" \
            -v ON_ERROR_STOP=1 \
            -c "COPY (SELECT ${cols} FROM messages
                      WHERE created_at >= date '${m}-01'
                        AND created_at <  (date '${m}-01' + interval '1 month')
                      ORDER BY created_at, id)
                TO STDOUT WITH (FORMAT csv, HEADER true)" \
        | gzip > "$staged"

    sent="$(wc -c <"$staged" | tr -d ' ')"
    [ "$sent" -ge "$floor" ] \
        || fail "[${m}] only ${sent} bytes for ${rows} rows (floor ${floor}) — treating as truncated"

    # A truncated gzip is the failure mode a byte floor cannot see: a stream cut
    # off at 90% still clears any sane floor.
    gzip -t "$staged" || fail "[${m}] staged export is not a complete gzip stream"

    key="${ARCHIVE_PREFIX}messages_${m}.${stem}.b${sent}.csv.gz"

    if [ "$TARGET" = "local" ]; then
        store_month_locally "$m" "$stem" "$staged" "$key" "$sent"
        return 0
    fi

    # shasum where there is no sha256sum (macOS), so the S3 target can be
    # exercised off Linux.
    if command -v sha256sum >/dev/null; then
        sha="$(sha256sum "$staged" | awk '{print $1}')"
    else
        sha="$(shasum -a 256 "$staged" | awk '{print $1}')"
    fi
    log "[${m}] ${sent} bytes, sha256 ${sha} — uploading once, as GLACIER_IR"
    # sha256 as object metadata, not in the key: it is for whoever RESTORES the
    # object, and a restore runs as nakom.is-admin, which can read metadata.
    # This identity cannot (no HeadObject), which is exactly why the facts it
    # needs are in the key instead.
    AWS_PROFILE="$AWS_PROFILE_BACKUP" aws s3 cp "$staged" "s3://${ARCHIVE_BUCKET}/${key}" \
        --storage-class GLACIER_IR --region "$REGION" \
        --metadata "sha256=${sha},rows=${rows},month=${m}" >&2

    # Confirm from the listing — the only read this identity has — that the
    # object is there at the size its own name claims.
    listing="$(archive_listing || true)"
    month_is_archived "$m" "$stem" "$listing" \
        || fail "[${m}] uploaded ${key} but the listing does not confirm it at ${sent} bytes"
    rm -f "$staged"
    STAGED=""
    log "[${m}] archived and verified: s3://${ARCHIVE_BUCKET}/${key}"
}

# The local target's half of archive_month: a rename into place (atomic, since
# the stage is on the same file system), the same listing check the S3 path
# makes, and only THEN the month's superseded files are removed. A local archive
# has the delete that the S3 identity is denied, so it keeps one file per month
# and a restore never has to choose between copies.
store_month_locally() {
    local m="$1" stem="$2" staged="$3" key="$4" sent="$5"
    local dest listing old
    dest="${ARCHIVE_DIR%/}/${key}"
    mkdir -p "$(dirname "$dest")"
    log "[${m}] ${sent} bytes — moving into ${dest}"
    mv -f "$staged" "$dest"
    STAGED=""

    listing="$(archive_listing || true)"
    month_is_archived "$m" "$stem" "$listing" \
        || fail "[${m}] wrote ${dest} but the listing does not confirm it at ${sent} bytes"

    for old in "${ARCHIVE_LOCATION}messages_${m}."*.csv.gz; do
        [ -f "$old" ] && [ "$old" != "$dest" ] || continue
        rm -f "$old"
        log "[${m}] removed superseded $(basename "$old")"
    done
    log "[${m}] archived and verified: ${dest}"
}

# ── cutoff ──────────────────────────────────────────────────────────────────

do_cutoff() {
    require_container
    local live_from listing months m facts rows stem
    live_from="$(live_from_month)"
    listing="$(archive_listing || true)"
    months="$(months_with_data)"

    while IFS= read -r m <&3; do
        [ -n "$m" ] || continue
        if [[ "$m" > "$live_from" || "$m" == "$live_from" ]]; then
            break
        fi
        facts="$(month_facts "$m")"
        rows="$(facts_rows "$facts")"
        stem="$(facts_stem "$facts")"
        [ "$rows" -gt 0 ] || continue
        if ! month_is_archived "$m" "$stem" "$listing"; then
            # Earliest month not provably in the archive: the nightly dump has
            # to carry it, however old it is.
            printf '%s-01\n' "$m"
            return 0
        fi
    done 3<<<"$months"

    printf '%s-01\n' "$live_from"
}

# ── expect ──────────────────────────────────────────────────────────────────
#
# How many message rows the nightly dump should contain. Callers turn this into
# a size floor.
#
# A FIXED byte floor cannot work with a one-month window: the dump is ~2 MB on
# the 1st (the month just archived, the new one barely begun) and ~200 MB by the
# 31st. run-backups.sh's existing 100,000,000-byte floor for this database would
# have failed the backup gate on the 1st of every month — and, being a gate,
# would have blocked every Luke playbook run until someone worked out why.
do_expect() {
    require_container
    local cutoff
    cutoff="$(do_cutoff)"
    psql_q -c "SELECT count(*) FROM messages WHERE created_at >= date '${cutoff}'"
}

# ── dump ────────────────────────────────────────────────────────────────────

do_dump() {
    require_container
    local cutoff cols
    cutoff="$(do_cutoff)"
    cols="$(columns)"
    [ -n "$cols" ] || fail "could not determine the column list"
    log "dumping schema + every other table + messages from ${cutoff}"

    # One object, not three. The retention Lambda keeps the 10 newest keys per
    # first path segment, so three objects a night under conversation-memory/
    # would leave three nights of history instead of ten. Emitting one
    # restorable .sql stream keeps the existing key pattern, the existing
    # retention behaviour and the existing restore muscle memory.
    #
    # Order is a dependency order: schema first, then every other table's data
    # (conversations, and Claude Facts: facts, fact_events, facts_progress),
    # then messages (which references conversations).
    #
    # The `messages from` line is read back by restore.sh: archived months
    # before it come from the archive, everything from it on from this dump.
    echo "-- conversation-memory windowed backup (HOME-458)"
    echo "-- messages from ${cutoff}; everything older is in ${ARCHIVE_LOCATION}"
    echo "-- restore: claude-chats scripts/restore.sh, or home-infra docs/runbooks/convmem-restore.md"

    "$DOCKER" exec "$CONTAINER" pg_dump -h /var/run/postgresql -U "$DB_USER" -s "$DB_NAME"

    # The data has to come from ONE snapshot. Until CHATS-41 conversations and
    # messages were read by two separate sessions, so a conversation that began
    # between them was missing from the dump while its first messages were in
    # it, and the restore failed on the foreign key. The session below exports
    # a snapshot and holds it open; pg_dump and the COPY both read through it.
    open_snapshot

    # Every table except messages, whole, every night: conversations (which
    # messages references), and Claude Facts' facts, fact_events and
    # facts_progress. Facts are not windowed by month: they are small, they
    # change in place for ever (last_confirmed, valid_to, superseded_by), and a
    # fact can come from no conversation at all, so a month is not a meaningful
    # unit for them. Excluding by name rather than listing what to include means
    # a table added later is backed up without anyone remembering to add it
    # here. The two bake-off tables (scripts/bakeoff-schema.sql) are
    # regenerable scratch and normally absent; excluding absent tables is a no-op.
    #
    # facts.superseded_by refers to facts, which makes pg_dump warn, on every
    # data-only dump, that the table has circular foreign keys and the dump
    # "might not" restore. It does: a self-reference within one COPY is checked
    # at the end of the statement, and restore.sh's round-trip test proves it.
    # That one warning is dropped so the journal is not cried wolf at nightly;
    # any other table's circularity still gets through.
    "$DOCKER" exec "$CONTAINER" pg_dump -h /var/run/postgresql -U "$DB_USER" -a \
        --snapshot="$SNAPSHOT" \
        --exclude-table-data=messages \
        --exclude-table-data=message_embeddings \
        --exclude-table-data=embedding_perf \
        "$DB_NAME" 4>&- 2> >(exec 4>&-; drop_facts_circularity_warning >&2)

    printf 'COPY public.messages (%s) FROM stdin WITH (FORMAT csv, HEADER match);\n' "$cols"
    # -q keeps BEGIN/SET/COMMIT's command tags out of the stream.
    "$DOCKER" exec "$CONTAINER" psql -h /var/run/postgresql -U "$DB_USER" -d "$DB_NAME" \
        -q -v ON_ERROR_STOP=1 \
        -c "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" \
        -c "SET TRANSACTION SNAPSHOT '${SNAPSHOT}'" \
        -c "COPY (SELECT ${cols} FROM messages WHERE created_at >= date '${cutoff}'
                  ORDER BY created_at, id)
            TO STDOUT WITH (FORMAT csv, HEADER true)" \
        -c "COMMIT" </dev/null 4>&-
    printf '\\.\n'

    close_snapshot
}

# Pass stderr through, minus pg_dump's four-line circular-foreign-key warning
# when the only table it names is facts.
drop_facts_circularity_warning() {
    awk '
        /circular foreign-key constraints on this table:$/ { held = $0; next }
        held != "" {
            if ($0 ~ /detail: facts$/) { skip = 2 } else { print held; print }
            held = ""; next
        }
        skip > 0 && /hint:/ { skip--; next }
        { skip = 0; print }
    '
}

# A session that exports a snapshot and keeps its transaction open until
# close_snapshot, so other sessions can SET TRANSACTION SNAPSHOT to it. It
# reads its SQL from a FIFO; the COMMIT that ends it is written once the dump
# is done, and an early exit closes the FIFO, which ends it all the same. The
# commands in between are started with fd 4 closed, so none of them can keep
# the FIFO open behind the session's back.
SNAP_DIR=""
SNAP_PID=""
SNAPSHOT=""
open_snapshot() {
    local _
    SNAP_DIR="$(mktemp -d)"
    mkfifo "${SNAP_DIR}/in"
    "$DOCKER" exec -i "$CONTAINER" psql -h /var/run/postgresql -U "$DB_USER" -d "$DB_NAME" \
        -q -At -v ON_ERROR_STOP=1 <"${SNAP_DIR}/in" >"${SNAP_DIR}/out" &
    SNAP_PID=$!
    exec 4>"${SNAP_DIR}/in"
    printf 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n' >&4
    for _ in $(seq 1 300); do
        SNAPSHOT="$(head -1 "${SNAP_DIR}/out" 2>/dev/null || true)"
        [[ "$SNAPSHOT" =~ ^[0-9A-Fa-f-]+$ ]] && return 0
        kill -0 "$SNAP_PID" 2>/dev/null || break
        sleep 0.1
    done
    fail "could not export a snapshot for the dump"
}

close_snapshot() {
    printf 'COMMIT;\n' >&4
    exec 4>&-
    wait "$SNAP_PID" || fail "the snapshot session did not end cleanly"
    SNAP_PID=""
    rm -rf "$SNAP_DIR"
    SNAP_DIR=""
}

case "${1:-}" in
    archive) do_archive "${2:-}" ;;
    cutoff)  do_cutoff ;;
    dump)    do_dump ;;
    expect)  do_expect ;;
    *)       fail "usage: $(basename "$0") {archive [YYYY-MM]|cutoff|dump|expect}" ;;
esac

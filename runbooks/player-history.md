# Player-history snapshot and public service

## Security boundary

- The builder supports one bounded live source and explicit offline backup
  copies. `--live-source` always resolves `data/lcu/games.db` from Git's common
  primary checkout; it accepts no path. A linked worktree may identify that
  checkout only through its strict, non-reparse `.git` metadata. `--source`
  remains offline-only and rejects any live-layout DB, WAL/SHM companion,
  symlink, or copy with adjacent `-wal`/`-shm` files.
- Keep snapshot, RSA public PEM, quarantine DB, limiter DB, live `games.db`, and
  public site DB on distinct resolved paths. Do not hardlink or symlink them.
- The runtime needs three distinct 32-byte secrets. Provision them outside the
  repo and inject lowercase hex through the service manager. The application
  never generates, rotates, prints, or writes credentials.
- Only the RSA public key is present on the public host. Decryption and any
  candidate review happen in a separate offline trust domain. Quarantine rows
  are untrusted hints, never crawl authorization.

## Build from the live collector database

Run the live mode while the collector remains active:

```powershell
aram-player-history-build --live-source --destination <new-path> `
  --dataset-id <id> --patch <patch> --generated-date <YYYY-MM-DD>
```

The builder opens exactly the Git-common primary checkout's
`data/lcu/games.db` with SQLite `mode=ro` (never `immutable`), enables
`query_only`, disables trusted schema execution, keeps temporary storage in
memory, and starts a read transaction before validating the exact current
`games` schema. It selects only the player-history projection for queue 2400
and the requested patches; `seed_family` and all unrelated tables are not
selected.

The live transaction is bounded to 20 minutes and fails if WAL growth exceeds
512 MiB from its starting size. Database, WAL, and SHM identities are pinned;
replacement or any symlink/reparse component fails closed. The transaction is
rolled back and the connection is closed before destination publication. On
deadline, WAL, identity, schema, or read failure, no destination is published.
The builder never writes, indexes, checkpoints, moves, or copies the live
database or its sidecars.

## Make a safe offline source

1. Stop writes long enough to create a consistent SQLite backup with an
   operator-approved SQLite backup tool. Copying only `games.db` while WAL mode
   is active is not a valid backup.
2. Put the completed backup outside the live `data/lcu/` layout.
3. Confirm the backup has no adjacent `-wal` or `-shm`, then mark/handle it as
   read-only for the build window.
4. Alternatively, run `aram-player-history-build --source <backup> --destination <new-path>
   --dataset-id <id> --patch <patch> --generated-date <YYYY-MM-DD>` with lookup
   and event secrets injected. Destination publication is audited, atomic, and
   no-clobber.
5. Remove the private backup through the operator's approved retention process;
   the builder does not delete it.

The builder records source file device/inode/size/mtime before reading and
after full iterator exhaustion. Any change fails the build. A new destination
must be used for every snapshot; do not edit a published snapshot in place.

## Start the isolated service

Set every required `ARAM_PLAYER_HISTORY_*` variable documented in
`docs/backend-frontend.md`, then run:

```powershell
aram-player-history-public
```

The uvicorn process disables access logs. Keep reverse-proxy request/query/body
logging disabled for this route too. If a proxy is used, list only its exact
immediate canonical IP; otherwise leave trusted peers empty. Requests with no
`Origin` are allowed. Requests with an `Origin` require an exact allowlist
match; no permissive CORS middleware is installed.

## Rotation and recovery

- Snapshot rotation requires a process restart so startup performs exactly one
  full audit and pins the new file identity. Never replace the file underneath
  a running process; changed identity yields fixed HTTP 503.
- The limiter persists HMAC keys only (never raw IPs) across restart and across
  instances sharing its dedicated DB. Lock, corruption, or active-key capacity
  failure denies with HTTP 429 and `Retry-After`.
- Quarantine admission uses a 50 ms maximum SQLite busy timeout, a 64 MiB
  preflight bound, a 10,000-row cap, 30-day pending TTL, and 90-day terminal
  TTL. Admission failure is intentionally silent and does not affect a valid
  lookup response.
- To rotate candidate RSA keys, deploy the new public PEM and restart. Retain
  old private keys only in the offline decryptor's explicit allowlist for the
  approved retirement period.

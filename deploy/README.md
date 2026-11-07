# Pinned runtime deployment

Development happens in this repository. Production instances run from a
separate, clean checkout of one verified commit (the *runtime checkout*);
per-agent config, workspace, memory, sessions, channels and ports stay in the
data root (`.local/`, never tracked).

1. Commit and verify in the development repository.
2. Create the runtime checkout: `git worktree add --detach <runtime_root> <commit>`
   (or `git clone` + `git checkout <commit>`). Do not edit it afterwards.
3. Write one profile per instance in `<data_root>/.local/runtimes/<instance>.json`
   from `deploy/runtime-profile.example.json`. Profiles hold paths and
   invariants only — no tokens; secrets stay in the referenced env files.
4. `scripts/runtime.sh <profile> verify` checks HEAD, a clean tree, module
   import paths and bytes, templates, and the config invariants without
   starting anything.
5. `scripts/runtime.sh <profile> up|restart|status|stop` controls only that
   instance. `start-nanobot-easy.sh` / `scripts/restart-nanobot-easy.sh`
   delegate automatically when a profile matches `NANOBOT_CONFIG` and
   `NANOBOT_WORKSPACE` (or `NANOBOT_RUNTIME_PROFILE` is set).
6. The gateway process writes the profile's `record` (deployment manifest):
   instance, PID, process start time, commit, module paths/hashes, template
   hashes, config path/hash, workspace, port, deployment time. `status`
   compares it with the live process and reports `stale_no_process`,
   `stale_pid_reused` or `commit_differs_from_profile` instead of trusting it.

Rollback: point the profile back at the previous runtime checkout/commit (kept
in place) and `restart` that single instance.

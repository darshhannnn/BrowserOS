# @browseros/build-tools

Builds agent image tarballs, publishes release artifacts to R2, and hydrates the local dev cache for agent tarballs.

The BrowserOS VM is defined by a committed Lima template at `template/browseros-vm.yaml`. There is no custom disk build step; `limactl` consumes the template directly at runtime.

## Setup

```bash
cp packages/build-tools/.env.sample packages/build-tools/.env
bun install
```

## Dev loop against the Lima template

Requires `limactl` on PATH. It is bundled with the server; for bare-worktree use, install Lima with Homebrew.

```bash
brew install lima
```

```bash
limactl start \
  --name browseros-vm-dev \
  packages/browseros-agent/packages/build-tools/template/browseros-vm.yaml

limactl shell browseros-vm-dev podman info

SOCK="$(limactl list browseros-vm-dev --format '{{.Dir}}')/sock/podman.sock"
curl --unix-socket "$SOCK" http://d/v5.0.0/libpod/_ping

bun run --filter @browseros/build-tools build:tarball -- --agent openclaw --arch arm64
limactl shell browseros-vm-dev podman load -i "$(ls dist/images/openclaw-*-arm64.tar.gz | head -1)"

limactl delete --force browseros-vm-dev
```

## Build an agent tarball

Requires `podman`.

```bash
bun run --filter @browseros/build-tools build:tarball -- --agent openclaw --arch arm64
```

## Smoke test an agent tarball

```bash
bun run --filter @browseros/build-tools smoke:tarball -- --agent openclaw --arch arm64 --tarball ./dist/images/openclaw-2026.4.12-arm64.tar.gz
```

## Emit a manifest

```bash
bun run --filter @browseros/build-tools emit-manifest -- --dist-dir packages/build-tools/dist
```

Publish workflows can update one agent slice at a time. Sliced publishing requires an existing R2 `vm/manifest.json` baseline; bootstrap first releases with `--slice full`.

```bash
bun run --filter @browseros/build-tools emit-manifest -- --slice agents:openclaw --merge-from https://cdn.browseros.com/vm/manifest.json
```

## Sync the dev cache

```bash
NODE_ENV=development bun run --filter @browseros/build-tools cache:sync
```

Development cache files land under `~/.browseros-dev/cache/vm/images/`. Production-mode cache files land under `~/.browseros/cache/vm/images/`.

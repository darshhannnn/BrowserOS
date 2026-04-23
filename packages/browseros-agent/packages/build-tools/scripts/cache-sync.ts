#!/usr/bin/env bun
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { homedir, arch as hostArch } from 'node:os'
import path from 'node:path'
import { parseArgs } from 'node:util'
import { PATHS } from '@browseros/shared/constants/paths'
import { ARCHES, type Arch } from './common/arch'
import { fetchWithTimeout } from './common/fetch'
import type { AgentManifest, Artifact } from './common/manifest'
import { verifySha256 } from './common/sha256'

type ChunkSink = ReturnType<ReturnType<typeof Bun.file>['writer']>

export interface PlanItem {
  key: string
  destPath: string
  sha256: string
}

export function planSync(opts: {
  local: AgentManifest | null
  remote: AgentManifest
  cacheRoot: string
  arches: Arch[]
}): PlanItem[] {
  const out: PlanItem[] = []
  for (const arch of opts.arches) {
    for (const [name, agent] of Object.entries(opts.remote.agents)) {
      maybeAdd(
        out,
        agent.tarballs[arch],
        opts.local?.agents[name]?.tarballs[arch],
        opts.cacheRoot,
      )
    }
  }
  return out
}

export function selectSyncArches(
  allArches: boolean,
  rawHostArch = hostArch(),
): Arch[] {
  if (allArches) return [...ARCHES]
  if (rawHostArch === 'arm64') return ['arm64']
  if (rawHostArch === 'x64' || rawHostArch === 'ia32') return ['x64']
  throw new Error(`unsupported host arch: ${rawHostArch}`)
}

if (import.meta.main) {
  const { values } = parseArgs({
    args: Bun.argv.slice(2),
    options: {
      'manifest-url': { type: 'string' },
      'all-arches': { type: 'boolean' },
      'cache-dir': { type: 'string' },
    },
  })

  const cdnBase =
    process.env.R2_PUBLIC_BASE_URL?.trim() ?? 'https://cdn.browseros.com'
  const manifestUrl = values['manifest-url'] ?? `${cdnBase}/vm/manifest.json`
  const cacheRoot = values['cache-dir'] ?? getCacheDir()
  const arches = selectSyncArches(values['all-arches'] ?? false)

  const response = await fetchWithTimeout(manifestUrl)
  if (!response.ok) {
    throw new Error(
      `manifest fetch failed: ${manifestUrl} (${response.status})`,
    )
  }
  const remote = (await response.json()) as AgentManifest

  const localManifestPath = path.join(cacheRoot, 'vm', 'manifest.json')
  const local = await readLocalManifest(localManifestPath)
  const plan = planSync({ local, remote, cacheRoot, arches })

  if (plan.length === 0) {
    console.log('agent cache up to date')
    process.exit(0)
  }

  console.log(`syncing ${plan.length} agent artifact(s)`)
  for (const item of plan) {
    await mkdir(path.dirname(item.destPath), { recursive: true })
    const partial = `${item.destPath}.partial`
    await downloadToFile(`${cdnBase}/${item.key}`, partial)
    await verifySha256(partial, item.sha256)
    await rename(partial, item.destPath)
    console.log(`synced ${item.key}`)
  }

  await mkdir(path.dirname(localManifestPath), { recursive: true })
  await writeFile(localManifestPath, `${JSON.stringify(remote, null, 2)}\n`)
  console.log(`manifest written to ${localManifestPath}`)
}

function maybeAdd(
  out: PlanItem[],
  remote: Artifact,
  local: Artifact | undefined,
  cacheRoot: string,
): void {
  if (local?.sha256 === remote.sha256) return
  out.push({
    key: remote.key,
    destPath: path.join(cacheRoot, remote.key),
    sha256: remote.sha256,
  })
}

function getCacheDir(): string {
  const dirName =
    process.env.NODE_ENV === 'development'
      ? PATHS.DEV_BROWSEROS_DIR_NAME
      : PATHS.BROWSEROS_DIR_NAME
  return path.join(homedir(), dirName, PATHS.CACHE_DIR_NAME)
}

export async function readLocalManifest(
  manifestPath: string,
): Promise<AgentManifest | null> {
  try {
    return JSON.parse(await readFile(manifestPath, 'utf8')) as AgentManifest
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw error
  }
}

async function downloadToFile(url: string, dest: string): Promise<void> {
  const response = await fetchWithTimeout(url)
  if (!response.ok || !response.body) {
    throw new Error(`download failed: ${url} (${response.status})`)
  }

  const sink = Bun.file(dest).writer()
  const reader = response.body.getReader()
  try {
    await pumpStream(reader, sink)
  } finally {
    await sink.end()
  }
}

async function pumpStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  sink: ChunkSink,
): Promise<void> {
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    sink.write(value)
  }
}

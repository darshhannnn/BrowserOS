#!/usr/bin/env bun
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { parseArgs } from 'node:util'
import { ARCHES } from './common/arch'
import { fetchWithTimeout } from './common/fetch'
import {
  type AgentEntry,
  type AgentManifest,
  type ArtifactInputs,
  type Bundle,
  type BundleAgent,
  buildManifest,
  tarballKey,
} from './common/manifest'
import { sha256File } from './common/sha256'

const { values } = parseArgs({
  args: Bun.argv.slice(2),
  options: {
    'dist-dir': { type: 'string', default: './dist' },
    out: { type: 'string' },
    slice: { type: 'string', default: 'full' },
    'merge-from': { type: 'string' },
  },
})

const distDir = values['dist-dir']
const slice = values.slice
const pkgRoot = path.resolve(import.meta.dir, '..')
const bundle = JSON.parse(
  await readFile(path.join(pkgRoot, 'bundle.json'), 'utf8'),
) as Bundle

if (slice !== 'full' && !slice.startsWith('agents:')) {
  throw new Error(`unknown slice: ${slice}`)
}

const baseline = values['merge-from']
  ? await loadBaseline(values['merge-from'])
  : null
if (slice !== 'full' && !baseline) {
  throw new Error(`--slice ${slice} requires --merge-from`)
}

const manifest = await buildSlicedManifest({ bundle, distDir, slice, baseline })
const outPath = values.out ?? path.join(distDir, 'manifest.json')
await mkdir(path.dirname(outPath), { recursive: true })
await writeFile(outPath, `${JSON.stringify(manifest, null, 2)}\n`)
console.log(`wrote ${outPath} (slice=${slice})`)

async function buildSlicedManifest(opts: {
  bundle: Bundle
  distDir: string
  slice: string
  baseline: AgentManifest | null
}): Promise<AgentManifest> {
  if (opts.slice === 'full') {
    return buildManifest(
      opts.bundle,
      await readAllInputs(opts.bundle, opts.distDir),
    )
  }

  const baseline = opts.baseline
  if (!baseline) throw new Error(`--slice ${opts.slice} requires --merge-from`)
  const updatedAt = new Date().toISOString()

  if (opts.slice.startsWith('agents:')) {
    const name = opts.slice.slice('agents:'.length)
    const agent = opts.bundle.agents.find((entry) => entry.name === name)
    if (!agent) throw new Error(`unknown agent: ${name}`)

    return {
      ...baseline,
      schemaVersion: 2,
      updatedAt,
      agents: {
        ...baseline.agents,
        [name]: await readAgentEntry(agent, opts.distDir),
      },
    }
  }

  throw new Error(`unknown slice: ${opts.slice}`)
}

async function readAllInputs(
  bundle: Bundle,
  distDir: string,
): Promise<ArtifactInputs> {
  const agents: ArtifactInputs['agents'] = {}
  for (const agent of bundle.agents) {
    agents[agent.name] = {} as ArtifactInputs['agents'][string]
    for (const arch of ARCHES) {
      const artifactPath = path.join(
        distDir,
        'images',
        path.basename(tarballKey(agent.name, agent.version, arch)),
      )
      agents[agent.name][arch] = await readArtifactInput(artifactPath)
    }
  }

  return {
    agents,
  }
}

async function readAgentEntry(
  agent: BundleAgent,
  distDir: string,
): Promise<AgentEntry> {
  const tarballs = {} as AgentEntry['tarballs']
  for (const arch of ARCHES) {
    const key = tarballKey(agent.name, agent.version, arch)
    const artifactPath = path.join(distDir, 'images', path.basename(key))
    tarballs[arch] = { key, ...(await readArtifactInput(artifactPath)) }
  }
  return { image: agent.image, version: agent.version, tarballs }
}

async function readArtifactInput(
  filePath: string,
): Promise<{ sha256: string; sizeBytes: number }> {
  return {
    sha256: await sha256File(filePath),
    sizeBytes: (await stat(filePath)).size,
  }
}

async function loadBaseline(src: string): Promise<AgentManifest> {
  if (src.startsWith('http://') || src.startsWith('https://')) {
    const response = await fetchWithTimeout(src)
    if (!response.ok) {
      throw new Error(`baseline fetch failed: ${src} (${response.status})`)
    }
    return (await response.json()) as AgentManifest
  }

  return JSON.parse(await readFile(src, 'utf8')) as AgentManifest
}

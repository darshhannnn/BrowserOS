import { ARCHES, type Arch } from './arch'

export interface Artifact {
  key: string
  sha256: string
  sizeBytes: number
}

export interface AgentEntry {
  image: string
  version: string
  tarballs: Record<Arch, Artifact>
}

export interface AgentManifest {
  schemaVersion: 2
  updatedAt: string
  agents: Record<string, AgentEntry>
}

export interface BundleAgent {
  name: string
  image: string
  version: string
}

export interface Bundle {
  agents: BundleAgent[]
}

export interface ArtifactInput {
  sha256: string
  sizeBytes: number
}

export interface ArtifactInputs {
  agents: Record<string, Record<Arch, ArtifactInput>>
}

export function tarballKey(name: string, version: string, arch: Arch): string {
  return `vm/images/${name}-${version}-${arch}.tar.gz`
}

export function buildManifest(
  bundle: Bundle,
  inputs: ArtifactInputs,
  now: Date = new Date(),
): AgentManifest {
  const agents: Record<string, AgentEntry> = {}
  for (const agent of bundle.agents) {
    const tarballs = {} as Record<Arch, Artifact>
    for (const arch of ARCHES) {
      const entry = inputs.agents[agent.name]?.[arch]
      if (!entry) {
        throw new Error(`missing tarball inputs for ${agent.name}/${arch}`)
      }
      tarballs[arch] = {
        key: tarballKey(agent.name, agent.version, arch),
        sha256: entry.sha256,
        sizeBytes: entry.sizeBytes,
      }
    }
    agents[agent.name] = {
      image: agent.image,
      version: agent.version,
      tarballs,
    }
  }

  return {
    schemaVersion: 2,
    updatedAt: now.toISOString(),
    agents,
  }
}

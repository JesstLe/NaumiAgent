export const artifactNavigationEvent = 'naumi:artifact-navigation'
export type ArtifactNavigation = { proceed: () => void; cancel?: () => void }

export function requestArtifactNavigation(proceed: () => void, cancel?: () => void) {
  const event = new CustomEvent<ArtifactNavigation>(artifactNavigationEvent, { cancelable: true, detail: { proceed, cancel } })
  if (window.dispatchEvent(event)) proceed()
}

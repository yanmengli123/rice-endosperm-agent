const KBASSET_URI_PATTERN = /^kbasset:\/\/([^/]+)\/([^/]+)\/([A-Za-z0-9_.-]+)$/

const requireRouteIdentity = (value, field) => {
  const normalized = String(value || '').trim()
  if (
    !normalized ||
    normalized.includes('/') ||
    normalized.includes('\\') ||
    normalized.includes('..')
  ) {
    throw new TypeError(`${field} is invalid`)
  }
  return normalized
}

/** Parse the persistent scientific-asset URI stored in canonical Markdown. */
export const parseKbAssetUri = (uri) => {
  const match = KBASSET_URI_PATTERN.exec(String(uri || ''))
  if (!match) return null
  const [, fileId, revisionId, assetName] = match
  return { fileId, revisionId, assetName }
}

/** Build the authenticated API endpoint without exposing the object-store layout. */
export const buildKnowledgeAssetUrl = (kbId, asset) => {
  const knowledgeBaseId = requireRouteIdentity(kbId, 'kbId')
  const fileId = requireRouteIdentity(asset?.fileId, 'fileId')
  const revisionId = requireRouteIdentity(asset?.revisionId, 'revisionId')
  const assetName = String(asset?.assetName || '')
  if (!/^[A-Za-z0-9_.-]+$/.test(assetName) || assetName.includes('..')) {
    throw new TypeError('assetName is invalid')
  }

  return `/api/knowledge/databases/${encodeURIComponent(
    knowledgeBaseId
  )}/documents/${encodeURIComponent(fileId)}/revisions/${encodeURIComponent(
    revisionId
  )}/assets/${encodeURIComponent(assetName)}`
}

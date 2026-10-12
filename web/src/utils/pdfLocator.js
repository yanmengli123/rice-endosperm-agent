/** Build one exclusive selector; inherited evidence anchors never override an asset. */
export const evidenceSourceIds = (evidence) =>
  [evidence?.evidence_id, evidence?.source?.span_evidence_id].filter(Boolean).map(String)

/** Match only ids issued by the authorized evidence endpoint; ambiguity fails closed. */
export const findSourceEvidence = (evidence, id) => {
  if (!id) return null
  const matches = evidence.filter((item) => evidenceSourceIds(item).includes(String(id)))
  return matches.length === 1 ? matches[0] : null
}

export const pdfLocatorRequest = (evidence, kbId, fileId) => {
  const source = evidence?.source || {}
  const request = {
    kb_id: kbId || source.kb_id,
    file_id: fileId || source.file_id,
    parse_revision_id: source.parse_revision_id,
    source_sha256: source.source_sha256
  }
  if (source.table_id) {
    request.table_id = source.table_id
    if (source.row_index != null && source.column_index != null) {
      request.row_index = source.row_index
      request.column_index = source.column_index
    }
  } else if (source.figure_label) {
    request.figure_label = source.figure_label
    if (source.panel_label) request.panel_label = source.panel_label
  } else if (source.span_id) request.span_id = source.span_id
  else if (source.region_id) request.region_id = source.region_id
  else request.anchor_id = source.anchor_id || evidence?.locator?.anchor_id
  // Parent sentence text is unrelated to a selected figure or table cell.
  if (!request.table_id && !request.figure_label) {
    request.quote_text = evidence?.locator?.highlight?.quote
  }
  return request
}

/** Attach a server-issued asset selector only within its parent evidence lineage. */
export const evidenceWithAssetSelector = (evidence, selector) => {
  if (!selector) return evidence
  const source = evidence?.source || {}
  if (
    ['kb_id', 'file_id', 'parse_revision_id'].some((key) => selector[key] !== source[key]) ||
    (selector.source_sha256 &&
      source.source_sha256 &&
      selector.source_sha256 !== source.source_sha256)
  )
    return null
  return {
    ...evidence,
    source: {
      ...selector,
      source_sha256: source.source_sha256 || selector.source_sha256,
      run_id: source.run_id
    }
  }
}

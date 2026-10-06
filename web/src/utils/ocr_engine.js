// Both knowledge-source upload and literature import use this default policy.
export function resolveDefaultOcrEngine({
  contractKey = '',
  formatTemplate = '',
  configuredEngine = ''
} = {}) {
  if (contractKey ? contractKey === 'pdf_evidence' : formatTemplate === 'pdf_literature')
    return 'mineru_official'
  return configuredEngine || 'rapid_ocr'
}

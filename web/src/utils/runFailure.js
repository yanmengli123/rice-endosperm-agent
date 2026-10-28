// Keep provider payloads out of the message area; the run archive retains diagnostics.
export function describeRunFailure(error) {
  const type = error?.type || error?.error_type || ''
  const reason = `${type} ${error?.message || error?.error_message || ''}`
  if (/authentication|invalid[ _-]*(?:api[ _-]*)?key|\b401\b/i.test(reason))
    return '模型服务鉴权失败，请检查该供应商的 API 密钥。'
  if (/rate.?limit|\b429\b/i.test(reason))
    return '模型服务限流，本次回答未完成，请稍后再试。'
  if (/timeout|timed.?out/i.test(reason)) return '模型服务响应超时，本次回答未完成。'
  if (/connection|connecterror|\b50[234]\b/i.test(reason))
    return '模型服务连接失败，本次回答未完成。'
  const safeType = /^[\w.-]{1,64}$/.test(type) ? `（${type}）` : ''
  return `生成失败${safeType}，请查看本轮状态了解原因。`
}

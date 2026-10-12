// DOMException fields can be read-only; wrap them without losing the cause.
export function normalizeTimeoutError(error) {
  if (error?.name !== 'TimeoutError') return error
  const wrapped = new Error('请求超时，请检查网络后重试', { cause: error })
  wrapped.name = 'TimeoutError'
  return wrapped
}

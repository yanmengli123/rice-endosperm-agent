// 鉴权下载的公共工具：解析 Content-Disposition 文件名并把 blob 响应保存为本地文件。

export const parseDownloadFilename = (contentDisposition) => {
  if (!contentDisposition) return ''

  const utf8Match = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch (error) {
      console.warn('解析 UTF-8 文件名失败:', error)
    }
  }

  const asciiMatch = contentDisposition.match(/filename="?([^";]+)"?/i)
  return asciiMatch?.[1] || ''
}

export const saveBlobResponse = async (response, fallbackName = '下载文件') => {
  const blob = await response.blob()
  const contentDisposition =
    response.headers.get('Content-Disposition') || response.headers.get('content-disposition')
  const filename = parseDownloadFilename(contentDisposition) || fallbackName
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
  return filename
}

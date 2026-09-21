// 鉴权下载的公共工具：解析 Content-Disposition 文件名并把 blob 响应保存为本地文件。

// RFC 5987 扩展参数：filename*=UTF-8''%E4%B8%AD%E6%96%87.html
const FILENAME_STAR_PATTERN = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i
// 普通参数：filename="a.html" / filename=a.html（(?!\*) 避免误吞 filename*）
const FILENAME_ASCII_PATTERN = /filename(?!\*)\s*=\s*(?:"([^"]*)"|([^;]+))/i

export const parseDownloadFilename = (contentDisposition) => {
  if (!contentDisposition) return ''

  const utf8Match = contentDisposition.match(FILENAME_STAR_PATTERN)
  if (utf8Match?.[2]) {
    try {
      return decodeURIComponent(utf8Match[2].trim())
    } catch (error) {
      console.warn('解析 UTF-8 文件名失败:', error)
    }
  }

  const asciiMatch = contentDisposition.match(FILENAME_ASCII_PATTERN)
  const asciiName = asciiMatch?.[1] ?? asciiMatch?.[2] ?? ''
  return asciiName.trim()
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

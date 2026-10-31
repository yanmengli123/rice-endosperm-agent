/** Decode SSE frames across arbitrary UTF-8 chunks, retaining opaque event IDs. */
export async function* publicSessionEvents(body) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, '\n')
      let boundary
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        let id = null
        const data = []
        for (const line of frame.split('\n')) {
          if (line.startsWith('id:')) id = line.slice(3).trimStart()
          if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
        }
        if (data.length) yield { id, data: JSON.parse(data.join('\n')) }
      }
      if (done) break
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

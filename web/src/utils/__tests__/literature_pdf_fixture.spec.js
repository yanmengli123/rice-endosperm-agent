// Optional real local PDF parsing smoke. No browser, OCR or model provider.
import assert from 'node:assert/strict'
import process from 'node:process'
import { readFile } from 'node:fs/promises'
import * as pdfjs from 'pdfjs-dist/legacy/build/pdf.mjs'
const path = process.env.LITERATURE_PDF_FIXTURE
if (!path) {
  console.log('Real PDF parsing smoke skipped: set LITERATURE_PDF_FIXTURE to a local test PDF')
} else {
  const bytes = await readFile(path)
  assert.ok(bytes.length > 0 && bytes.length <= 100 * 1024 * 1024, 'Fixture size budget')
  const task = pdfjs.getDocument({ data: new Uint8Array(bytes), isEvalSupported: false })
  let chars = 0,
    items = 0
  try {
    const doc = await task.promise
    assert.ok(doc.numPages > 0 && doc.numPages <= 1000)
    for (let number = 1; number <= doc.numPages; number++) {
      const page = await doc.getPage(number)
      const viewport = page.getViewport({ scale: 1 })
      assert.ok(Number.isFinite(viewport.width) && viewport.width > 0)
      assert.ok(Number.isFinite(viewport.height) && viewport.height > 0)
      const content = await page.getTextContent()
      for (const item of content.items) {
        if (typeof item.str !== 'string') continue
        chars += item.str.length
        items++
        assert.equal(item.transform.length, 6)
        assert.ok(item.transform.every(Number.isFinite), 'Text-layer transform is finite')
      }
      if (number === 1 && process.env.LITERATURE_PDF_EXPECT_RISBZ1 === '1')
        assert.match(content.items.map((item) => item.str || '').join(' '), /RISBZ1/)
      page.cleanup()
    }
    assert.ok(chars > 1000 && items > 50, 'Selectable-text data is present')
    console.log(
      `Local PDF.js smoke passed: ${doc.numPages} pages, ${items} text items, ${chars} characters; no provider calls`
    )
  } finally {
    await task.destroy()
  }
}

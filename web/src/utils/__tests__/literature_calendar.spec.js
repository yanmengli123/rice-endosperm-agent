import assert from 'node:assert/strict'
import { monthDays, adjacentMonth } from '../literature_calendar.js'
const leap = monthDays('2024-02')
assert.equal(leap.filter(Boolean).length, 29)
assert.equal(leap[3], '2024-02-01')
assert.equal(leap.length % 7, 0)
assert.equal(monthDays('2025-02').filter(Boolean).length, 28)
assert.equal(monthDays('2026-06')[0], '2026-06-01')
assert.equal(adjacentMonth('2026-01', -1), '2025-12')
assert.equal(adjacentMonth('2026-12', 1), '2027-01')
for (const invalid of ['2026-13', '2026-00', '2026-1', 'bad', '0000-01']) {
  assert.deepEqual(monthDays(invalid), [])
  assert.equal(adjacentMonth(invalid, 1), '')
}
assert.equal(adjacentMonth('9999-12', 1), '')
console.log('Literature calendar leap year, week alignment and month boundaries passed')

/**
 * The half-open range the group page sends, and the catch-up count it
 * shows. Both are arithmetic the page would otherwise hide inside JSX.
 */
import { describe, expect, it } from 'vitest'

import {
  catchUpMonths,
  exclusiveEnd,
  inclusiveEnd,
  keepInOrder,
  presetRange,
} from '@/lib/group-period'

const may15 = new Date('2026-05-15T12:00:00')

describe('presetRange', () => {
  it('ends a month on the first of the next one', () => {
    expect(presetRange('thisMonth', may15)).toEqual({ start: '2026-05-01', end: '2026-06-01' })
  })

  it('reads last month as the month before', () => {
    expect(presetRange('lastMonth', may15)).toEqual({ start: '2026-04-01', end: '2026-05-01' })
  })

  it('reads this year as the calendar year', () => {
    expect(presetRange('thisYear', may15)).toEqual({ start: '2026-01-01', end: '2027-01-01' })
  })

  it('leaves all time unbounded', () => {
    expect(presetRange('allTime', may15)).toEqual({ start: null, end: null })
  })
})

describe('the inclusive end a date picker shows', () => {
  it('is the day before the exclusive end', () => {
    expect(inclusiveEnd('2026-06-01')).toBe('2026-05-31')
    expect(inclusiveEnd(null)).toBe('')
  })

  it('round-trips back to the exclusive end', () => {
    expect(exclusiveEnd(inclusiveEnd('2026-06-01'))).toBe('2026-06-01')
    expect(exclusiveEnd('')).toBeNull()
  })
})

describe('keepInOrder', () => {
  it('leaves a range that is already in order alone', () => {
    expect(keepInOrder('2026-05-01', '2026-05-31', 'start')).toEqual({
      start: '2026-05-01',
      lastDay: '2026-05-31',
    })
  })

  it('takes the end along when the start moves past it', () => {
    expect(keepInOrder('2026-06-10', '2026-05-31', 'start')).toEqual({
      start: '2026-06-10',
      lastDay: '2026-06-10',
    })
  })

  it('takes the start along when the end moves before it', () => {
    expect(keepInOrder('2026-05-01', '2026-04-10', 'end')).toEqual({
      start: '2026-04-10',
      lastDay: '2026-04-10',
    })
  })

  it('leaves a half-set range as it is', () => {
    expect(keepInOrder('', '2026-05-31', 'end')).toEqual({ start: '', lastDay: '2026-05-31' })
    expect(keepInOrder('2026-05-01', '', 'start')).toEqual({ start: '2026-05-01', lastDay: '' })
  })
})

describe('catchUpMonths', () => {
  it('rounds a part month up', () => {
    expect(catchUpMonths(2500, 1000)).toBe(3)
    expect(catchUpMonths(2000, 1000)).toBe(2)
  })

  it('has nothing to say without a backlog or a positive catch-up', () => {
    expect(catchUpMonths(0, 1000)).toBeNull()
    expect(catchUpMonths(-500, 1000)).toBeNull()
    expect(catchUpMonths(2500, 0)).toBeNull()
    expect(catchUpMonths(2500, -10)).toBeNull()
  })
})

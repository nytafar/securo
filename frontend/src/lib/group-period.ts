/**
 * The period arithmetic behind the group page.
 *
 * The API's range is half-open: `start` is inclusive, `end` exclusive, so
 * May 2026 is 2026-05-01 → 2026-06-01. A date picker is not, so the
 * custom range shows the last day the user means and this module is the
 * one place that converts between the two.
 */
import { addDays, addMonths, addYears, format, startOfYear } from 'date-fns'

export type PeriodPreset = 'thisMonth' | 'lastMonth' | 'thisYear' | 'allTime' | 'custom'

/** Half-open [start, end). `null` on a side means unbounded there. */
export interface PeriodRange {
  start: string | null
  end: string | null
}

export const PERIOD_PRESETS: PeriodPreset[] = [
  'thisMonth',
  'lastMonth',
  'thisYear',
  'allTime',
  'custom',
]

const iso = (date: Date) => format(date, 'yyyy-MM-dd')

/** A calendar month, as `YYYY-MM`. The month picker's whole state. */
export type CalendarMonth = string

/** The month a date falls in. */
export function monthOf(today: Date = new Date()): CalendarMonth {
  return format(today, 'yyyy-MM')
}

/** A month moved by whole months, across year boundaries. */
export function shiftMonth(month: CalendarMonth, delta: number): CalendarMonth {
  const [year, index] = month.split('-').map(Number)
  return format(addMonths(new Date(year, index - 1, 1), delta), 'yyyy-MM')
}

/** The half-open range a calendar month stands for: the first of the
 *  month up to, but not including, the first of the next. */
export function monthRange(month: CalendarMonth): PeriodRange {
  return { start: `${month}-01`, end: `${shiftMonth(month, 1)}-01` }
}

/** The month's name and year in the reader's language. Month names come
 *  from the platform, never from a translation key per month. */
export function monthLabel(month: CalendarMonth, locale: string): string {
  const [year, index] = month.split('-').map(Number)
  return new Date(year, index - 1, 1).toLocaleDateString(locale, {
    month: 'long',
    year: 'numeric',
  })
}

/** The range a preset stands for. `custom` keeps whatever the user set,
 *  so it is answered with an unbounded range and the caller's dates.
 *  The two month presets are positions of the month picker, so they are
 *  answered by the same arithmetic. */
export function presetRange(preset: PeriodPreset, today: Date = new Date()): PeriodRange {
  switch (preset) {
    case 'thisMonth':
      return monthRange(monthOf(today))
    case 'lastMonth':
      return monthRange(shiftMonth(monthOf(today), -1))
    case 'thisYear':
      return { start: iso(startOfYear(today)), end: iso(startOfYear(addYears(today, 1))) }
    default:
      return { start: null, end: null }
  }
}

/** The last day inside the range, for a date picker to show. */
export function inclusiveEnd(end: string | null): string {
  if (!end) return ''
  return iso(addDays(new Date(`${end}T00:00:00`), -1))
}

/** The exclusive `end` for a last day the user picked. */
export function exclusiveEnd(lastDay: string): string | null {
  if (!lastDay) return null
  return iso(addDays(new Date(`${lastDay}T00:00:00`), 1))
}

/**
 * Keeps a custom range in order.
 *
 * The two pickers are independent, so a user can move the start past the
 * end. Rather than refusing the pick, the other end follows: the range
 * stays valid, and `start > end` — which the API answers with a 400 —
 * is never sent. `lastDay` is the inclusive end the pickers show.
 */
export function keepInOrder(
  start: string,
  lastDay: string,
  moved: 'start' | 'end',
): { start: string; lastDay: string } {
  if (!start || !lastDay || start <= lastDay) return { start, lastDay }
  return moved === 'start' ? { start, lastDay: start } : { start: lastDay, lastDay }
}

/**
 * Months of a given monthly catch-up needed to clear a backlog.
 *
 * `null` whenever there is nothing to compute: no backlog, a backlog in
 * the member's favour, or a catch-up that is zero or negative and would
 * never clear anything. Nothing here is stored.
 */
export function catchUpMonths(backlog: number, monthly: number): number | null {
  if (!(backlog > 0) || !(monthly > 0)) return null
  return Math.ceil(backlog / monthly)
}

/**
 * The period arithmetic behind the group page.
 *
 * The API's range is half-open: `start` is inclusive, `end` exclusive, so
 * May 2026 is 2026-05-01 → 2026-06-01. A date picker is not, so the
 * custom range shows the last day the user means and this module is the
 * one place that converts between the two.
 */
import { addDays, addMonths, addYears, format, startOfMonth, startOfYear, subMonths } from 'date-fns'

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

/** The range a preset stands for. `custom` keeps whatever the user set,
 *  so it is answered with an unbounded range and the caller's dates. */
export function presetRange(preset: PeriodPreset, today: Date = new Date()): PeriodRange {
  switch (preset) {
    case 'thisMonth':
      return { start: iso(startOfMonth(today)), end: iso(startOfMonth(addMonths(today, 1))) }
    case 'lastMonth':
      return {
        start: iso(startOfMonth(subMonths(today, 1))),
        end: iso(startOfMonth(today)),
      }
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

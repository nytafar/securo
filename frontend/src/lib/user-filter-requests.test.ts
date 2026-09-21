/**
 * The user filter on the wire.
 *
 * The consumption endpoints have to receive the *person*, not the list
 * of accounts that person owns: account ids say "cash flow over these
 * accounts", which is what a collection means, and that reading drops
 * every share of a cost somebody else paid. These tests pin the exact
 * query a filtered dashboard, report and budget comparison send.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.hoisted(() => vi.fn())

vi.mock('axios', () => ({
  default: {
    create: () => ({
      get,
      post: vi.fn(),
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    }),
  },
}))

const HER = 'user-her'

beforeEach(() => {
  get.mockReset()
  get.mockResolvedValue({ data: {} })
})

async function paramsOf(call: () => Promise<unknown>): Promise<Record<string, unknown>> {
  await call()
  return get.mock.calls[0][1].params
}

describe('a page under the user filter', () => {
  it('asks the dashboard for a person, not for a set of accounts', async () => {
    const { dashboard } = await import('@/lib/api')

    const summary = await paramsOf(() =>
      dashboard.summary('2026-09-01', undefined, undefined, undefined, HER),
    )
    expect(summary.user_id).toBe(HER)
    expect(summary.account_ids).toBeUndefined()

    get.mockClear()
    const spending = await paramsOf(() =>
      dashboard.spendingByCategory('2026-09-01', undefined, HER),
    )
    expect(spending.user_id).toBe(HER)
    expect(spending.account_ids).toBeUndefined()

    get.mockClear()
    const trend = await paramsOf(() => dashboard.monthlyTrend(6, undefined, HER))
    expect(trend.user_id).toBe(HER)

    get.mockClear()
    const balances = await paramsOf(() => dashboard.balanceHistory('2026-09-01', undefined, HER))
    expect(balances.user_id).toBe(HER)
  })

  it('asks the reports for the same person', async () => {
    const { reports } = await import('@/lib/api')

    const income = await paramsOf(() =>
      reports.incomeExpenses(12, 'monthly', undefined, undefined, undefined, HER),
    )
    expect(income.user_id).toBe(HER)

    get.mockClear()
    const netWorth = await paramsOf(() =>
      reports.netWorth(12, 'monthly', undefined, undefined, undefined, HER),
    )
    expect(netWorth.user_id).toBe(HER)

    get.mockClear()
    const cashFlow = await paramsOf(() =>
      reports.cashFlow(6, 'daily', false, undefined, HER),
    )
    expect(cashFlow.user_id).toBe(HER)
  })

  it('asks the budget comparison for the same person', async () => {
    const { budgets } = await import('@/lib/api')

    const comparison = await paramsOf(() => budgets.comparison('2026-09-01', HER))
    expect(comparison.user_id).toBe(HER)
  })

  it('sends no person at all when the filter is a collection', async () => {
    const { dashboard } = await import('@/lib/api')

    const spending = await paramsOf(() =>
      dashboard.spendingByCategory('2026-09-01', ['acct-her']),
    )
    expect(spending.user_id).toBeUndefined()
    expect(spending.account_ids).toEqual(['acct-her'])
  })
})

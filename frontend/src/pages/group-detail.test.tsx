/**
 * The group page as a common pot, against a mocked period endpoint.
 *
 * The figures asserted here are the ones the page exists to answer: what
 * to transfer for the period, what to transfer including the backlog,
 * the shared income that is not hidden inside a category, and how long a
 * catch-up takes. The transaction page is deliberately one row shorter
 * than the period it belongs to, so a breakdown computed from the loaded
 * rows — the bug this page had — would show the wrong totals.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'

import GroupDetailPage from '@/pages/group-detail'
import { presetRange } from '@/lib/group-period'
import { renderWithProviders, t } from '@/test/utils'

const api = vi.hoisted(() => ({
  groups: {
    get: vi.fn(),
    period: vi.fn(),
    members: { create: vi.fn(), update: vi.fn(), delete: vi.fn() },
    settlements: { create: vi.fn(), delete: vi.fn() },
  },
  accounts: { list: vi.fn() },
  transactions: { list: vi.fn() },
  users: { lookupByEmail: vi.fn(), list: vi.fn() },
}))

vi.mock('@/lib/api', () => ({
  groups: api.groups,
  accounts: api.accounts,
  transactions: api.transactions,
  users: api.users,
}))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { id: 'user-1' } }),
}))

vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({ canWrite: true }),
}))

const ME = 'member-me'
const ANNA = 'member-anna'

const group = {
  id: 'group-1',
  user_id: 'user-1',
  name: 'Home',
  kind: 'household',
  default_currency: 'USD',
  icon: 'home',
  color: '#000000',
  is_archived: false,
  is_owner: true,
  notes: null,
  created_at: '2026-01-01T00:00:00Z',
  members: [
    {
      id: ME,
      group_id: 'group-1',
      name: 'Me',
      linked_user_id: 'user-1',
      email: null,
      is_self: true,
      created_at: '2026-01-01T00:00:00Z',
    },
    {
      id: ANNA,
      group_id: 'group-1',
      name: 'Anna',
      linked_user_id: 'user-2',
      email: 'anna@example.com',
      is_self: false,
      created_at: '2026-01-02T00:00:00Z',
    },
  ],
}

/**
 * May: 3 000 of groceries paid by me, a 400 refund in the same category,
 * and 1 000 Anna already sent. Anna carries 2 000 from earlier periods.
 */
const thisMonth = {
  group_id: 'group-1',
  kind: 'household',
  default_currency: 'USD',
  start: '2026-05-01',
  end: '2026-06-01',
  owner_member_id: ME,
  members: [
    { id: ME, name: 'Me', is_owner_member: true },
    { id: ANNA, name: 'Anna', is_owner_member: false },
  ],
  positions: [
    {
      member_id: ME,
      currency: 'USD',
      paid: 2600,
      share: 1300,
      contributions_made: 0,
      contributions_received: 1000,
      period_position: -300,
      backlog: -2000,
      running_position: -2300,
      period_position_in_default_currency: -300,
      running_position_in_default_currency: -2300,
    },
    {
      member_id: ANNA,
      currency: 'USD',
      paid: 0,
      share: 1300,
      contributions_made: 1000,
      contributions_received: 0,
      period_position: 300,
      backlog: 2000,
      running_position: 2300,
      period_position_in_default_currency: 300,
      running_position_in_default_currency: 2300,
    },
  ],
  transfers_period: [
    { from_member_id: ANNA, to_member_id: ME, currency: 'USD', amount: 300 },
  ],
  transfers_running: [
    { from_member_id: ANNA, to_member_id: ME, currency: 'USD', amount: 2300 },
  ],
  costs: [
    {
      category_id: 'cat-food',
      category_name: 'Groceries',
      currency: 'USD',
      total: 3000,
      shares: [
        { member_id: ME, amount: 1500 },
        { member_id: ANNA, amount: 1500 },
      ],
    },
  ],
  shared_income: [
    {
      category_id: 'cat-food',
      category_name: 'Groceries',
      currency: 'USD',
      total: 400,
      shares: [
        { member_id: ME, amount: 200 },
        { member_id: ANNA, amount: 200 },
      ],
    },
  ],
  totals: [
    {
      currency: 'USD',
      costs: 3000,
      shared_income: 400,
      net: 2600,
      shares: [
        { member_id: ME, amount: 1300 },
        { member_id: ANNA, amount: 1300 },
      ],
    },
  ],
  contributions: [
    {
      id: 'contribution-1',
      from_member_id: ANNA,
      to_member_id: ME,
      amount: 1000,
      currency: 'USD',
      date: '2026-05-20',
      transaction_id: null,
      receiver_transaction_id: null,
      notes: 'May transfer',
    },
  ],
  payer_assumed_transactions: [
    {
      id: 'tx-refund',
      date: '2026-05-06',
      booked_date: '2026-05-06',
      description: 'Groceries refund',
      type: 'credit',
      amount: 400,
      currency: 'USD',
      category_id: 'cat-food',
      category_name: 'Groceries',
      account_id: 'acc-1',
      account_name: 'Wallet',
      payer_member_id: ME,
      payer_assumed: true,
      shared_total: -400,
      shares: [
        { member_id: ME, amount: -200 },
        { member_id: ANNA, amount: -200 },
      ],
    },
  ],
  // One row of the two the period holds: the figures above must not
  // depend on what this page happens to carry.
  transactions: {
    items: [
      {
        id: 'tx-refund',
        date: '2026-05-06',
        booked_date: '2026-05-06',
        description: 'Groceries refund',
        type: 'credit',
        amount: 400,
        currency: 'USD',
        category_id: 'cat-food',
        category_name: 'Groceries',
        account_id: 'acc-1',
        account_name: 'Wallet',
        payer_member_id: ME,
        payer_assumed: true,
        shared_total: -400,
        shares: [
          { member_id: ME, amount: -200 },
          { member_id: ANNA, amount: -200 },
        ],
      },
    ],
    total: 2,
    page: 1,
    page_size: 1,
  },
}

/** April: nothing shared, nothing to move. */
const lastMonth = {
  ...thisMonth,
  start: '2026-04-01',
  end: '2026-05-01',
  positions: thisMonth.positions.map((position) => ({
    ...position,
    paid: 0,
    share: 0,
    contributions_made: 0,
    contributions_received: 0,
    period_position: 0,
    backlog: 0,
    running_position: 0,
    period_position_in_default_currency: 0,
    running_position_in_default_currency: 0,
  })),
  transfers_period: [],
  transfers_running: [],
  costs: [],
  shared_income: [],
  totals: [],
  contributions: [],
  payer_assumed_transactions: [],
  transactions: { items: [], total: 0, page: 1, page_size: 25 },
}

function renderPage() {
  return renderWithProviders(<GroupDetailPage />, {
    route: '/groups/group-1',
    path: '/groups/:id',
  })
}

/** Renders and waits for the period response to land. */
async function renderLoaded() {
  const result = renderPage()
  await screen.findByText(/May transfer/)
  return result
}

/** The card carrying the given heading. */
function card(title: string): HTMLElement {
  return screen.getByText(title).closest('div.bg-card') as HTMLElement
}

beforeEach(() => {
  vi.clearAllMocks()
  api.groups.get.mockResolvedValue(group)
  api.groups.period.mockImplementation(
    async (_id: string, params: { start?: string | null }) =>
      params?.start === presetRange('lastMonth').start ? lastMonth : thisMonth,
  )
  api.accounts.list.mockResolvedValue([])
  api.transactions.list.mockResolvedValue({ items: [], total: 0 })
  api.users.list.mockResolvedValue([])
})

describe('the group page as a common pot', () => {
  it('opens a household on the current month', async () => {
    renderPage()
    await waitFor(() => expect(api.groups.period).toHaveBeenCalled())
    const [, params] = api.groups.period.mock.calls[0]
    expect(params).toMatchObject(presetRange('thisMonth'))
  })

  it('shows the transfer for the period and the one including the backlog', async () => {
    await renderLoaded()

    const periodCard = card(t('splitGroups.pot.transfersPeriod'))
    expect(within(periodCard).getByText('$300.00')).toBeInTheDocument()

    const runningCard = card(t('splitGroups.pot.transfersRunning'))
    expect(within(runningCard).getByText('$2,300.00')).toBeInTheDocument()
    // The direction names who pays whom, so a backlog in a member's
    // favour reads as the others paying them.
    expect(within(runningCard).getAllByText('Anna')[0]).toBeInTheDocument()
  })

  it('shows shared income on its own line, apart from the category cost', async () => {
    await renderLoaded()

    const costs = card(t('splitGroups.pot.costs'))
    expect(within(costs).getByText(t('splitGroups.pot.sharedIncome'))).toBeInTheDocument()
    // The cost stays gross and the income is its own, negative line.
    expect(within(costs).getByText('$3,000.00')).toBeInTheDocument()
    expect(within(costs).getByText('-$400.00')).toBeInTheDocument()
    // The net comes from the server, not from subtracting the two here.
    expect(within(costs).getByText('$2,600.00')).toBeInTheDocument()
  })

  it('counts the months a catch-up needs', async () => {
    const { user } = await renderLoaded()

    const input = screen.getByLabelText(t('splitGroups.pot.catchUpMonthly'))
    expect(
      screen.getByText(t('splitGroups.pot.catchUpEnterAmount')),
    ).toBeInTheDocument()

    await user.type(input, '500')

    expect(
      await screen.findByText(
        t('splitGroups.pot.catchUpResult', {
          months: 4,
          name: 'Anna',
          amount: '$2,000.00',
        }),
      ),
    ).toBeInTheDocument()
  })

  it('marks a transaction whose payer was assumed', async () => {
    renderPage()
    expect(
      await screen.findByText(t('splitGroups.pot.payerAssumed')),
    ).toBeInTheDocument()
    expect(
      screen.getByText(t('splitGroups.pot.payerAssumedHint', { total: 1 })),
    ).toBeInTheDocument()
  })

  it('follows the period everywhere when it changes', async () => {
    const { user } = await renderLoaded()

    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.lastMonth') }))

    await waitFor(() =>
      expect(
        api.groups.period.mock.calls.some(
          ([, params]) => params?.start === presetRange('lastMonth').start,
        ),
      ).toBe(true),
    )
    expect(await screen.findByText(t('splitGroups.pot.noCosts'))).toBeInTheDocument()
    expect(screen.queryByText('$300.00')).not.toBeInTheDocument()
    expect(screen.getAllByText(t('splitGroups.pot.nothingToMove')).length).toBe(2)
    expect(screen.getByText(t('splitGroups.pot.catchUpNoBacklog'))).toBeInTheDocument()
  })

  it('labels a household with contributions and a social group with settlements', async () => {
    const { unmount } = renderPage()
    expect(
      await screen.findByText(t('splitGroups.pot.contributions')),
    ).toBeInTheDocument()
    unmount()

    api.groups.get.mockResolvedValue({ ...group, kind: 'social' })
    api.groups.period.mockResolvedValue({ ...thisMonth, kind: 'social' })
    renderPage()
    expect(await screen.findByText(t('splitGroups.settlements'))).toBeInTheDocument()
    // A social group opens on the running position, not on the month.
    const [, params] = api.groups.period.mock.calls.at(-1)!
    expect(params).toMatchObject({ start: null, end: null })
  })

  it('records a contribution from a suggested transfer', async () => {
    api.groups.settlements.create.mockResolvedValue({ id: 'contribution-2' })
    const { user } = await renderLoaded()

    const periodCard = card(t('splitGroups.pot.transfersPeriod'))
    await user.click(
      within(periodCard).getByRole('button', {
        name: t('splitGroups.pot.recordContribution'),
      }),
    )

    await user.click(screen.getByRole('button', { name: t('common.save') }))

    await waitFor(() => expect(api.groups.settlements.create).toHaveBeenCalled())
    expect(api.groups.settlements.create).toHaveBeenCalledWith(
      'group-1',
      expect.objectContaining({
        from_member_id: ANNA,
        to_member_id: ME,
        amount: 300,
        currency: 'USD',
      }),
    )
  })

  it('pages the transaction list on the server', async () => {
    const { user } = renderPage()

    expect(
      await screen.findByText(t('splitGroups.pot.pageOf', { page: 1, pages: 2 })),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('common.next') }))

    await waitFor(() =>
      expect(
        api.groups.period.mock.calls.some(([, params]) => params?.page === 2),
      ).toBe(true),
    )
  })
})

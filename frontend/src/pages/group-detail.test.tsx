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
import {
  monthLabel,
  monthOf,
  monthRange,
  presetRange,
  shiftMonth,
} from '@/lib/group-period'
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

/** The member names of a transfer card's rows, in the order they read:
 *  [payer, receiver] per row. */
function transferDirections(cardEl: HTMLElement): string[][] {
  return within(cardEl)
    .getAllByRole('listitem')
    .map((row) =>
      within(row)
        .getAllByText(/^(Me|Anna)$/)
        .map((node) => node.textContent ?? ''),
    )
}

/** One row of the positions table, cell by cell. */
function positionRow(name: string): string[] {
  const row = within(card(t('splitGroups.pot.positions')))
    .getAllByRole('row')
    .find((candidate) => within(candidate).queryByText(name) !== null)!
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent ?? '')
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
    // Anna pays Me, not the other way round.
    expect(transferDirections(periodCard)).toEqual([['Anna', 'Me']])

    const runningCard = card(t('splitGroups.pot.transfersRunning'))
    expect(within(runningCard).getByText('$2,300.00')).toBeInTheDocument()
    expect(transferDirections(runningCard)).toEqual([['Anna', 'Me']])
  })

  it('shows the backlog per member, and in whose favour it runs', async () => {
    await renderLoaded()

    // Member, paid, share, sent, received, this period, backlog, running.
    expect(positionRow('Anna')).toEqual([
      'Anna',
      '$0.00',
      '$1,300.00',
      '$1,000.00',
      '$0.00',
      '$300.00',
      '$2,000.00',
      '$2,300.00',
    ])
    // The same backlog from the other side: the pot owes Me, so both the
    // backlog and the running position read negative.
    expect(positionRow('Me')).toEqual([
      'Me',
      '$2,600.00',
      '$1,300.00',
      '$0.00',
      '$1,000.00',
      '-$300.00',
      '-$2,000.00',
      '-$2,300.00',
    ])
    expect(
      screen.getByText(t('splitGroups.pot.positionsHint')),
    ).toBeInTheDocument()
  })

  it('turns the transfers round when the backlog runs the other way', async () => {
    // The mirror image: Me owes the pot, Anna is owed.
    api.groups.period.mockResolvedValue({
      ...thisMonth,
      positions: thisMonth.positions.map((position) => ({
        ...position,
        period_position: -position.period_position,
        backlog: -position.backlog,
        running_position: -position.running_position,
      })),
      transfers_period: [
        { from_member_id: ME, to_member_id: ANNA, currency: 'USD', amount: 300 },
      ],
      transfers_running: [
        { from_member_id: ME, to_member_id: ANNA, currency: 'USD', amount: 2300 },
      ],
    })
    await renderLoaded()

    expect(transferDirections(card(t('splitGroups.pot.transfersPeriod')))).toEqual([
      ['Me', 'Anna'],
    ])
    expect(transferDirections(card(t('splitGroups.pot.transfersRunning')))).toEqual([
      ['Me', 'Anna'],
    ])
    expect(positionRow('Anna')[6]).toBe('-$2,000.00')
    expect(positionRow('Me')[6]).toBe('$2,000.00')
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
          count: 4,
          name: 'Anna',
          amount: '$2,000.00',
        }),
      ),
    ).toBeInTheDocument()
    expect(screen.getByText(/4 months/)).toBeInTheDocument()

    // One month reads as one month, not "1 months".
    await user.clear(input)
    await user.type(input, '2000')

    expect(await screen.findByText(/1 month to clear/)).toBeInTheDocument()
    expect(screen.queryByText(/1 months/)).not.toBeInTheDocument()
  })

  it('asks for the month the picker names, across the new year', async () => {
    // The nearest December behind us: in the picker's two-year window
    // whatever month the suite happens to run in.
    let december = monthOf()
    while (!december.endsWith('-12')) december = shiftMonth(december, -1)
    const year = Number(december.slice(0, 4))

    const decemberPeriod = {
      ...thisMonth,
      start: `${year}-12-01`,
      end: `${year + 1}-01-01`,
      costs: [
        {
          ...thisMonth.costs[0],
          category_name: 'Christmas',
          total: 900,
          shares: [
            { member_id: ME, amount: 450 },
            { member_id: ANNA, amount: 450 },
          ],
        },
      ],
      shared_income: [],
      totals: [
        {
          currency: 'USD',
          costs: 900,
          shared_income: 0,
          net: 900,
          shares: [
            { member_id: ME, amount: 450 },
            { member_id: ANNA, amount: 450 },
          ],
        },
      ],
    }
    api.groups.period.mockImplementation(async (_id: string, params: { start?: string | null }) =>
      params?.start === `${year}-12-01` ? decemberPeriod : thisMonth,
    )

    const { user } = await renderLoaded()
    await user.selectOptions(
      screen.getByLabelText(t('splitGroups.pot.monthLabel')),
      december,
    )

    await waitFor(() =>
      expect(
        api.groups.period.mock.calls.some(
          ([, params]) =>
            params?.start === `${year}-12-01` && params?.end === `${year + 1}-01-01`,
        ),
      ).toBe(true),
    )
    // Every section follows: the breakdown is December's.
    expect(await screen.findByText('Christmas')).toBeInTheDocument()
    const christmas = within(card(t('splitGroups.pot.costs')))
      .getAllByRole('row')
      .find((row) => within(row).queryByText('Christmas') !== null)!
    expect(within(christmas).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'Christmas',
      '$900.00',
      '$450.00',
      '$450.00',
    ])

    // And the arrow rolls over into January.
    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.nextMonth') }))
    await waitFor(() =>
      expect(
        api.groups.period.mock.calls.some(
          ([, params]) =>
            params?.start === `${year + 1}-01-01` && params?.end === `${year + 1}-02-01`,
        ),
      ).toBe(true),
    )
    expect(
      (screen.getByLabelText(t('splitGroups.pot.monthLabel')) as HTMLSelectElement).value,
    ).toBe(`${year + 1}-01`)
  })

  it('moves the month picker with the two month presets', async () => {
    const { user } = await renderLoaded()
    const picker = () => screen.getByLabelText(t('splitGroups.pot.monthLabel')) as HTMLSelectElement

    // A household opens on this month, and the button says so.
    expect(picker().value).toBe(monthOf())
    expect(screen.getByRole('button', { name: t('splitGroups.pot.thisMonth') })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.lastMonth') }))
    expect(picker().value).toBe(shiftMonth(monthOf(), -1))
    await waitFor(() =>
      expect(
        api.groups.period.mock.calls.some(
          ([, params]) => params?.start === presetRange('lastMonth').start,
        ),
      ).toBe(true),
    )

    // Stepping back once more is no longer "last month", and the button
    // stops claiming it is.
    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.previousMonth') }))
    expect(picker().value).toBe(shiftMonth(monthOf(), -2))
    expect(screen.getByRole('button', { name: t('splitGroups.pot.lastMonth') })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(
      screen.getByText(monthLabel(shiftMonth(monthOf(), -2), 'en-US')),
    ).toBeInTheDocument()

    // Stepping forward twice lands on this month, and it says so again.
    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.nextMonth') }))
    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.nextMonth') }))
    expect(picker().value).toBe(monthOf())
    expect(screen.getByRole('button', { name: t('splitGroups.pot.thisMonth') })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    for (const [, params] of api.groups.period.mock.calls) {
      if (params?.start) expect(params.start).toBe(monthRange(params.start.slice(0, 7)).start)
    }
  })

  it('leaves the calculator out when the range has no start', async () => {
    const { user } = await renderLoaded()
    expect(screen.getByText(t('splitGroups.pot.catchUp'))).toBeInTheDocument()

    // All time carries no backlog by definition, so there is nothing for
    // the calculator to answer.
    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.allTime') }))

    await waitFor(() =>
      expect(screen.queryByText(t('splitGroups.pot.catchUp'))).not.toBeInTheDocument(),
    )
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

  it('links the credit that landed as the receiver side of a contribution', async () => {
    // The household's real case: Anna transfers, the owner marks the
    // credit on his own account. It is his receiving leg, so it must go
    // in the receiver column — the payer column is where history put it,
    // not where a new record belongs.
    api.groups.settlements.create.mockResolvedValue({ id: 'contribution-2' })
    // His account and hers, both in the one workspace the household
    // shares — which is why the picker has to be filtered at all.
    api.accounts.list.mockResolvedValue([
      { id: 'account-mine', user_id: 'user-1', name: 'Mine', display_name: null },
      { id: 'account-hers', user_id: 'user-2', name: 'Hers', display_name: null },
    ])
    api.transactions.list.mockResolvedValue({
      items: [
        {
          id: 'tx-credit',
          date: '2026-09-05',
          description: 'Transfer from Anna',
          amount: 300,
          currency: 'USD',
          type: 'credit',
        },
      ],
      total: 1,
    })
    const { user } = await renderLoaded()

    const periodCard = card(t('splitGroups.pot.transfersPeriod'))
    await user.click(
      within(periodCard).getByRole('button', {
        name: t('splitGroups.pot.recordContribution'),
      }),
    )

    const dialog = await screen.findByRole('dialog')
    const txAction = within(dialog)
      .getByRole('option', { name: t('splitGroups.txActionExisting') })
      .closest('select')!
    await user.selectOptions(txAction, 'existing')
    // The receiver writes no fresh debit — the credit already landed —
    // so that option is not offered to them.
    expect(
      within(dialog).queryByRole('option', { name: t('splitGroups.txActionCreate') }),
    ).not.toBeInTheDocument()
    // It is the credits that are searched, not the debits, and only the
    // viewer's own accounts: a link the API accepts has to sit on the
    // account of the member on that side.
    await waitFor(() =>
      expect(api.transactions.list).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'credit', account_ids: ['account-mine'] }),
      ),
    )

    await user.click(await within(dialog).findByText(/Transfer from Anna/))
    await user.click(within(dialog).getByRole('button', { name: t('common.save') }))

    await waitFor(() => expect(api.groups.settlements.create).toHaveBeenCalled())
    const [, payload] = api.groups.settlements.create.mock.calls.at(-1)!
    expect(payload.receiver_transaction_id).toBe('tx-credit')
    expect(payload.transaction_id).toBeUndefined()
  })

  it('never offers a linked member a pair the API would refuse', async () => {
    // A linked member may record a contribution she is part of, on
    // either side, and nothing between two other people. Moving one side
    // away from her takes the other side to her, so the dialog cannot be
    // walked into a 403.
    const THIRD = 'member-third'
    api.groups.get.mockResolvedValue({
      ...group,
      is_owner: false,
      user_id: 'user-9',
      members: [
        ...group.members,
        {
          id: THIRD,
          group_id: 'group-1',
          name: 'Third',
          linked_user_id: null,
          email: null,
          is_self: false,
          created_at: '2026-01-03T00:00:00Z',
        },
      ],
    })
    api.groups.settlements.create.mockResolvedValue({ id: 'contribution-2' })
    const { user } = await renderLoaded()

    const periodCard = card(t('splitGroups.pot.transfersPeriod'))
    await user.click(
      within(periodCard).getByRole('button', {
        name: t('splitGroups.pot.recordContribution'),
      }),
    )

    // The viewer is user-1, linked to Me. The suggested transfer fills
    // Anna → Me; moving the receiver to Third must put the viewer back
    // on the paying side rather than leave her out of the pair.
    const dialog = await screen.findByRole('dialog')
    const selects = within(dialog).getAllByRole('combobox')
    await user.selectOptions(selects[1], THIRD)
    await user.click(within(dialog).getByRole('button', { name: t('common.save') }))

    await waitFor(() => expect(api.groups.settlements.create).toHaveBeenCalled())
    const [, payload] = api.groups.settlements.create.mock.calls.at(-1)!
    expect(payload.to_member_id).toBe(THIRD)
    expect(payload.from_member_id).toBe(ME)
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

  it('keeps the figures on screen while the next page loads', async () => {
    let releasePageTwo: (value: unknown) => void = () => {}
    const pageTwo = new Promise((resolve) => {
      releasePageTwo = resolve
    })
    api.groups.period.mockImplementation(async (_id: string, params: { page?: number }) => {
      if (params?.page === 2) {
        await pageTwo
        return { ...thisMonth, transactions: { ...thisMonth.transactions, page: 2 } }
      }
      return thisMonth
    })

    const { user } = await renderLoaded()
    await user.click(screen.getByRole('button', { name: t('common.next') }))

    await waitFor(() =>
      expect(
        api.groups.period.mock.calls.some(([, params]) => params?.page === 2),
      ).toBe(true),
    )
    // Still in flight: the transfers and the positions must not blank.
    expect(
      within(card(t('splitGroups.pot.transfersPeriod'))).getByText('$300.00'),
    ).toBeInTheDocument()
    expect(screen.queryByText(t('splitGroups.pot.nothingToMove'))).not.toBeInTheDocument()
    expect(positionRow('Anna')[6]).toBe('$2,000.00')

    releasePageTwo(null)
    expect(
      await screen.findByText(t('splitGroups.pot.pageOf', { page: 2, pages: 2 })),
    ).toBeInTheDocument()
  })

  it('says so when the period cannot be loaded', async () => {
    api.groups.period.mockRejectedValue(new Error('boom'))
    renderPage()

    expect(await screen.findByText(t('splitGroups.pot.loadFailed'))).toBeInTheDocument()
    // Never an empty pot: a failure and a quiet period must not read alike.
    expect(screen.queryByText(t('splitGroups.pot.noCosts'))).not.toBeInTheDocument()
    expect(screen.queryByText(t('splitGroups.pot.nothingToMove'))).not.toBeInTheDocument()
    expect(screen.queryByText(t('splitGroups.pot.noTransactions'))).not.toBeInTheDocument()

    api.groups.period.mockResolvedValue(thisMonth)
    const { default: userEvent } = await import('@testing-library/user-event')
    await userEvent.setup().click(screen.getByRole('button', { name: t('common.retry') }))

    expect(await screen.findByText(/May transfer/)).toBeInTheDocument()
    expect(
      within(card(t('splitGroups.pot.transfersPeriod'))).getByText('$300.00'),
    ).toBeInTheDocument()
  })

  it('never asks for a range that ends before it starts', async () => {
    // `keepInOrder` is covered in lib/group-period.test.ts; this is the
    // wiring: the custom range seeds itself in order and every request
    // the page makes stays that way.
    const { user } = await renderLoaded()

    await user.click(screen.getByRole('button', { name: t('splitGroups.pot.custom') }))
    expect(screen.getByText(t('splitGroups.pot.rangeStart'))).toBeInTheDocument()

    await waitFor(() => expect(api.groups.period.mock.calls.length).toBeGreaterThan(0))
    for (const [, params] of api.groups.period.mock.calls) {
      if (params?.start && params?.end) expect(params.start <= params.end).toBe(true)
    }
  })

  it('marks the owner as "you" when their own member is unlinked', async () => {
    api.groups.get.mockResolvedValue({
      ...group,
      members: [
        { ...group.members[0], linked_user_id: null, is_self: true },
        group.members[1],
      ],
    })
    await renderLoaded()

    const members = card(t('splitGroups.members'))
    const mine = within(members)
      .getAllByRole('listitem')
      .find((row) => within(row).queryByText('Me') !== null)!
    expect(within(mine).getByText(t('splitGroups.you'))).toBeInTheDocument()
    expect(within(members).queryByText(t('splitGroups.ownerBadge'))).not.toBeInTheDocument()
  })
})

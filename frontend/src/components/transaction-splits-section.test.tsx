/**
 * The payer selector on a transaction's group sharing.
 *
 * The payer is normally the owner of the account the transaction sits
 * on. For cash, for an account outside Securo and for a member with no
 * Securo user that is wrong, so the dialog lets it be named — and
 * putting it back on "from the account's owner" is how it is cleared.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { TransactionSplitsSection } from '@/components/transaction-splits-section'
import { renderWithProviders, t } from '@/test/utils'
import type { TransactionSplitsInput } from '@/types'

const api = vi.hoisted(() => ({
  groups: { list: vi.fn(), get: vi.fn(), create: vi.fn(), members: { create: vi.fn() } },
}))

vi.mock('@/lib/api', () => ({
  groups: api.groups,
}))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

const HOME = 'group-home'
const ME = 'member-me'
const HER = 'member-her'

function renderSection(value: TransactionSplitsInput | null) {
  const onChange = vi.fn()
  renderWithProviders(
    <TransactionSplitsSection
      amount={400}
      currency="USD"
      value={value}
      onChange={onChange}
    />,
  )
  return onChange
}

/** The payload the section last pushed up. */
function lastPayload(onChange: ReturnType<typeof vi.fn>): TransactionSplitsInput | null {
  const calls = onChange.mock.calls
  return calls.length ? (calls[calls.length - 1][0] as TransactionSplitsInput | null) : null
}

describe('naming who paid', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.groups.list.mockResolvedValue([
      {
        id: HOME,
        name: 'Home',
        kind: 'household',
        is_archived: false,
        is_owner: true,
        members: [
          { id: ME, name: 'Me', is_self: true },
          { id: HER, name: 'Partner', is_self: false },
        ],
      },
    ])
    api.groups.get.mockResolvedValue({
      id: HOME,
      name: 'Home',
      kind: 'household',
      members: [
        { id: ME, name: 'Me', is_self: true },
        { id: HER, name: 'Partner', is_self: false },
      ],
    })
  })

  const shared: TransactionSplitsInput = {
    share_type: 'equal',
    splits: [{ group_member_id: ME }, { group_member_id: HER }],
  }

  /** Put the cost in the pot the way a user does: tick both members. */
  async function shareBetweenBoth(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByLabelText(/^Me/))
    await user.click(screen.getByLabelText('Partner'))
  }

  it('leaves the payer to the account by default', async () => {
    const user = userEvent.setup()
    const onChange = renderSection(shared)

    const select = (await screen.findByLabelText(
      t('splitGroups.payer'),
    )) as HTMLSelectElement
    expect(select.value).toBe('')
    expect(
      screen.getByRole('option', { name: t('splitGroups.payerFromAccount') }),
    ).toBeInTheDocument()

    await shareBetweenBoth(user)
    await waitFor(() => expect(lastPayload(onChange)?.payer_group_member_id).toBeNull())
  })

  it('sends the member the user names as the payer', async () => {
    const user = userEvent.setup()
    const onChange = renderSection(shared)

    const select = (await screen.findByLabelText(
      t('splitGroups.payer'),
    )) as HTMLSelectElement
    await shareBetweenBoth(user)
    await user.selectOptions(select, HER)

    await waitFor(() => expect(lastPayload(onChange)?.payer_group_member_id).toBe(HER))
    // The shares are untouched by naming a payer.
    expect(
      lastPayload(onChange)
        ?.splits.map((s) => s.group_member_id)
        .sort(),
    ).toEqual([HER, ME].sort())
  })

  it('opens on the payer that was stored and clears it back to the account', async () => {
    const user = userEvent.setup()
    const onChange = renderSection({ ...shared, payer_group_member_id: HER })

    const select = (await screen.findByLabelText(
      t('splitGroups.payer'),
    )) as HTMLSelectElement
    expect(select.value).toBe(HER)

    await shareBetweenBoth(user)
    await waitFor(() => expect(lastPayload(onChange)?.payer_group_member_id).toBe(HER))

    await user.selectOptions(select, '')
    await waitFor(() => expect(lastPayload(onChange)?.payer_group_member_id).toBeNull())
  })
})

/**
 * The rule editor's two group actions.
 *
 * Without these the feature is unreachable: a household can only have the
 * bank's groceries shared automatically, or her transfer marked as a
 * contribution, if the editor can write the action. The test asserts on
 * what the user does — pick the action, pick the group, tick the members —
 * and on the payload that leaves the dialog.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { RuleDialog } from '@/components/rule-dialog'
import { renderWithProviders, t } from '@/test/utils'

const api = vi.hoisted(() => ({
  groups: { list: vi.fn(), get: vi.fn() },
  rules: { preview: vi.fn() },
}))

vi.mock('@/lib/api', () => ({
  groups: api.groups,
  rules: api.rules,
}))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))

const HOME = 'group-home'
const ME = 'member-me'
const HER = 'member-her'

function renderDialog(onSave = vi.fn()) {
  const result = renderWithProviders(
    <RuleDialog
      open
      onClose={vi.fn()}
      rule={null}
      categories={[]}
      categoryGroups={[]}
      accounts={[]}
      payees={[]}
      onSave={onSave}
      loading={false}
    />,
  )
  return { ...result, onSave }
}

describe('the rule editor writes the two group actions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.groups.list.mockResolvedValue([
      { id: HOME, name: 'Home', kind: 'household', is_archived: false, is_owner: true },
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
    api.rules.preview.mockResolvedValue({
      matched: 0,
      will_change: 0,
      will_apply: true,
      will_share: 0,
      will_mark_contribution: 0,
      sample: [],
      offset: 0,
    })
  })

  it('saves a rule that shares every match equally in a group', async () => {
    const { user, onSave } = renderDialog()

    await user.type(screen.getByPlaceholderText('Ex: Uber'), 'Groceries')
    await user.type(screen.getByPlaceholderText(t('rules.valuePlaceholder')), 'SUPERMARKET')
    await user.selectOptions(
      screen.getByDisplayValue(t('rules.setCategory')),
      'share_in_group',
    )

    await user.selectOptions(await screen.findByLabelText(t('splitGroups.group')), HOME)
    await user.click(await screen.findByLabelText('Me'))
    await user.click(screen.getByLabelText('Partner'))

    await user.click(screen.getByRole('button', { name: t('common.save') }))

    expect(onSave).toHaveBeenCalledTimes(1)
    expect(onSave.mock.calls[0][0].actions).toEqual([
      {
        op: 'share_in_group',
        value: {
          group_id: HOME,
          share_type: 'equal',
          splits: [{ group_member_id: ME }, { group_member_id: HER }],
        },
      },
    ])
  })

  it('saves a 60/40 distribution and says when the percentages do not add up', async () => {
    const { user, onSave } = renderDialog()

    await user.type(screen.getByPlaceholderText('Ex: Uber'), 'Rent')
    await user.type(screen.getByPlaceholderText(t('rules.valuePlaceholder')), 'RENT')
    await user.selectOptions(
      screen.getByDisplayValue(t('rules.setCategory')),
      'share_in_group',
    )
    await user.selectOptions(await screen.findByLabelText(t('splitGroups.group')), HOME)
    await user.selectOptions(screen.getByLabelText(t('splitGroups.shareType')), 'percent')
    await user.click(await screen.findByLabelText('Me'))
    await user.click(screen.getByLabelText('Partner'))

    await user.type(screen.getByLabelText('Me %'), '60')
    expect(screen.getByText(t('splitGroups.percentSum', { total: '60.00' }))).toBeInTheDocument()
    await user.type(screen.getByLabelText('Partner %'), '40')
    expect(screen.getByText(t('splitGroups.percentSum', { total: '100.00' }))).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('common.save') }))
    expect(onSave.mock.calls[0][0].actions[0].value).toEqual({
      group_id: HOME,
      share_type: 'percent',
      splits: [
        { group_member_id: ME, share_pct: 60 },
        { group_member_id: HER, share_pct: 40 },
      ],
    })
  })

  it('saves a rule that marks a match as a contribution from the other member', async () => {
    const { user, onSave } = renderDialog()

    await user.type(screen.getByPlaceholderText('Ex: Uber'), 'Her transfer')
    await user.type(
      screen.getByPlaceholderText(t('rules.valuePlaceholder')),
      'MONTHLY TRANSFER',
    )
    await user.selectOptions(
      screen.getByDisplayValue(t('rules.setCategory')),
      'mark_as_contribution',
    )

    await user.selectOptions(await screen.findByLabelText(t('splitGroups.group')), HOME)
    await user.selectOptions(
      await screen.findByLabelText(t('rules.contributionMember')),
      HER,
    )
    expect(screen.getByText(t('rules.contributionMemberHint'))).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('common.save') }))
    expect(onSave.mock.calls[0][0].actions).toEqual([
      { op: 'mark_as_contribution', value: { group_id: HOME, member_id: HER } },
    ])
  })

  it('keeps a half-written group action out of the preview request', async () => {
    const { user } = renderDialog()

    await user.type(screen.getByPlaceholderText(t('rules.valuePlaceholder')), 'SUPERMARKET')
    await user.selectOptions(
      screen.getByDisplayValue(t('rules.setCategory')),
      'share_in_group',
    )
    await user.click(screen.getByRole('button', { name: new RegExp(t('rules.preview')) }))

    await waitFor(() => expect(api.rules.preview).toHaveBeenCalled())
    expect(api.rules.preview.mock.calls[0][0].actions).toEqual([])
  })

  it('says how many matches a sharing rule would share, not just what changes', async () => {
    api.rules.preview.mockResolvedValue({
      matched: 12,
      will_change: 0,
      will_apply: true,
      will_share: 12,
      will_mark_contribution: 0,
      sample: [],
      offset: 0,
    })
    const { user } = renderDialog()

    await user.type(screen.getByPlaceholderText(t('rules.valuePlaceholder')), 'SUPERMARKET')
    await user.selectOptions(
      screen.getByDisplayValue(t('rules.setCategory')),
      'share_in_group',
    )
    await user.selectOptions(await screen.findByLabelText(t('splitGroups.group')), HOME)
    await user.click(await screen.findByLabelText('Me'))
    await user.click(screen.getByLabelText('Partner'))
    await user.click(screen.getByRole('button', { name: new RegExp(t('rules.preview')) }))

    expect(
      await screen.findByText(new RegExp(t('rules.previewShared', { shared: 12 }))),
    ).toBeInTheDocument()
  })

  it('does not ask for the groups until a group action is chosen', async () => {
    const { user } = renderDialog()

    expect(api.groups.list).not.toHaveBeenCalled()
    await user.selectOptions(
      screen.getByDisplayValue(t('rules.setCategory')),
      'share_in_group',
    )
    await waitFor(() => expect(api.groups.list).toHaveBeenCalled())
  })
})

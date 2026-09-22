/**
 * The viewing filter: a person, or a collection, or neither.
 *
 * Picking a person has to reach the server as the *person*, because the
 * figure it asks for is their consumption — their own costs plus their
 * shares of shared costs. A list of their account ids would read as a
 * collection and lose the shares, which is exactly the confusion this
 * control has to make impossible.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { CollectionSelector } from '@/components/collection-selector'
import { CollectionFilterProvider } from '@/contexts/collection-filter-provider'
import { useCollectionFilter } from '@/contexts/collection-filter-context'
import { renderWithProviders, t } from '@/test/utils'

const api = vi.hoisted(() => ({
  collections: { list: vi.fn() },
  workspaces: { listMembers: vi.fn() },
  accounts: { list: vi.fn() },
}))

vi.mock('@/lib/api', () => ({
  collections: api.collections,
  workspaces: api.workspaces,
  accounts: api.accounts,
}))

const WORKSPACE = 'ws-1'
const HIM = 'user-him'
const HER = 'user-her'

vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({ current: { id: WORKSPACE, name: 'Home' } }),
}))

function Probe() {
  const { activeUserId, activeCollectionId, activeAccountIds } = useCollectionFilter()
  return (
    <div>
      <span data-testid="user">{activeUserId ?? 'none'}</span>
      <span data-testid="collection">{activeCollectionId ?? 'none'}</span>
      <span data-testid="accounts">{(activeAccountIds ?? ['all']).join(',')}</span>
    </div>
  )
}

function renderSelector() {
  return renderWithProviders(
    <CollectionFilterProvider>
      <CollectionSelector variant="header" />
      <Probe />
    </CollectionFilterProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  api.collections.list.mockResolvedValue([
    {
      id: 'coll-1',
      user_id: HIM,
      name: 'Her cards',
      icon: 'layers',
      color: '#123456',
      position: 0,
      account_ids: ['acct-her'],
      account_count: 1,
      wallet_ids: [],
      wallet_count: 0,
    },
  ])
  api.workspaces.listMembers.mockResolvedValue([
    {
      id: 'm1',
      user_id: HIM,
      email: 'him@example.com',
      display_name: 'Lasse',
      role: 'owner',
      joined_at: '2026-01-01',
    },
    {
      id: 'm2',
      user_id: HER,
      email: 'her@example.com',
      display_name: 'Anna',
      role: 'member',
      joined_at: '2026-01-01',
    },
  ])
  api.accounts.list.mockResolvedValue([
    { id: 'acct-his', user_id: HIM, name: 'His' },
    { id: 'acct-her', user_id: HER, name: 'Hers' },
    { id: 'acct-her-2', user_id: HER, name: 'Her savings' },
  ])
})

describe('the viewing filter', () => {
  it('offers everyone in the workspace beside the collections', async () => {
    const { user } = renderSelector()

    await user.click(await screen.findByRole('button', { name: /all accounts/i }))

    expect(await screen.findByText(t('collections.people'))).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /Anna/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /Lasse/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /Her cards/ })).toBeInTheDocument()
  })

  it('resolves a person to the accounts they own, and names them', async () => {
    const { user } = renderSelector()

    await user.click(await screen.findByRole('button', { name: /all accounts/i }))
    await user.click(await screen.findByRole('menuitem', { name: /Anna/ }))

    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(HER))
    expect(screen.getByTestId('accounts')).toHaveTextContent('acct-her,acct-her-2')
    expect(screen.getByRole('button', { name: /Anna/ })).toBeInTheDocument()
  })

  it('never has a person and a collection on at once', async () => {
    const { user } = renderSelector()

    await user.click(await screen.findByRole('button', { name: /all accounts/i }))
    await user.click(await screen.findByRole('menuitem', { name: /Anna/ }))
    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(HER))

    await user.click(screen.getByRole('button', { name: /Anna/ }))
    await user.click(await screen.findByRole('menuitem', { name: /Her cards/ }))

    await waitFor(() => expect(screen.getByTestId('collection')).toHaveTextContent('coll-1'))
    expect(screen.getByTestId('user')).toHaveTextContent('none')
  })

  it('clears back to the whole workspace', async () => {
    const { user } = renderSelector()

    await user.click(await screen.findByRole('button', { name: /all accounts/i }))
    await user.click(await screen.findByRole('menuitem', { name: /Anna/ }))
    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(HER))

    await user.click(screen.getByRole('button', { name: t('collections.clearFilter') }))

    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent('none'))
    expect(screen.getByTestId('accounts')).toHaveTextContent('all')
  })

  it('resolves a person who owns no account here to an empty set', async () => {
    // Not "no filter": she owns nothing in this workspace and still
    // carries her share of what he paid, which is what the consumption
    // pages go on to ask the server for.
    api.accounts.list.mockResolvedValue([{ id: 'acct-his', user_id: HIM, name: 'His' }])
    const { user } = renderSelector()

    await user.click(await screen.findByRole('button', { name: /all accounts/i }))
    await user.click(await screen.findByRole('menuitem', { name: /Anna/ }))

    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(HER))
    expect(screen.getByTestId('accounts')).toHaveTextContent('')
    expect(screen.getByTestId('accounts')).not.toHaveTextContent('all')
  })

  it('stays out of the way for one person with no collections', async () => {
    api.collections.list.mockResolvedValue([])
    api.workspaces.listMembers.mockResolvedValue([
      {
        id: 'm1',
        user_id: HIM,
        email: 'him@example.com',
        display_name: 'Lasse',
        role: 'owner',
        joined_at: '2026-01-01',
      },
    ])
    renderSelector()

    await waitFor(() => expect(screen.getByTestId('accounts')).toHaveTextContent('all'))
    expect(screen.queryByText(t('collections.viewing'))).not.toBeInTheDocument()
  })
})

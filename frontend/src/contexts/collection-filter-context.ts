import { createContext, useContext } from 'react'
import type { Collection, WorkspaceMember } from '@/types'

/**
 * The app's "viewing" filter. Two alternatives, never both:
 *
 *  - a **collection**, which is cash flow over its accounts with no
 *    share adjustment, and
 *  - a **user**, which resolves to the accounts that person owns and
 *    shows their consumption — their own costs plus their shares of
 *    shared costs, whoever paid.
 *
 * Pages that show cash read `activeAccountIds` and need to know nothing
 * about which of the two is on. Pages that show consumption (dashboard,
 * reports, budgets) send `activeUserId` to the server instead, which
 * resolves the accounts itself and keeps the shares with the right
 * person.
 */
export type CollectionFilterValue = {
  collections: Collection[]
  activeCollectionId: string | null
  activeCollection: Collection | null
  setActiveCollectionId: (id: string | null) => void
  /** Everyone in the current workspace, for the people section. */
  users: WorkspaceMember[]
  activeUserId: string | null
  activeUser: WorkspaceMember | null
  setActiveUserId: (id: string | null) => void
  // null = all accounts (no filter); otherwise the active collection's
  // account ids, or the accounts the filtered user owns.
  activeAccountIds: string[] | null
  // null = no filter; otherwise the active collection's wallet (asset_group) ids.
  // A user filter never narrows wallets: an asset has no owner.
  activeWalletIds: string[] | null
}

export const CollectionFilterContext = createContext<CollectionFilterValue | null>(null)

export function useCollectionFilter(): CollectionFilterValue {
  const ctx = useContext(CollectionFilterContext)
  if (!ctx) {
    throw new Error('useCollectionFilter must be used within a CollectionFilterProvider')
  }
  return ctx
}

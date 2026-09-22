import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  accounts as accountsApi,
  collections as collectionsApi,
  workspaces as workspacesApi,
} from '@/lib/api'
import { useWorkspace } from '@/contexts/workspace-context'
import { CollectionFilterContext, type CollectionFilterValue } from '@/contexts/collection-filter-context'

const STORAGE_PREFIX = 'securo.activeCollection.'
const USER_STORAGE_PREFIX = 'securo.activeUser.'

export function CollectionFilterProvider({ children }: { children: ReactNode }) {
  const { current } = useWorkspace()
  const wsId = current?.id ?? ''

  const { data } = useQuery({
    queryKey: ['collections'],
    queryFn: collectionsApi.list,
  })
  const collections = useMemo(() => data ?? [], [data])

  // The people the filter can be pointed at, and the accounts it
  // resolves to. Both are workspace-wide reads the app already makes
  // elsewhere, so they come out of the query cache most of the time.
  const { data: memberData } = useQuery({
    queryKey: ['workspace-members', wsId || undefined],
    queryFn: () => workspacesApi.listMembers(wsId),
    enabled: !!wsId,
  })
  const users = useMemo(() => memberData ?? [], [memberData])

  const { data: accountData } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => accountsApi.list(),
  })
  const allAccounts = useMemo(() => accountData ?? [], [accountData])

  const [activeCollectionId, setCollectionId] = useState<string | null>(null)
  const [activeUserId, setUserId] = useState<string | null>(null)

  // The active selection is persisted per workspace — switching workspaces
  // restores that workspace's last-used filter (or "all").
  const [loadedWsId, setLoadedWsId] = useState<string | null>(null)
  if (wsId && wsId !== loadedWsId) {
    setLoadedWsId(wsId)
    setCollectionId(localStorage.getItem(STORAGE_PREFIX + wsId) || null)
    setUserId(localStorage.getItem(USER_STORAGE_PREFIX + wsId) || null)
  }

  const remember = (prefix: string, id: string | null) => {
    if (!wsId) return
    if (id) localStorage.setItem(prefix + wsId, id)
    else localStorage.removeItem(prefix + wsId)
  }

  // The two are alternatives: picking one clears the other, so a page
  // never has to decide what a collection of her accounts *and* the
  // filter on him would mean.
  const setActiveCollectionId = (id: string | null) => {
    setCollectionId(id)
    remember(STORAGE_PREFIX, id)
    if (id) {
      setUserId(null)
      remember(USER_STORAGE_PREFIX, null)
    }
  }

  const setActiveUserId = (id: string | null) => {
    setUserId(id)
    remember(USER_STORAGE_PREFIX, id)
    if (id) {
      setCollectionId(null)
      remember(STORAGE_PREFIX, null)
    }
  }

  const activeCollection = useMemo(
    () => collections.find((c) => c.id === activeCollectionId) ?? null,
    [collections, activeCollectionId],
  )
  const activeUser = useMemo(
    () => users.find((u) => u.user_id === activeUserId) ?? null,
    [users, activeUserId],
  )

  // If the active collection was deleted elsewhere, fall back to "all".
  useEffect(() => {
    if (activeCollectionId && collections.length > 0 && !activeCollection) {
      setActiveCollectionId(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCollectionId, collections, activeCollection])

  // Same for a person who has left the workspace.
  useEffect(() => {
    if (activeUserId && users.length > 0 && !activeUser) {
      setActiveUserId(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeUserId, users, activeUser])

  const userAccountIds = useMemo(
    () =>
      activeUserId ? allAccounts.filter((a) => a.user_id === activeUserId).map((a) => a.id) : null,
    [allAccounts, activeUserId],
  )

  const value: CollectionFilterValue = {
    collections,
    activeCollectionId,
    activeCollection,
    setActiveCollectionId,
    users,
    activeUserId,
    activeUser,
    setActiveUserId,
    activeAccountIds: activeCollection
      ? activeCollection.account_ids
      : userAccountIds,
    activeWalletIds: activeCollection ? activeCollection.wallet_ids : null,
  }

  return (
    <CollectionFilterContext.Provider value={value}>{children}</CollectionFilterContext.Provider>
  )
}

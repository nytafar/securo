import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useCollectionFilter } from '@/contexts/collection-filter-context'
import { Check, ChevronsUpDown, Layers, Settings2, User, X } from 'lucide-react'

/**
 * Global "viewing" selector (issue #105, extended for the user filter).
 * Filters the app either to the accounts (and wallets) of a collection,
 * or to one person.
 *
 * The two are alternatives, and they mean different things: a collection
 * is cash flow over its accounts, while a person is their consumption —
 * their own costs plus their shares of shared costs, whoever paid. The
 * menu keeps them in separate sections for that reason.
 *
 * Two placements:
 *  - `sidebar`  legacy spot in the left nav.
 *  - `header`   a sticky bar at the top of the content area, so the active
 *               filter is visible right above the data it scopes (and reads
 *               as a peer of the workspace switcher rather than a list filter).
 *
 * Hidden entirely until there is something to pick: no collections and
 * nobody to share the workspace with means no clutter.
 */
export function CollectionSelector({ variant = 'sidebar' }: { variant?: 'sidebar' | 'header' }) {
  const { t } = useTranslation()
  const nav = useNavigate()
  const {
    collections,
    activeCollection,
    setActiveCollectionId,
    users,
    activeUser,
    setActiveUserId,
  } = useCollectionFilter()

  // One person alone in a workspace is not a filter worth offering.
  const people = users.length > 1 ? users : []
  if (collections.length === 0 && people.length === 0) return null

  const personName = (member: { display_name: string | null; email: string }) =>
    member.display_name || member.email

  const activeLabel = activeCollection
    ? activeCollection.name
    : activeUser
      ? personName(activeUser)
      : t('collections.allAccounts')

  const clear = () => {
    setActiveCollectionId(null)
    setActiveUserId(null)
  }

  const menu = (
    <DropdownMenuContent align="start" className="w-60">
      <DropdownMenuItem onClick={clear} className="flex items-center gap-2">
        <Layers size={14} className="text-muted-foreground" />
        <span className="flex-1">{t('collections.allAccounts')}</span>
        {!activeCollection && !activeUser && <Check size={14} className="text-primary" />}
      </DropdownMenuItem>
      {people.length > 0 && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuLabel className="text-[10.5px] font-medium uppercase tracking-[0.1em] text-muted-foreground/70">
            {t('collections.people')}
          </DropdownMenuLabel>
          {people.map((member) => (
            <DropdownMenuItem
              key={member.user_id}
              onClick={() => setActiveUserId(member.user_id)}
              className="flex items-center gap-2"
            >
              <User size={14} className="shrink-0 text-muted-foreground" />
              <span className="flex-1 truncate">{personName(member)}</span>
              {activeUser?.user_id === member.user_id && (
                <Check size={14} className="text-primary" />
              )}
            </DropdownMenuItem>
          ))}
        </>
      )}
      {collections.length > 0 && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuLabel className="text-[10.5px] font-medium uppercase tracking-[0.1em] text-muted-foreground/70">
            {t('collections.title')}
          </DropdownMenuLabel>
          {collections.map((c) => (
            <DropdownMenuItem key={c.id} onClick={() => setActiveCollectionId(c.id)} className="flex items-center gap-2">
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: c.color }} />
              <span className="flex-1 truncate">{c.name}</span>
              <span className="text-[10.5px] tabular-nums text-muted-foreground/70">
                {c.account_count > 0 && `${c.account_count}a`}
                {c.account_count > 0 && c.wallet_count > 0 && ' · '}
                {c.wallet_count > 0 && `${c.wallet_count}w`}
              </span>
              {activeCollection?.id === c.id && <Check size={14} className="text-primary" />}
            </DropdownMenuItem>
          ))}
        </>
      )}
      <DropdownMenuSeparator />
      <DropdownMenuItem onClick={() => nav('/collections')} className="flex items-center gap-2 text-muted-foreground">
        <Settings2 size={14} />
        {t('collections.manage')}
      </DropdownMenuItem>
    </DropdownMenuContent>
  )

  // ── Sidebar placement (legacy) ──────────────────────────────────────────
  if (variant === 'sidebar') {
    return (
      <div className="px-3 pt-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex items-center gap-2 w-full rounded-lg border border-sidebar-border/60 bg-sidebar-accent/30 px-2.5 py-1.5 text-left hover:bg-sidebar-accent/50 transition-colors">
              {activeCollection ? (
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: activeCollection.color }} />
              ) : activeUser ? (
                <User size={13} className="shrink-0 text-sidebar-muted" />
              ) : (
                <Layers size={13} className="shrink-0 text-sidebar-muted" />
              )}
              <span className="flex-1 min-w-0 truncate text-xs font-medium text-sidebar-foreground">
                {activeLabel}
              </span>
              <ChevronsUpDown size={13} className="shrink-0 text-sidebar-muted" />
            </button>
          </DropdownMenuTrigger>
          {menu}
        </DropdownMenu>
      </div>
    )
  }

  // ── Header placement (sticky content-area bar) ──────────────────────────
  // When a filter is active the trigger is tinted and gains a one-click
  // clear, so the filtered state is impossible to miss. Idle ("All
  // accounts") it stays quiet.
  return (
    <div className="sticky top-0 z-30 -mx-6 mb-6 border-b border-border/60 bg-background/80 px-6 backdrop-blur supports-[backdrop-filter]:bg-background/65 lg:-mx-6">
      <div className="flex h-12 max-w-7xl items-center gap-2">
        <span className="text-[11px] font-medium uppercase tracking-[0.1em] text-muted-foreground/70">
          {t('collections.viewing')}
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              className="group inline-flex items-center gap-2 rounded-full border border-border/60 px-3 py-1.5 text-[13px] transition-colors hover:bg-muted/50"
              style={
                activeCollection
                  ? { borderColor: `${activeCollection.color}55`, backgroundColor: `${activeCollection.color}14` }
                  : undefined
              }
            >
              {activeCollection ? (
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: activeCollection.color }} />
              ) : activeUser ? (
                <User size={14} className="shrink-0 text-muted-foreground" />
              ) : (
                <Layers size={14} className="shrink-0 text-muted-foreground" />
              )}
              <span
                className={
                  activeCollection || activeUser
                    ? 'font-medium text-foreground'
                    : 'text-muted-foreground group-hover:text-foreground'
                }
              >
                {activeLabel}
              </span>
              <ChevronsUpDown size={13} className="shrink-0 text-muted-foreground/70" />
            </button>
          </DropdownMenuTrigger>
          {menu}
        </DropdownMenu>
        {(activeCollection || activeUser) && (
          <button
            onClick={clear}
            className="inline-flex items-center gap-1 rounded-full px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            aria-label={t('collections.clearFilter')}
          >
            <X size={13} />
            {t('collections.clearFilter')}
          </button>
        )}
        {activeUser && (
          <span className="hidden text-[11px] text-muted-foreground/80 sm:inline">
            {t('collections.consumptionHint')}
          </span>
        )}
      </div>
    </div>
  )
}

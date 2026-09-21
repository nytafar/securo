/**
 * The group page as a common pot.
 *
 * Every figure here comes from `GET /api/groups/{id}/period`, which
 * aggregates over the whole period on the server. Nothing is added up
 * from the transaction rows the page happens to have loaded: that is
 * what made the old totals, trend and category breakdown wrong once a
 * group had more than the twenty transactions they were computed from.
 */
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useDisplayLocale, useDateLocale } from '@/hooks/use-display-locale'
import { getAccountName, sortAccountsByDisplayName } from '@/lib/account-utils'
import { useNavigate, useParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  Link2,
  Receipt,
  Trash2,
  UserPlus,
} from 'lucide-react'

import {
  groups as groupsApi,
  accounts as accountsApi,
  transactions as transactionsApi,
  type GroupMemberPayload,
  type GroupSettlementPayload,
} from '@/lib/api'

/** Marking a real transaction, as opposed to recording a contribution
 *  by hand. Wrapped so the one mutation can tell the two apart. */
type MarkContributionPayload = {
  transaction: { transaction_id: string; member_id: string; notes?: string | null }
}
import { localDateString } from '@/lib/date-utils'
import {
  catchUpMonths,
  exclusiveEnd,
  inclusiveEnd,
  keepInOrder,
  monthLabel,
  monthOf,
  monthRange,
  presetRange,
  shiftMonth,
  PERIOD_PRESETS,
  type CalendarMonth,
  type PeriodPreset,
  type PeriodRange,
} from '@/lib/group-period'
import { MemberForm } from '@/components/member-form'
import { useAuth } from '@/contexts/auth-context'
import { useWorkspace } from '@/contexts/workspace-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { DatePickerInput } from '@/components/ui/date-picker-input'
import { PageHeader } from '@/components/page-header'
import type {
  GroupMember,
  GroupMemberShare,
  GroupSuggestedTransfer,
  Transaction,
} from '@/types'
import { formatCurrency } from '@/lib/format'

/** The transaction list is paged on the server; the figures above it are not. */
const PAGE_SIZE = 25

/** What the period picker is set to. A month is its own mode: the month
 *  picker holds which one, and the year, all time and custom ranges sit
 *  beside it. */
type RangeMode = 'month' | 'thisYear' | 'allTime' | 'custom'

function SectionCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
      {children}
    </div>
  )
}

function SectionHeader({
  title,
  description,
  action,
}: {
  title: string
  description?: string
  action?: React.ReactNode
}) {
  return (
    <div className="px-4 sm:px-5 py-4 border-b border-border flex flex-wrap items-center justify-between gap-2">
      <div className="min-w-0">
        <p className="text-sm font-semibold text-foreground">{title}</p>
        {description && (
          <p className="text-xs text-muted-foreground mt-0.5">{description}</p>
        )}
      </div>
      {action}
    </div>
  )
}

function shareOf(shares: GroupMemberShare[], memberId: string): number {
  return Number(shares.find((s) => s.member_id === memberId)?.amount ?? 0)
}

/** A suggested transfer reads in the direction money moves, so a backlog
 *  in a member's favour shows as the others paying them. */
function TransferList({
  transfers,
  loaded,
  nameOf,
  locale,
  emptyLabel,
  actionLabel,
  onPick,
}: {
  transfers: GroupSuggestedTransfer[]
  /** False until a period has actually been read: an empty list is only
   *  "nothing to move" once we know the period is empty. */
  loaded: boolean
  nameOf: (memberId: string | null) => string
  locale: string
  emptyLabel: string
  actionLabel?: string
  onPick?: (transfer: GroupSuggestedTransfer) => void
}) {
  if (!loaded) {
    return (
      <div className="p-4 space-y-2">
        <Skeleton className="h-8 w-full" />
      </div>
    )
  }
  if (transfers.length === 0) {
    return (
      <div className="text-center py-6 text-muted-foreground text-sm">{emptyLabel}</div>
    )
  }
  return (
    <ul className="divide-y divide-border">
      {transfers.map((transfer, index) => (
        <li
          key={`${transfer.from_member_id}-${transfer.to_member_id}-${transfer.currency}-${index}`}
          className="flex items-center justify-between gap-3 px-4 py-3"
        >
          <div className="text-sm flex items-center gap-1.5 min-w-0">
            <span className="font-medium truncate">{nameOf(transfer.from_member_id)}</span>
            <ArrowRight size={12} className="text-muted-foreground shrink-0" />
            <span className="font-medium truncate">{nameOf(transfer.to_member_id)}</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold tabular-nums whitespace-nowrap">
              {formatCurrency(Number(transfer.amount), transfer.currency, locale)}
            </span>
            {actionLabel && onPick && (
              <Button variant="outline" size="sm" onClick={() => onPick(transfer)}>
                {actionLabel}
              </Button>
            )}
          </div>
        </li>
      ))}
    </ul>
  )
}

export default function GroupDetailPage() {
  const { id } = useParams<{ id: string }>()
  const groupId = id ?? ''
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const { canWrite } = useWorkspace()

  const { data: group, isLoading: loadingGroup } = useQuery({
    queryKey: ['groups', groupId],
    queryFn: () => groupsApi.get(groupId),
    enabled: !!groupId,
  })

  // Linked members get a read-only view of the group.
  const isOwner = group?.is_owner ?? false
  // A household is read period by period and calls its transfers
  // contributions; every other kind opens on the running position and
  // calls them settlements.
  const isHousehold = group?.kind === 'household'

  // ── The period ───────────────────────────────────────────────
  // A month is a mode of its own, so the month picker holds the choice
  // and "This month" and "Last month" are two of its positions rather
  // than a second way of saying the same thing.
  const [mode, setMode] = useState<RangeMode | null>(null)
  const [month, setMonth] = useState<CalendarMonth>(() => monthOf())
  const [customStart, setCustomStart] = useState('')
  // What the picker shows: the last day inside the range, not the
  // exclusive `end` the API takes.
  const [customEnd, setCustomEnd] = useState('')
  const [page, setPage] = useState(1)

  const activeMode: RangeMode = mode ?? (isHousehold ? 'month' : 'allTime')
  const range: PeriodRange = useMemo(() => {
    if (activeMode === 'month') return monthRange(month)
    if (activeMode === 'custom') {
      return { start: customStart || null, end: exclusiveEnd(customEnd) }
    }
    return presetRange(activeMode)
  }, [activeMode, month, customStart, customEnd])

  // Every way of changing the range goes through here, so page 2 of the
  // old period never survives into the new one.
  const changeRange = (change: () => void) => {
    change()
    setPage(1)
  }

  const {
    data: period,
    isError: periodFailed,
    refetch: refetchPeriod,
  } = useQuery({
    queryKey: ['groups', groupId, 'period', range.start, range.end, page],
    queryFn: () =>
      groupsApi.period(groupId, {
        start: range.start,
        end: range.end,
        page,
        page_size: PAGE_SIZE,
      }),
    // Waits for the group, so the first request already uses the range
    // the group's kind opens on.
    enabled: !!groupId && !!group,
    // Keep the figures on screen while the next period or page loads.
    // Without this, paging the transaction list blanks the transfers and
    // the positions, which reads as "nothing to move".
    placeholderData: keepPreviousData,
  })

  // A failed request must never look like a quiet period, so the
  // sections are replaced rather than left showing their empty states.
  const showFigures = !!period && !periodFailed

  const openCustomRange = () => {
    // Seed the custom pickers from whatever is on screen, so switching
    // to Custom never blanks the page.
    if (!customStart && range.start) setCustomStart(range.start)
    if (!customEnd && range.end) setCustomEnd(inclusiveEnd(range.end))
    setMode('custom')
  }

  // One of the five buttons. The two month ones move the month picker.
  const pickPreset = (preset: PeriodPreset) =>
    changeRange(() => {
      if (preset === 'thisMonth' || preset === 'lastMonth') {
        setMode('month')
        setMonth(preset === 'thisMonth' ? monthOf() : shiftMonth(monthOf(), -1))
      } else if (preset === 'custom') {
        openCustomRange()
      } else {
        setMode(preset)
      }
    })

  const pickMonth = (value: CalendarMonth) =>
    changeRange(() => {
      setMode('month')
      setMonth(value)
    })

  /** True when a button stands for what is on screen. */
  const presetIsActive = (preset: PeriodPreset) => {
    if (preset === 'thisMonth') return activeMode === 'month' && month === monthOf()
    if (preset === 'lastMonth') {
      return activeMode === 'month' && month === shiftMonth(monthOf(), -1)
    }
    return activeMode === preset
  }

  // The last two years, newest first, plus wherever the arrows have
  // taken the reader.
  const monthOptions = useMemo(() => {
    const options = new Set<CalendarMonth>([month])
    for (let back = 0; back < 24; back++) options.add(shiftMonth(monthOf(), -back))
    return [...options].sort().reverse()
  }, [month])

  // The range stays in order whatever the user picks, so `start > end`
  // and the 400 it earns never happen.
  const pickRange = (value: string, moved: 'start' | 'end') =>
    changeRange(() => {
      const ordered = keepInOrder(
        moved === 'start' ? value : customStart,
        moved === 'end' ? value : customEnd,
        moved,
      )
      setCustomStart(ordered.start)
      setCustomEnd(ordered.lastDay)
    })

  const periodMembers = useMemo(() => period?.members ?? [], [period])

  // ── Names and lookups ────────────────────────────────────────
  // Figures are keyed by the members the period response lists; the
  // group payload is only for managing them.
  const nameOf = useMemo(() => {
    const names = new Map<string, string>()
    for (const m of group?.members ?? []) names.set(m.id, m.name)
    for (const m of periodMembers) names.set(m.id, m.name)
    return (memberId: string | null) => (memberId && names.get(memberId)) || '—'
  }, [group?.members, periodMembers])

  const ownerMemberId = period?.owner_member_id ?? null
  // The member that stands for whoever is looking: their linked member,
  // or — for the group's owner, whose own member is often unlinked — the
  // owner's member the period response resolved. Never the rewritten
  // `is_self`, which depends on the viewer.
  const viewerMemberId = useMemo(() => {
    const linked = group?.members.find((m) => user && m.linked_user_id === user.id)
    if (linked) return linked.id
    return isOwner ? ownerMemberId : null
  }, [group?.members, user, isOwner, ownerMemberId])

  // One block per currency: positions are never added across currencies.
  const currencies = useMemo(() => {
    const seen: string[] = []
    for (const position of period?.positions ?? []) {
      if (!seen.includes(position.currency)) seen.push(position.currency)
    }
    return seen
  }, [period])

  // ── Member management ────────────────────────────────────────
  const [memberDialogOpen, setMemberDialogOpen] = useState(false)
  const [editingMember, setEditingMember] = useState<GroupMember | null>(null)
  const [memberName, setMemberName] = useState('')
  const [memberEmail, setMemberEmail] = useState('')
  // The Securo user this member should be linked to (if any). When set,
  // name+email are derived from that user and the inputs are locked —
  // is_self is auto-inferred (true iff the linked user is the viewer).
  const [memberLinkedUserId, setMemberLinkedUserId] = useState<string | null>(null)

  const invalidateGroup = () => {
    // Prefix match: the group, its period and everything else under it.
    queryClient.invalidateQueries({ queryKey: ['groups', groupId] })
  }

  const memberMutation = useMutation({
    mutationFn: (payload: GroupMemberPayload) =>
      editingMember
        ? groupsApi.members.update(groupId, editingMember.id, payload)
        : groupsApi.members.create(groupId, payload),
    onSuccess: () => {
      invalidateGroup()
      setMemberDialogOpen(false)
      setEditingMember(null)
      toast.success(editingMember ? t('splitGroups.memberUpdated') : t('splitGroups.memberAdded'))
    },
    onError: (err: unknown) => {
      const detail =
        err && typeof err === 'object' && 'response' in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : undefined
      toast.error(detail ?? t('common.error'))
    },
  })

  const deleteMemberMutation = useMutation({
    mutationFn: (memberId: string) => groupsApi.members.delete(groupId, memberId),
    onSuccess: () => {
      invalidateGroup()
      setMemberDialogOpen(false)
      setEditingMember(null)
      toast.success(t('splitGroups.memberDeleted'))
    },
    onError: (err: unknown) => {
      const detail =
        err && typeof err === 'object' && 'response' in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : undefined
      toast.error(detail ?? t('common.error'))
    },
  })

  const openCreateMember = () => {
    setEditingMember(null)
    setMemberName('')
    setMemberEmail('')
    setMemberLinkedUserId(null)
    setMemberDialogOpen(true)
  }

  const openEditMember = (member: GroupMember) => {
    setEditingMember(member)
    setMemberName(member.name)
    setMemberEmail(member.email ?? '')
    setMemberLinkedUserId(member.linked_user_id)
    setMemberDialogOpen(true)
  }

  const saveMember = () => {
    // is_self is derived: a linked-to-viewer member is always "you".
    // Unlinked members can still represent the viewer if explicitly the
    // first member of their own group (legacy data) — we preserve that
    // flag during edits via the existing record.
    const linkedToViewer =
      memberLinkedUserId !== null && memberLinkedUserId === user?.id
    const is_self = linkedToViewer || (!memberLinkedUserId && (editingMember?.is_self ?? false))
    memberMutation.mutate({
      name: memberName.trim(),
      email: memberEmail.trim() || null,
      is_self,
    })
  }

  // ── Recording a contribution (a settlement in other kinds) ────
  const [settleOpen, setSettleOpen] = useState(false)
  const [settleFrom, setSettleFrom] = useState('')
  const [settleTo, setSettleTo] = useState('')
  const [settleAmount, setSettleAmount] = useState('')
  const [settleDate, setSettleDate] = useState(localDateString)
  const [settleNotes, setSettleNotes] = useState('')
  const [settleCurrency, setSettleCurrency] = useState('USD')
  // Optional ledger integration for the payer: 'none' records the
  // contribution only, 'create' makes a fresh debit, 'existing' links a
  // transaction the payer already has.
  const [settleTxMode, setSettleTxMode] = useState<'none' | 'create' | 'existing'>('none')
  const [settleAccountId, setSettleAccountId] = useState('')
  // The transaction picked to link, plus the search box state. We keep
  // the whole object so the selection stays visible even after the
  // search term changes and it drops out of the result list.
  const [settlePickedTx, setSettlePickedTx] = useState<Transaction | null>(null)
  const [settleTxSearch, setSettleTxSearch] = useState('')
  const [settleTxQuery, setSettleTxQuery] = useState('')

  // Which side of the contribution the viewer is on. The payer's leg is
  // a debit leaving their account and the receiver's a credit landing on
  // it, so the side decides what to search for and which link column the
  // picked transaction fills. Null when the viewer is on neither side —
  // an owner recording a transfer between two other members.
  const settleSide: 'payer' | 'receiver' | null = !viewerMemberId
    ? null
    : settleFrom === viewerMemberId
      ? 'payer'
      : settleTo === viewerMemberId
        ? 'receiver'
        : null

  // Accounts of the workspace — needed for the optional "create
  // transaction" toggle, and to keep the transaction picker to the
  // viewer's own rows.
  const { data: accountsList } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => accountsApi.list(),
    enabled: settleOpen,
  })

  // Both members of a household keep their accounts in one workspace, so
  // an unfiltered picker offers the other member's rows too — and the
  // API refuses those, because a link has to sit on the account of the
  // member on that side. Offer only what it accepts.
  const ownAccountIds = useMemo(
    () => (accountsList ?? []).filter((a) => a.user_id === user?.id).map((a) => a.id),
    [accountsList, user?.id],
  )

  // A linked member may record a contribution she is part of, on either
  // side, and nothing between two other people. Moving one side away
  // from her takes the other side to her, so the dialog cannot be walked
  // into a pair the API turns down.
  const keepViewerInThePair = (side: 'from' | 'to', value: string) => {
    if (isOwner || !viewerMemberId || value === viewerMemberId) return
    if (side === 'from') setSettleTo(viewerMemberId)
    else setSettleFrom(viewerMemberId)
  }

  // Debounce the transaction search so we don't hit the API on every
  // keystroke (mirrors the transactions page pattern).
  useEffect(() => {
    const id = setTimeout(() => setSettleTxQuery(settleTxSearch), 300)
    return () => clearTimeout(id)
  }, [settleTxSearch])

  // The viewer's own leg of the transfer, searched server-side and
  // capped — offered when linking an existing transaction instead of
  // creating one. Debits when they paid, credits when they were paid.
  const { data: settleTxOptions } = useQuery({
    queryKey: ['settle-tx-options', settleTxQuery, settleSide, ownAccountIds],
    queryFn: () =>
      transactionsApi.list({
        type: settleSide === 'receiver' ? 'credit' : 'debit',
        account_ids: ownAccountIds.length > 0 ? ownAccountIds : undefined,
        q: settleTxQuery || undefined,
        limit: 20,
        sort_by: 'date',
        sort_dir: 'desc',
      }),
    enabled: settleOpen && settleTxMode === 'existing' && settleSide !== null,
  })

  // A contribution backed by a real transaction goes through the marking
  // endpoint, not through create: only that path knows that the other
  // leg of the same transfer may already be a contribution, and joins it
  // instead of making a second one for money that moved once. A
  // hand-entered contribution with no transaction behind it still gets
  // created outright.
  const settlementMutation = useMutation({
    mutationFn: (payload: GroupSettlementPayload | MarkContributionPayload) =>
      'transaction' in payload
        ? groupsApi.settlements.markFromTransaction(groupId, payload.transaction)
        : groupsApi.settlements.create(groupId, payload),
    onSuccess: () => {
      invalidateGroup()
      setSettleOpen(false)
      toast.success(t('splitGroups.settled'))
    },
    onError: (err: unknown) => {
      const detail =
        err && typeof err === 'object' && 'response' in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : undefined
      toast.error(detail ?? t('common.error'))
    },
  })

  const deleteSettlementMutation = useMutation({
    mutationFn: (settlementId: string) =>
      groupsApi.settlements.delete(groupId, settlementId),
    onSuccess: invalidateGroup,
  })

  const openSettleUp = (
    from?: string,
    to?: string,
    amount?: number,
    currency?: string,
  ) => {
    setSettleFrom(from ?? '')
    setSettleTo(to ?? '')
    setSettleAmount(amount != null ? amount.toFixed(2) : '')
    setSettleDate(localDateString())
    setSettleNotes('')
    // Use the suggested transfer's currency, falling back to the group's
    // default for a free-form entry. This matters when the same group
    // carries positions in more than one currency.
    setSettleCurrency(currency ?? group?.default_currency ?? 'USD')
    setSettleTxMode('none')
    setSettleAccountId('')
    setSettlePickedTx(null)
    setSettleTxSearch('')
    setSettleTxQuery('')
    setSettleOpen(true)
  }

  const saveSettlement = () => {
    if (!settleFrom || !settleTo) return
    if (settleTxMode === 'existing' && settlePickedTx) {
      // The endpoint reads the amount, the currency, the date and which
      // side the transaction is on off the transaction itself — which is
      // why those inputs are disabled in this mode. Only the other
      // member and the note are ours to send.
      settlementMutation.mutate({
        transaction: {
          transaction_id: settlePickedTx.id,
          member_id: settleSide === 'receiver' ? settleFrom : settleTo,
          notes: settleNotes.trim() || null,
        },
      })
      return
    }
    if (!settleAmount) return
    const payload: GroupSettlementPayload = {
      from_member_id: settleFrom,
      to_member_id: settleTo,
      amount: parseFloat(settleAmount),
      currency: settleCurrency,
      date: settleDate,
      notes: settleNotes.trim() || null,
    }
    if (settleTxMode === 'create' && settleAccountId) {
      payload.account_id = settleAccountId
    }
    settlementMutation.mutate(payload)
  }

  // ── The catch-up calculator ──────────────────────────────────
  const [catchUpInput, setCatchUpInput] = useState('')

  // The member carrying the largest backlog: with two members that is
  // the one the catch-up is for. Nothing here is stored.
  const backlogLine = useMemo(() => {
    let largest: { memberId: string; amount: number; currency: string } | null = null
    for (const position of period?.positions ?? []) {
      const amount = Number(position.backlog)
      if (amount > 0 && (!largest || amount > largest.amount)) {
        largest = { memberId: position.member_id, amount, currency: position.currency }
      }
    }
    return largest
  }, [period])

  const catchUpAmount = Number.parseFloat(catchUpInput)
  const months = backlogLine
    ? catchUpMonths(backlogLine.amount, Number.isFinite(catchUpAmount) ? catchUpAmount : 0)
    : null

  const recordLabel = isHousehold
    ? t('splitGroups.pot.recordContribution')
    : t('splitGroups.recordSettlement')
  const transferActionLabel = isHousehold
    ? t('splitGroups.pot.recordContribution')
    : t('splitGroups.settleUp')

  if (loadingGroup) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-12 w-64" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    )
  }
  if (!group) {
    return <div className="text-muted-foreground">{t('splitGroups.notFound')}</div>
  }

  const transfersPeriodCard = (
    <SectionCard>
      <SectionHeader
        title={t('splitGroups.pot.transfersPeriod')}
        description={t('splitGroups.pot.transfersPeriodHint')}
      />
      <TransferList
        transfers={period?.transfers_period ?? []}
        loaded={showFigures}
        nameOf={nameOf}
        locale={locale}
        emptyLabel={t('splitGroups.pot.nothingToMove')}
        actionLabel={canWrite ? transferActionLabel : undefined}
        onPick={
          canWrite
            ? (transfer) =>
                openSettleUp(
                  transfer.from_member_id,
                  transfer.to_member_id,
                  Number(transfer.amount),
                  transfer.currency,
                )
            : undefined
        }
      />
    </SectionCard>
  )

  const transfersRunningCard = (
    <SectionCard>
      <SectionHeader
        title={t('splitGroups.pot.transfersRunning')}
        description={t('splitGroups.pot.transfersRunningHint')}
      />
      <TransferList
        transfers={period?.transfers_running ?? []}
        loaded={showFigures}
        nameOf={nameOf}
        locale={locale}
        emptyLabel={t('splitGroups.pot.nothingToMove')}
        actionLabel={canWrite ? transferActionLabel : undefined}
        onPick={
          canWrite
            ? (transfer) =>
                openSettleUp(
                  transfer.from_member_id,
                  transfer.to_member_id,
                  Number(transfer.amount),
                  transfer.currency,
                )
            : undefined
        }
      />
    </SectionCard>
  )

  const hasBreakdown =
    (period?.costs.length ?? 0) > 0 || (period?.shared_income.length ?? 0) > 0
  const transactionPages = Math.max(
    1,
    Math.ceil((period?.transactions.total ?? 0) / (period?.transactions.page_size ?? PAGE_SIZE)),
  )

  return (
    <div className="space-y-4">
      <PageHeader
        section={t('splitGroups.section')}
        title={group.name}
        action={
          <div className="flex items-center gap-2">
            {!isOwner && (
              <span className="text-xs bg-muted text-muted-foreground px-2 py-1 rounded-full">
                {t('splitGroups.sharedWithYou')}
              </span>
            )}
            <Button variant="outline" onClick={() => navigate('/groups')}>
              <ArrowLeft size={14} className="mr-1" />
              {t('common.back')}
            </Button>
          </div>
        }
      />

      {/* Period picker — every section below follows it. */}
      <SectionCard>
        <div className="px-4 sm:px-5 py-3 flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide mr-1">
            {t('splitGroups.pot.periodLabel')}
          </span>
          {/* The month picker. "This month" and "Last month" below move
              it rather than compete with it, so whichever month is on
              screen is always the one named here. */}
          <div
            className={`flex items-center gap-1 rounded-md border px-1 py-0.5 ${
              activeMode === 'month' ? 'border-primary' : 'border-border'
            }`}
          >
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              aria-label={t('splitGroups.pot.previousMonth')}
              onClick={() => pickMonth(shiftMonth(month, -1))}
            >
              <ChevronLeft size={14} />
            </Button>
            <select
              className="h-7 bg-transparent text-sm font-medium focus:outline-none cursor-pointer"
              aria-label={t('splitGroups.pot.monthLabel')}
              value={month}
              onChange={(e) => pickMonth(e.target.value)}
            >
              {monthOptions.map((option) => (
                <option key={option} value={option}>
                  {monthLabel(option, dateLocale)}
                </option>
              ))}
            </select>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              aria-label={t('splitGroups.pot.nextMonth')}
              onClick={() => pickMonth(shiftMonth(month, 1))}
            >
              <ChevronRight size={14} />
            </Button>
          </div>
          {PERIOD_PRESETS.map((option) => (
            <Button
              key={option}
              size="sm"
              variant={presetIsActive(option) ? 'default' : 'outline'}
              className="h-8"
              aria-pressed={presetIsActive(option)}
              onClick={() => pickPreset(option)}
            >
              {t(`splitGroups.pot.${option}`)}
            </Button>
          ))}
          {activeMode === 'custom' && (
            <div className="flex flex-wrap items-end gap-2 w-full sm:w-auto">
              <div className="space-y-1">
                <Label className="text-xs">{t('splitGroups.pot.rangeStart')}</Label>
                <DatePickerInput
                  value={customStart}
                  onChange={(value) => pickRange(value, 'start')}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">{t('splitGroups.pot.rangeEnd')}</Label>
                <DatePickerInput
                  value={customEnd}
                  onChange={(value) => pickRange(value, 'end')}
                />
              </div>
            </div>
          )}
        </div>
      </SectionCard>

      {periodFailed && (
        <SectionCard>
          <div className="px-4 py-6 flex flex-col items-center gap-3 text-center">
            <AlertTriangle size={20} className="text-rose-500" />
            <p className="text-sm text-foreground">{t('splitGroups.pot.loadFailed')}</p>
            <Button variant="outline" size="sm" onClick={() => refetchPeriod()}>
              {t('common.retry')}
            </Button>
          </div>
        </SectionCard>
      )}

      {!periodFailed && (
        <>
      {/* Who moves what: the period alone, and with the backlog. A
          household reads the period first; other kinds the running one. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 sm:gap-4">
        {isHousehold ? transfersPeriodCard : transfersRunningCard}
        {isHousehold ? transfersRunningCard : transfersPeriodCard}
      </div>

      {/* Costs by category, with shared income on its own lines and the
          net total straight from the server. */}
      <SectionCard>
        <SectionHeader title={t('splitGroups.pot.costs')} />
        {!showFigures ? (
          <div className="p-4 space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : !hasBreakdown ? (
          <div className="text-center py-8 text-muted-foreground text-sm">
            {t('splitGroups.pot.noCosts')}
          </div>
        ) : (
          currencies.map((currency) => {
            const costs = (period?.costs ?? []).filter((line) => line.currency === currency)
            const income = (period?.shared_income ?? []).filter(
              (line) => line.currency === currency,
            )
            const total = (period?.totals ?? []).find((line) => line.currency === currency)
            if (costs.length === 0 && income.length === 0) return null
            return (
              <div key={currency} className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-muted-foreground border-b border-border">
                      <th className="text-left font-medium px-4 py-2">
                        {t('splitGroups.pot.category')}
                      </th>
                      <th className="text-right font-medium px-4 py-2 whitespace-nowrap">
                        {t('splitGroups.pot.groupTotal')}
                      </th>
                      {periodMembers.map((member) => (
                        <th
                          key={member.id}
                          className="text-right font-medium px-4 py-2 whitespace-nowrap"
                        >
                          {member.name}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {costs.map((line) => (
                      <tr key={`cost-${line.category_id ?? 'none'}`}>
                        <td className="px-4 py-2">
                          {line.category_name ?? t('splitGroups.uncategorized')}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {formatCurrency(Number(line.total), currency, locale)}
                        </td>
                        {periodMembers.map((member) => (
                          <td
                            key={member.id}
                            className="px-4 py-2 text-right tabular-nums text-muted-foreground"
                          >
                            {formatCurrency(shareOf(line.shares, member.id), currency, locale)}
                          </td>
                        ))}
                      </tr>
                    ))}
                    {income.length > 0 && (
                      <tr className="bg-muted/40">
                        <td
                          className="px-4 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                          colSpan={2 + periodMembers.length}
                        >
                          {t('splitGroups.pot.sharedIncome')}
                        </td>
                      </tr>
                    )}
                    {income.map((line) => (
                      <tr key={`income-${line.category_id ?? 'none'}`}>
                        <td className="px-4 py-2">
                          {line.category_name ?? t('splitGroups.uncategorized')}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums text-emerald-600">
                          {formatCurrency(-Number(line.total), currency, locale)}
                        </td>
                        {periodMembers.map((member) => (
                          <td
                            key={member.id}
                            className="px-4 py-2 text-right tabular-nums text-emerald-600/80"
                          >
                            {formatCurrency(
                              -shareOf(line.shares, member.id),
                              currency,
                              locale,
                            )}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                  {total && (
                    <tfoot>
                      <tr className="border-t border-border font-semibold">
                        <td className="px-4 py-2">{t('splitGroups.pot.netTotal')}</td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {formatCurrency(Number(total.net), currency, locale)}
                        </td>
                        {periodMembers.map((member) => (
                          <td key={member.id} className="px-4 py-2 text-right tabular-nums">
                            {formatCurrency(shareOf(total.shares, member.id), currency, locale)}
                          </td>
                        ))}
                      </tr>
                    </tfoot>
                  )}
                </table>
              </div>
            )
          })
        )}
      </SectionCard>

      {/* What each member paid against their share. */}
      <SectionCard>
        <SectionHeader
          title={t('splitGroups.pot.positions')}
          description={t('splitGroups.pot.positionsHint')}
        />
        {!showFigures ? (
          <div className="p-4 space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : currencies.length === 0 ? (
          <div className="text-center py-6 text-muted-foreground text-sm">
            {t('splitGroups.pot.noCosts')}
          </div>
        ) : (
          currencies.map((currency) => (
            <div key={currency} className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-muted-foreground border-b border-border">
                    <th className="text-left font-medium px-4 py-2">
                      {t('splitGroups.pot.member')}
                    </th>
                    <th className="text-right font-medium px-4 py-2">
                      {t('splitGroups.pot.paid')}
                    </th>
                    <th className="text-right font-medium px-4 py-2">
                      {t('splitGroups.pot.share')}
                    </th>
                    <th className="text-right font-medium px-4 py-2">
                      {t('splitGroups.pot.sent')}
                    </th>
                    <th className="text-right font-medium px-4 py-2">
                      {t('splitGroups.pot.received')}
                    </th>
                    <th className="text-right font-medium px-4 py-2 whitespace-nowrap">
                      {t('splitGroups.pot.thisPeriod')}
                    </th>
                    <th className="text-right font-medium px-4 py-2">
                      {t('splitGroups.pot.backlog')}
                    </th>
                    <th className="text-right font-medium px-4 py-2 whitespace-nowrap">
                      {t('splitGroups.pot.inclBacklog')}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(period?.positions ?? [])
                    .filter((position) => position.currency === currency)
                    .map((position) => (
                      <tr key={`${position.member_id}-${currency}`}>
                        <td className="px-4 py-2 font-medium whitespace-nowrap">
                          {nameOf(position.member_id)}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {formatCurrency(Number(position.paid), currency, locale)}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">
                          {formatCurrency(Number(position.share), currency, locale)}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                          {formatCurrency(
                            Number(position.contributions_made),
                            currency,
                            locale,
                          )}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                          {formatCurrency(
                            Number(position.contributions_received),
                            currency,
                            locale,
                          )}
                        </td>
                        <td
                          className={`px-4 py-2 text-right tabular-nums font-semibold ${
                            Number(position.period_position) < 0
                              ? 'text-emerald-600'
                              : 'text-foreground'
                          }`}
                        >
                          {formatCurrency(Number(position.period_position), currency, locale)}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                          {formatCurrency(Number(position.backlog), currency, locale)}
                        </td>
                        <td
                          className={`px-4 py-2 text-right tabular-nums font-semibold ${
                            Number(position.running_position) < 0
                              ? 'text-emerald-600'
                              : 'text-foreground'
                          }`}
                        >
                          {formatCurrency(Number(position.running_position), currency, locale)}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ))
        )}
      </SectionCard>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 sm:gap-4">
        {/* The catch-up calculator. Nothing here is stored. A range with
            no start carries no backlog by definition, so there is
            nothing for it to answer and it stays away. */}
        {range.start && (
        <SectionCard>
          <SectionHeader
            title={t('splitGroups.pot.catchUp')}
            description={t('splitGroups.pot.catchUpHint')}
          />
          <div className="px-4 py-4 space-y-3">
            <div className="space-y-2">
              <Label htmlFor="catch-up">{t('splitGroups.pot.catchUpMonthly')}</Label>
              <Input
                id="catch-up"
                type="number"
                step="0.01"
                min="0"
                value={catchUpInput}
                onChange={(e) => setCatchUpInput(e.target.value)}
              />
            </div>
            <p className="text-sm">
              {!backlogLine
                ? t('splitGroups.pot.catchUpNoBacklog')
                : months === null
                  ? t('splitGroups.pot.catchUpEnterAmount')
                  : t('splitGroups.pot.catchUpResult', {
                      count: months,
                      name: nameOf(backlogLine.memberId),
                      amount: formatCurrency(
                        backlogLine.amount,
                        backlogLine.currency,
                        locale,
                      ),
                    })}
            </p>
          </div>
        </SectionCard>
        )}

        {/* The period's contributions, straight from the response. */}
        <SectionCard>
          <SectionHeader
            title={
              isHousehold
                ? t('splitGroups.pot.contributions')
                : t('splitGroups.settlements')
            }
            action={
              isOwner && canWrite ? (
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1.5 h-8"
                  onClick={() => openSettleUp()}
                >
                  {recordLabel}
                </Button>
              ) : undefined
            }
          />
          {!showFigures ? (
            <div className="p-4 space-y-2">
              <Skeleton className="h-10 w-full" />
            </div>
          ) : period && period.contributions.length > 0 ? (
            <ul className="divide-y divide-border">
              {period.contributions.map((contribution) => (
                <li
                  key={contribution.id}
                  className="flex items-center justify-between px-4 py-3"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm flex items-center gap-1.5">
                      <span className="font-medium">{nameOf(contribution.from_member_id)}</span>
                      <ArrowRight size={12} className="text-muted-foreground" />
                      <span className="font-medium">{nameOf(contribution.to_member_id)}</span>
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {new Date(contribution.date + 'T00:00:00').toLocaleDateString(dateLocale)}
                      {contribution.notes ? ` · ${contribution.notes}` : ''}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-semibold tabular-nums">
                      {formatCurrency(
                        Number(contribution.amount),
                        contribution.currency,
                        locale,
                      )}
                    </span>
                    {isOwner && canWrite && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => deleteSettlementMutation.mutate(contribution.id)}
                        title={t('common.delete')}
                        aria-label={t('common.delete')}
                      >
                        <Trash2 size={14} />
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-center py-6 text-muted-foreground text-sm">
              {isHousehold
                ? t('splitGroups.pot.noContributions')
                : t('splitGroups.pot.noSettlements')}
            </div>
          )}
        </SectionCard>
      </div>

      {/* The period's shared transactions, paged on the server. */}
      <SectionCard>
        <SectionHeader
          title={t('splitGroups.pot.transactions')}
          description={
            period && period.payer_assumed_transactions.length > 0
              ? t('splitGroups.pot.payerAssumedHint', {
                  total: period.payer_assumed_transactions.length,
                })
              : undefined
          }
          action={
            <Button
              variant="ghost"
              size="sm"
              className="gap-1 h-8 text-xs"
              onClick={() => navigate(`/transactions?group_id=${groupId}`)}
            >
              {t('splitGroups.viewAllTransactions')}
              <ArrowRight size={12} />
            </Button>
          }
        />
        {!showFigures || !period ? (
          <div className="p-4 space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : period.transactions.items.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground text-sm flex flex-col items-center gap-2">
            <Receipt size={20} className="opacity-50" />
            {t('splitGroups.pot.noTransactions')}
          </div>
        ) : (
          <>
            <ul className="divide-y divide-border">
              {period.transactions.items.map((tx) => (
                <li
                  key={tx.id}
                  className="flex items-center gap-3 px-4 py-3 hover:bg-muted cursor-pointer transition-colors"
                  onClick={() => navigate(`/transactions?group_id=${groupId}&highlight=${tx.id}`)}
                >
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-foreground truncate">
                      {tx.description}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {new Date(tx.date + 'T00:00:00').toLocaleDateString(dateLocale)}
                      {tx.category_name ? ` · ${tx.category_name}` : ''}
                      {tx.payer_member_id
                        ? ` · ${t('splitGroups.pot.paidBy', { name: nameOf(tx.payer_member_id) })}`
                        : ''}
                    </p>
                  </div>
                  {tx.payer_assumed && (
                    <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full inline-flex items-center gap-1 shrink-0">
                      <AlertTriangle size={11} />
                      {t('splitGroups.pot.payerAssumed')}
                    </span>
                  )}
                  <span
                    className={`text-sm font-semibold tabular-nums ml-3 ${
                      tx.type === 'debit' ? 'text-rose-500' : 'text-emerald-600'
                    }`}
                  >
                    {formatCurrency(Number(tx.amount), tx.currency, locale)}
                  </span>
                </li>
              ))}
            </ul>
            <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border">
              <span className="text-xs text-muted-foreground">
                {t('splitGroups.pot.pageOf', {
                  page: period.transactions.page,
                  pages: transactionPages,
                })}
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                >
                  {t('common.previous')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= transactionPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  {t('common.next')}
                </Button>
              </div>
            </div>
          </>
        )}
      </SectionCard>
        </>
      )}

      {/* Members */}
      <SectionCard>
        <SectionHeader
          title={t('splitGroups.members')}
          action={
            isOwner && canWrite ? (
              <Button size="sm" className="gap-1.5 h-8" onClick={openCreateMember}>
                <UserPlus size={13} />
                {t('splitGroups.addMember')}
              </Button>
            ) : undefined
          }
        />
        {group.members.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground text-sm">
            {t('splitGroups.noMembers')}
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {group.members.map((member) => (
              <li key={member.id} className="flex items-center justify-between px-4 py-3">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{member.name}</span>
                    {/* "(you)" marks the viewer; the owner's member comes
                        from the period response, which resolves it the
                        same way for everyone who looks. */}
                    {viewerMemberId === member.id ? (
                      <span className="text-xs bg-primary/10 text-primary px-2 py-0.5 rounded-full">
                        {t('splitGroups.you')}
                      </span>
                    ) : ownerMemberId === member.id ? (
                      <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full">
                        {t('splitGroups.ownerBadge')}
                      </span>
                    ) : null}
                  </div>
                  {member.email && (
                    <p className="text-xs text-muted-foreground inline-flex items-center gap-1">
                      {member.linked_user_id && <Link2 size={10} />}
                      {member.email}
                    </p>
                  )}
                </div>
                {isOwner && canWrite && (
                  <Button variant="ghost" size="sm" onClick={() => openEditMember(member)}>
                    {t('common.edit')}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

      {/* Member dialog */}
      <Dialog open={memberDialogOpen} onOpenChange={setMemberDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {editingMember ? t('splitGroups.editMember') : t('splitGroups.addMember')}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <MemberForm
              name={memberName}
              onChangeName={setMemberName}
              email={memberEmail}
              onChangeEmail={setMemberEmail}
              linkedUserId={memberLinkedUserId}
              onChangeLinkedUserId={setMemberLinkedUserId}
            />
          </div>
          <DialogFooter className={editingMember ? 'flex justify-between sm:justify-between' : ''}>
            {editingMember && (
              <Button
                variant="destructive"
                onClick={() => deleteMemberMutation.mutate(editingMember.id)}
                disabled={deleteMemberMutation.isPending}
              >
                <Trash2 size={14} className="mr-1" />
                {t('common.delete')}
              </Button>
            )}
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setMemberDialogOpen(false)}>
                {t('common.cancel')}
              </Button>
              <Button
                onClick={saveMember}
                disabled={!memberName.trim() || memberMutation.isPending}
              >
                {t('common.save')}
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Contribution / settlement dialog */}
      <Dialog open={settleOpen} onOpenChange={setSettleOpen}>
        <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{recordLabel}</DialogTitle>
          </DialogHeader>
          {(() => {
            const viewerIsPayer = settleSide === 'payer'
            return (
          <div className="space-y-4 min-w-0">
            <div className="space-y-2">
              <Label>{t('splitGroups.from')}</Label>
              <select
                className="w-full border border-border rounded-md px-3 py-2 text-sm bg-card"
                value={settleFrom}
                onChange={(e) => {
                  setSettleFrom(e.target.value)
                  keepViewerInThePair('from', e.target.value)
                  // Reset the ledger-side options: which side the viewer
                  // is on decides what can be linked.
                  setSettleTxMode('none')
                  setSettleAccountId('')
                  setSettlePickedTx(null)
                  setSettleTxSearch('')
                }}
              >
                <option value="">{t('splitGroups.selectMember')}</option>
                {group.members.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label>{t('splitGroups.to')}</Label>
              <select
                className="w-full border border-border rounded-md px-3 py-2 text-sm bg-card"
                value={settleTo}
                onChange={(e) => {
                  setSettleTo(e.target.value)
                  keepViewerInThePair('to', e.target.value)
                  setSettleTxMode('none')
                  setSettleAccountId('')
                  setSettlePickedTx(null)
                  setSettleTxSearch('')
                }}
              >
                <option value="">{t('splitGroups.selectMember')}</option>
                {group.members.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </select>
            </div>
            {/* Transaction action — placed right after the members so
                whoever is on one side of this can decide upfront whether
                a real transaction backs the record. When linking an
                existing transaction, the amount/currency/date below
                mirror that transaction and lock so the two records can't
                disagree. The payer may also have a fresh debit written;
                the receiver only ever links the credit that landed, so a
                contribution is never a copy of money already imported. */}
            {settleSide !== null && (
                <div className="space-y-2">
                  <Label>{t('splitGroups.txAction')}</Label>
                  <select
                    className="w-full border border-border rounded-md px-3 py-2 text-sm bg-card"
                    value={settleTxMode}
                    onChange={(e) => {
                      setSettleTxMode(e.target.value as 'none' | 'create' | 'existing')
                      setSettleAccountId('')
                      setSettlePickedTx(null)
                      setSettleTxSearch('')
                    }}
                  >
                    <option value="none">{t('splitGroups.txActionNone')}</option>
                    {viewerIsPayer && (
                      <option value="create">{t('splitGroups.txActionCreate')}</option>
                    )}
                    <option value="existing">{t('splitGroups.txActionExisting')}</option>
                  </select>
                  {settleTxMode === 'create' && (
                    <div className="space-y-1">
                      <select
                        className="w-full border border-border rounded-md px-3 py-2 text-sm bg-card"
                        value={settleAccountId}
                        onChange={(e) => setSettleAccountId(e.target.value)}
                      >
                        <option value="">{t('splitGroups.selectAccount')}</option>
                        {sortAccountsByDisplayName(accountsList ?? []).map((a) => (
                          <option key={a.id} value={a.id}>
                            {getAccountName(a)}
                          </option>
                        ))}
                      </select>
                      <p className="text-xs text-muted-foreground">
                        {t('splitGroups.affectAccountHint')}
                      </p>
                    </div>
                  )}
                  {settleTxMode === 'existing' && (
                    <div className="space-y-1.5">
                      <Input
                        type="text"
                        value={settleTxSearch}
                        onChange={(e) => setSettleTxSearch(e.target.value)}
                        placeholder={t('splitGroups.searchTransaction')}
                      />
                      <div className="max-h-44 overflow-y-auto rounded-md border border-border divide-y divide-border">
                        {(settleTxOptions?.items ?? []).length === 0 ? (
                          <p className="text-xs text-muted-foreground px-3 py-4 text-center">
                            {t('splitGroups.noTransactions')}
                          </p>
                        ) : (
                          (settleTxOptions?.items ?? []).map((tx) => {
                            const picked = settlePickedTx?.id === tx.id
                            return (
                              <button
                                key={tx.id}
                                type="button"
                                onClick={() => {
                                  // Picking an existing transaction *as* the
                                  // contribution: align amount, currency and
                                  // date so the two records can't disagree.
                                  setSettlePickedTx(tx)
                                  setSettleAmount(Number(tx.amount).toFixed(2))
                                  setSettleCurrency(tx.currency)
                                  setSettleDate(tx.date)
                                }}
                                className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between gap-3 ${
                                  picked ? 'bg-primary/10' : 'hover:bg-muted/50'
                                }`}
                              >
                                <span className="min-w-0 truncate">
                                  <span className="text-muted-foreground">{tx.date}</span> ·{' '}
                                  {tx.description}
                                </span>
                                <span className="shrink-0 tabular-nums text-muted-foreground">
                                  {tx.amount} {tx.currency}
                                </span>
                              </button>
                            )
                          })
                        )}
                      </div>
                      {settlePickedTx && (
                        <p className="text-xs text-muted-foreground truncate">
                          {t('splitGroups.selectedTransaction')}: {settlePickedTx.date} ·{' '}
                          {settlePickedTx.description}
                        </p>
                      )}
                    </div>
                  )}
                </div>
            )}
            <div className="grid grid-cols-3 gap-3">
              <div className="space-y-2 col-span-2">
                <Label>{t('splitGroups.amount')}</Label>
                <Input
                  type="number"
                  step="0.01"
                  value={settleAmount}
                  onChange={(e) => setSettleAmount(e.target.value)}
                  disabled={settleTxMode === 'existing'}
                />
              </div>
              <div className="space-y-2">
                <Label>{t('splitGroups.currency')}</Label>
                <Input
                  value={settleCurrency}
                  maxLength={3}
                  onChange={(e) => setSettleCurrency(e.target.value.toUpperCase())}
                  disabled={settleTxMode === 'existing'}
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label>{t('splitGroups.date')}</Label>
              <DatePickerInput
                value={settleDate}
                onChange={setSettleDate}
                className="w-full justify-start"
                disabled={settleTxMode === 'existing'}
              />
            </div>
            <div className="space-y-2">
              <Label>{t('splitGroups.notes')}</Label>
              <textarea
                className="w-full border border-input rounded-md px-3 py-2 text-sm bg-card resize-none"
                rows={2}
                value={settleNotes}
                onChange={(e) => setSettleNotes(e.target.value)}
              />
            </div>
          </div>
            )
          })()}
          <DialogFooter>
            <Button variant="outline" onClick={() => setSettleOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={saveSettlement}
              disabled={
                !settleFrom ||
                !settleTo ||
                settleFrom === settleTo ||
                !settleAmount ||
                (settleTxMode === 'create' && !settleAccountId) ||
                (settleTxMode === 'existing' && !settlePickedTx) ||
                settlementMutation.isPending
              }
            >
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

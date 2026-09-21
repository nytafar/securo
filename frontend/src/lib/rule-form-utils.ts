import type {
  RuleAction,
  RuleContributionActionValue,
  RuleShareActionValue,
} from '../types'

/** The two actions that work on a group's common pot rather than on the
 * transaction's own fields. Their value is an object, not a string. */
export const GROUP_ACTION_OPS = ['share_in_group', 'mark_as_contribution']

export function isGroupAction(action: RuleAction): boolean {
  return GROUP_ACTION_OPS.includes(action.op)
}

export function shareValue(action: RuleAction): RuleShareActionValue {
  const value = action.value
  if (value && typeof value === 'object' && 'splits' in value) return value
  return { group_id: '', share_type: 'equal', splits: [] }
}

export function contributionValue(action: RuleAction): RuleContributionActionValue {
  const value = action.value
  if (value && typeof value === 'object' && 'member_id' in value) return value
  return { group_id: '', member_id: '' }
}

/** True while a group action is still being filled in. The backend
 * refuses a group with no members and a percent split that misses 100, so
 * an incomplete row is kept out of the preview rather than failing it —
 * the same courtesy `previewableActions` does a blank category. */
export function isIncompleteGroupAction(action: RuleAction): boolean {
  if (action.op === 'share_in_group') {
    const value = shareValue(action)
    if (!value.group_id || value.splits.length === 0) return true
    if (value.share_type !== 'percent') return false
    const total = value.splits.reduce((sum, split) => sum + (split.share_pct ?? 0), 0)
    return Math.abs(total - 100) >= 0.005
  }
  if (action.op === 'mark_as_contribution') {
    const value = contributionValue(action)
    return !value.group_id || !value.member_id
  }
  return false
}

/** An action's value as text. The group actions carry an object, and no
 * field that edits text should show it. */
export function actionText(action: RuleAction): string {
  return typeof action.value === 'string' ? action.value : ''
}

export function isInvalidDescriptionAction(action: RuleAction): boolean {
  if (action.op !== 'set_description') return false
  const value = String(action.value ?? '').trim()
  return value === '' || value.length > 500
}

/** The actions a draft is complete enough to preview.
 *
 * The editor holds an action row from the moment it is added, so a draft in
 * progress routinely carries `set_category` with nothing picked yet. That is a
 * rule still being written, not a broken one — the engine already treats it as
 * a no-op — and the preview should keep answering the question about the
 * conditions instead of failing the backend's action validation. Actions the
 * form itself flags are dropped for the same reason: the inline error already
 * says what is wrong. `ignore` carries no value at all.
 */
export function previewableActions(actions: RuleAction[]): RuleAction[] {
  return actions.filter((action) => {
    if (action.op === 'ignore') return true
    if (isGroupAction(action)) return !isIncompleteGroupAction(action)
    if (isInvalidDescriptionAction(action)) return false
    return String(action.value ?? '').trim() !== ''
  })
}

export function parseRulePriority(value: string): number {
  if (value.trim() === '') return 0
  const priority = Number(value)
  return Number.isFinite(priority) ? priority : 0
}

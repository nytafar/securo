type CategoryReference = {
  id: string
  name: string
}

type RuleReference = {
  // A rule action's value is a category id here, but the group actions
  // carry an object, so the list is typed for what it really holds and
  // narrowed where a string is wanted.
  actions: Array<{ op: string; value: unknown }>
}

export function findCategoryReference<T extends CategoryReference>(
  categories: readonly T[],
  categoryId: string,
): T | undefined {
  return categories.find((category) => category.id === categoryId)
}

export function getRuleCategoryId(rule: RuleReference): string | null {
  const action = rule.actions.find(
    (candidate) => candidate.op === 'set_category' && candidate.value,
  )
  return typeof action?.value === 'string' ? action.value : null
}

export function getRuleCategoryName<T extends CategoryReference>(
  rule: RuleReference,
  categories: readonly T[],
): string | null {
  const categoryId = getRuleCategoryId(rule)
  if (!categoryId) return null
  return findCategoryReference(categories, categoryId)?.name ?? null
}

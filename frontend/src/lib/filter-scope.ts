/**
 * What an active filter leaves a page able to ask for.
 *
 * Two different questions hang on "the filter resolves to no accounts":
 *
 *  - **Cash** — balances, the transaction list, the calendar, cash flow.
 *    With no accounts there is nothing to walk, and the query has to be
 *    skipped rather than sent: an empty list of account ids serializes
 *    to no parameter at all and would silently read as "all accounts".
 *  - **Consumption** — the category breakdown, income and expenses,
 *    budgets. A person who owns no account here still carries their
 *    share of what the others paid, so there is very much an answer to
 *    ask for. Only a collection with no accounts has nothing.
 *
 * Reading both off one boolean is what left the filter on such a person
 * showing a total with a blank breakdown under it.
 */
export type FilterScope = {
  /** Skip the cash queries: the filter resolves to no accounts. */
  noCashAccounts: boolean
  /** Skip the consumption queries: a collection with no accounts. */
  noConsumption: boolean
}

export function filterScope(
  activeAccountIds: string[] | null,
  activeUserId: string | null,
): FilterScope {
  const noCashAccounts = activeAccountIds !== null && activeAccountIds.length === 0
  return { noCashAccounts, noConsumption: noCashAccounts && !activeUserId }
}

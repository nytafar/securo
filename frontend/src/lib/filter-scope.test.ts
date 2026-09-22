import { describe, expect, it } from 'vitest'

import { filterScope } from '@/lib/filter-scope'

describe('what an active filter leaves a page able to ask for', () => {
  it('asks for everything when nothing is filtered', () => {
    expect(filterScope(null, null)).toEqual({ noCashAccounts: false, noConsumption: false })
  })

  it('asks for everything when the filter resolves to accounts', () => {
    expect(filterScope(['acct-her'], 'user-her')).toEqual({
      noCashAccounts: false,
      noConsumption: false,
    })
  })

  it('asks for nothing under a collection with no accounts', () => {
    expect(filterScope([], null)).toEqual({ noCashAccounts: true, noConsumption: true })
  })

  it('still asks for the consumption of a person who owns no account', () => {
    // She carries half the groceries he paid for. There is no cash to
    // walk on her side and a breakdown to show all the same.
    expect(filterScope([], 'user-her')).toEqual({
      noCashAccounts: true,
      noConsumption: false,
    })
  })
})

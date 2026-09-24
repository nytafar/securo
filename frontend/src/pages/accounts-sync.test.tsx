import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { AxiosError, AxiosHeaders } from 'axios'

import AccountsPage from '@/pages/accounts'
import { renderWithProviders, t } from '@/test/utils'
import type { BankConnection } from '@/types'

const api = vi.hoisted(() => ({
  accounts: { list: vi.fn() },
  connections: { list: vi.fn(), getProviders: vi.fn(), sync: vi.fn() },
  currencies: { list: vi.fn() },
}))

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}))

vi.mock('@/lib/api', () => api)
vi.mock('sonner', () => ({ toast }))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({ canWrite: true }),
}))

vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))

function connection(settings: BankConnection['settings'] = null): BankConnection {
  return {
    id: 'conn-1',
    user_id: 'user-1',
    provider: 'enable_banking',
    institution_name: 'Test Bank',
    display_name: null,
    logo_url: null,
    external_id: 'session-1',
    status: 'active',
    settings,
    last_sync_at: '2026-01-01T00:00:00Z',
    created_at: '2026-01-01T00:00:00Z',
    institutions: [],
  }
}

function rateLimitedError(): AxiosError {
  const headers = new AxiosHeaders()
  return new AxiosError('Request failed with status code 429', 'ERR_BAD_REQUEST', undefined, null, {
    status: 429,
    statusText: 'Too Many Requests',
    headers,
    config: { headers },
    data: { detail: { message: 'Enable Banking → 429', code: 'provider_rate_limited' } },
  })
}

describe('AccountsPage bank sync', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.accounts.list.mockResolvedValue([])
    api.connections.getProviders.mockResolvedValue([])
    api.currencies.list.mockResolvedValue([])
  })

  it('says the bank limit was reached instead of reporting a completed sync', async () => {
    api.connections.list.mockResolvedValue([connection()])
    api.connections.sync.mockRejectedValue(rateLimitedError())

    const { user } = renderWithProviders(<AccountsPage />, { route: '/accounts' })
    await user.click(await screen.findByRole('button', { name: t('accounts.sync') }))

    await waitFor(() => expect(toast.warning).toHaveBeenCalledWith(t('accounts.syncRateLimited')))
    expect(toast.success).not.toHaveBeenCalled()
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('shows when automatic sync resumes while the bank limit holds', async () => {
    const until = new Date(Date.now() + 3 * 60 * 60 * 1000)
    api.connections.list.mockResolvedValue([
      connection({ rate_limited_until: until.toISOString() }),
    ])

    renderWithProviders(<AccountsPage />, { route: '/accounts' })

    expect(
      await screen.findByText(
        t('accounts.rateLimitedUntil', { time: until.toLocaleString('en-US') }),
      ),
    ).toBeInTheDocument()
  })

  it('drops the note once the backoff has passed', async () => {
    const until = new Date(Date.now() - 60 * 1000)
    api.connections.list.mockResolvedValue([
      connection({ rate_limited_until: until.toISOString() }),
    ])

    renderWithProviders(<AccountsPage />, { route: '/accounts' })

    await screen.findByText('Test Bank')
    expect(
      screen.queryByText(t('accounts.rateLimitedUntil', { time: until.toLocaleString('en-US') })),
    ).not.toBeInTheDocument()
  })
})

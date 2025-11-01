import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import Dashboard from './Dashboard';
import { automationAPI, streamAPI, m3uAPI, streamCheckerAPI } from '../services/api';

// Mock the APIs
jest.mock('../services/api', () => ({
  automationAPI: {
    getStatus: jest.fn(),
    updateConfig: jest.fn(),
  },
  streamAPI: {
    refreshPlaylist: jest.fn(),
    discoverStreams: jest.fn(),
  },
  m3uAPI: {
    getAccounts: jest.fn(),
  },
  streamCheckerAPI: {
    getStatus: jest.fn(),
  },
}));

describe('Dashboard Component', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  const mockAutomationStatus = {
    running: true,
    last_playlist_update: '2024-01-01T10:00:00Z',
    next_playlist_update: '2024-01-01T10:05:00Z',
    config: {
      playlist_update_interval_minutes: 5,
      enabled_m3u_accounts: [],
      enabled_features: {
        auto_playlist_update: true,
        auto_stream_discovery: true,
      },
    },
    recent_changelog: [],
  };

  const mockStreamCheckerStatus = {
    running: true,
    config: {
      global_check_schedule: {
        enabled: true,
        cron_expression: '0 3 * * *',
        frequency: 'daily',
        hour: 3,
        minute: 0,
      },
    },
  };

  test('displays correct cron schedule for daily at 3:00 AM', async () => {
    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: mockStreamCheckerStatus });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Global Check Schedule')).toBeInTheDocument();
    expect(screen.getByText('Daily at 03:00')).toBeInTheDocument();
  });

  test('displays correct cron schedule for custom daily time', async () => {
    const customSchedule = {
      ...mockStreamCheckerStatus,
      config: {
        global_check_schedule: {
          enabled: true,
          cron_expression: '30 14 * * *',
        },
      },
    };

    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: customSchedule });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Daily at 14:30')).toBeInTheDocument();
  });

  test('displays correct cron schedule for monthly', async () => {
    const monthlySchedule = {
      ...mockStreamCheckerStatus,
      config: {
        global_check_schedule: {
          enabled: true,
          cron_expression: '0 2 15 * *',
        },
      },
    };

    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: monthlySchedule });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Monthly on day 15 at 02:00')).toBeInTheDocument();
  });

  test('displays cron expression for complex patterns', async () => {
    const complexSchedule = {
      ...mockStreamCheckerStatus,
      config: {
        global_check_schedule: {
          enabled: true,
          cron_expression: '0 */6 * * *',
        },
      },
    };

    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: complexSchedule });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Cron: 0 */6 * * *')).toBeInTheDocument();
  });

  test('displays fallback schedule when cron_expression not available', async () => {
    const legacySchedule = {
      ...mockStreamCheckerStatus,
      config: {
        global_check_schedule: {
          enabled: true,
          frequency: 'daily',
          hour: 5,
          minute: 30,
        },
      },
    };

    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: legacySchedule });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Daily at 05:30')).toBeInTheDocument();
  });

  test('displays "Not configured" when schedule is missing', async () => {
    const noSchedule = {
      ...mockStreamCheckerStatus,
      config: {},
    };

    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: noSchedule });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Not configured')).toBeInTheDocument();
  });

  test('displays playlist update interval correctly', async () => {
    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: mockStreamCheckerStatus });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Playlist Update Interval')).toBeInTheDocument();
    expect(screen.getByText('5 minutes')).toBeInTheDocument();
  });

  test('handles API errors gracefully', async () => {
    automationAPI.getStatus.mockRejectedValue(new Error('API Error'));
    streamCheckerAPI.getStatus.mockRejectedValue(new Error('API Error'));
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Failed to load automation status')).toBeInTheDocument();
  });

  test('fetches both automation and stream checker status', async () => {
    automationAPI.getStatus.mockResolvedValue({ data: mockAutomationStatus });
    streamCheckerAPI.getStatus.mockResolvedValue({ data: mockStreamCheckerStatus });
    m3uAPI.getAccounts.mockResolvedValue({ data: [] });

    render(<Dashboard />);

    await waitFor(() => {
      expect(automationAPI.getStatus).toHaveBeenCalledTimes(1);
      expect(streamCheckerAPI.getStatus).toHaveBeenCalledTimes(1);
    });
  });
});

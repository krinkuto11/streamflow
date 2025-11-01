import React, { useState, useEffect } from 'react';
import {
  Grid,
  Card,
  CardContent,
  Typography,
  Button,
  Chip,
  Box,
  CircularProgress,
  Alert,
  Divider,
  FormGroup,
  FormControlLabel,
  Checkbox
} from '@mui/material';
import {
  PlayArrow as StartIcon,
  Refresh as RefreshIcon,
  PlaylistAdd as DiscoverIcon,
  CheckCircle as CheckIcon,
  Schedule as ScheduleIcon,
  TrendingUp as TrendingIcon
} from '@mui/icons-material';
import { automationAPI, streamAPI, m3uAPI, streamCheckerAPI } from '../services/api';

function Dashboard() {
  const [status, setStatus] = useState(null);
  const [streamCheckerStatus, setStreamCheckerStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [m3uAccounts, setM3uAccounts] = useState([]);
  const [selectedAccounts, setSelectedAccounts] = useState([]);

  useEffect(() => {
    loadStatus();
    loadM3uAccounts();
    // Refresh status every 30 seconds
    const interval = setInterval(loadStatus, 30000);
    return () => clearInterval(interval);
  }, []);

  const loadStatus = async () => {
    try {
      const [automationResponse, streamCheckerResponse] = await Promise.all([
        automationAPI.getStatus(),
        streamCheckerAPI.getStatus()
      ]);
      setStatus(automationResponse.data);
      setStreamCheckerStatus(streamCheckerResponse.data);
      // Update selected accounts from config
      const enabledAccounts = automationResponse.data?.config?.enabled_m3u_accounts || [];
      setSelectedAccounts(enabledAccounts);
    } catch (err) {
      console.error('Failed to load status:', err);
      setError('Failed to load automation status');
    } finally {
      setLoading(false);
    }
  };

  const loadM3uAccounts = async () => {
    try {
      const response = await m3uAPI.getAccounts();
      setM3uAccounts(response.data || []);
    } catch (err) {
      console.error('Failed to load M3U accounts:', err);
      // Non-critical error, don't show to user
    }
  };



  const handleRefreshPlaylist = async () => {
    try {
      setActionLoading('playlist');
      await streamAPI.refreshPlaylist();
      setSuccess('Playlist refresh initiated successfully');
      await loadStatus();
    } catch (err) {
      setError('Failed to refresh playlist');
    } finally {
      setActionLoading('');
    }
  };

  const handleDiscoverStreams = async () => {
    try {
      setActionLoading('discover');
      const response = await streamAPI.discoverStreams();
      setSuccess(`Stream discovery completed. ${response.data.total_assigned} streams assigned.`);
      await loadStatus();
    } catch (err) {
      setError('Failed to discover streams');
    } finally {
      setActionLoading('');
    }
  };

  const handleRunCycle = async () => {
    try {
      setActionLoading('cycle');
      await automationAPI.runCycle();
      setSuccess('Automation cycle completed successfully');
      await loadStatus();
    } catch (err) {
      setError('Failed to run automation cycle');
    } finally {
      setActionLoading('');
    }
  };

  const handleAccountToggle = async (accountId) => {
    try {
      const newSelectedAccounts = selectedAccounts.includes(accountId)
        ? selectedAccounts.filter(id => id !== accountId)
        : [...selectedAccounts, accountId];
      
      setSelectedAccounts(newSelectedAccounts);
      
      // Update config with new selection
      const updatedConfig = {
        ...status.config,
        enabled_m3u_accounts: newSelectedAccounts
      };
      
      await automationAPI.updateConfig(updatedConfig);
      setSuccess('M3U account selection updated');
      await loadStatus();
    } catch (err) {
      setError('Failed to update M3U account selection');
      // Revert on error
      await loadStatus();
    }
  };

  const formatDateTime = (dateString) => {
    if (!dateString) return 'Never';
    return new Date(dateString).toLocaleString();
  };

  const formatCronSchedule = (schedule) => {
    if (!schedule) return 'Not configured';
    
    // If cron_expression is available, try to parse it for a human-readable format
    if (schedule.cron_expression) {
      const cronExpr = schedule.cron_expression;
      // Common cron patterns
      if (cronExpr === '0 * * * *') return 'Hourly';
      if (cronExpr.match(/^0 \d{1,2} \* \* \*$/)) {
        const hour = cronExpr.split(' ')[1];
        const hourStr = String(hour).padStart(2, '0');
        return `Daily at ${hourStr}:00`;
      }
      if (cronExpr.match(/^\d{1,2} \d{1,2} \* \* \*$/)) {
        const [minute, hour] = cronExpr.split(' ');
        const hourStr = String(hour).padStart(2, '0');
        const minuteStr = String(minute).padStart(2, '0');
        return `Daily at ${hourStr}:${minuteStr}`;
      }
      if (cronExpr.match(/^\d{1,2} \d{1,2} \d{1,2} \* \*$/)) {
        const [minute, hour, day] = cronExpr.split(' ');
        const hourStr = String(hour).padStart(2, '0');
        const minuteStr = String(minute).padStart(2, '0');
        return `Monthly on day ${day} at ${hourStr}:${minuteStr}`;
      }
      // For other patterns, show the cron expression
      return `Cron: ${cronExpr}`;
    }
    
    // Fallback to legacy format (frequency, hour, minute)
    const freq = schedule.frequency || 'daily';
    const hour = schedule.hour ?? 3;
    const minute = schedule.minute ?? 0;
    const hourStr = String(hour).padStart(2, '0');
    const minuteStr = String(minute).padStart(2, '0');
    
    if (freq === 'monthly') {
      const day = schedule.day_of_month || 1;
      return `Monthly on day ${day} at ${hourStr}:${minuteStr}`;
    }
    return `Daily at ${hourStr}:${minuteStr}`;
  };

  const formatActivityDetails = (entry) => {
    const details = entry.details || {};
    const parts = [];

    // Format different types of activity details
    if (details.success !== undefined) {
      parts.push(details.success ? '✓ Success' : '✗ Failed');
    }
    if (details.total_assigned) {
      parts.push(`${details.total_assigned} streams assigned`);
    }
    if (details.added_count) {
      parts.push(`+${details.added_count} added`);
    }
    if (details.removed_count) {
      parts.push(`-${details.removed_count} removed`);
    }
    if (details.channel_count) {
      parts.push(`${details.channel_count} channels`);
    }
    if (details.error) {
      parts.push(`Error: ${details.error}`);
    }

    return parts.length > 0 ? parts.join(' • ') : '';
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" height="400px">
        <CircularProgress />
      </Box>
    );
  }

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Dashboard
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError('')}>
          {error}
        </Alert>
      )}

      {success && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSuccess('')}>
          {success}
        </Alert>
      )}

      <Grid container spacing={2}>
        {/* Automation Status Card */}
        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Automation Status
              </Typography>
              <Box display="flex" alignItems="center" gap={2} mb={1}>
                <Chip
                  label={status?.running ? 'Running' : 'Stopped'}
                  color={status?.running ? 'success' : 'default'}
                />
              </Box>
              <Typography variant="body2" color="text.secondary">
                Last Playlist Update: {formatDateTime(status?.last_playlist_update)}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Next Playlist Update: {formatDateTime(status?.next_playlist_update)}
              </Typography>
            </CardContent>
          </Card>
        </Grid>

        {/* Quick Actions Card */}
        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Quick Actions
              </Typography>
              <Box display="flex" flexDirection="column" gap={2}>
                <Button
                  variant="outlined"
                  onClick={handleRefreshPlaylist}
                  disabled={actionLoading === 'playlist'}
                  startIcon={
                    actionLoading === 'playlist' ? (
                      <CircularProgress size={20} />
                    ) : (
                      <RefreshIcon />
                    )
                  }
                  fullWidth
                >
                  Refresh M3U Playlist
                </Button>
                <Button
                  variant="outlined"
                  onClick={handleDiscoverStreams}
                  disabled={actionLoading === 'discover'}
                  startIcon={
                    actionLoading === 'discover' ? (
                      <CircularProgress size={20} />
                    ) : (
                      <DiscoverIcon />
                    )
                  }
                  fullWidth
                >
                  Discover & Assign Streams
                </Button>
                <Button
                  variant="outlined"
                  onClick={handleRunCycle}
                  disabled={actionLoading === 'cycle'}
                  startIcon={
                    actionLoading === 'cycle' ? <CircularProgress size={20} /> : <StartIcon />
                  }
                  fullWidth
                >
                  Run Automation Cycle
                </Button>
              </Box>
            </CardContent>
          </Card>
        </Grid>

        {/* System Overview Stats */}
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                System Overview
              </Typography>
              <Grid container spacing={2}>
                <Grid item xs={6} sm={3}>
                  <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                    <ScheduleIcon color="action" fontSize="small" />
                    <Typography variant="body2" color="text.secondary">
                      Update Interval
                    </Typography>
                  </Box>
                  <Typography variant="h5">
                    {status?.config?.playlist_update_interval_minutes || 5}m
                  </Typography>
                </Grid>
                <Grid item xs={6} sm={3}>
                  <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                    <CheckIcon color="success" fontSize="small" />
                    <Typography variant="body2" color="text.secondary">
                      Status
                    </Typography>
                  </Box>
                  <Chip
                    label={status?.running ? 'Running' : 'Stopped'}
                    color={status?.running ? 'success' : 'default'}
                    size="small"
                  />
                </Grid>
                <Grid item xs={6} sm={3}>
                  <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                    <TrendingIcon color="action" fontSize="small" />
                    <Typography variant="body2" color="text.secondary">
                      Recent Activity
                    </Typography>
                  </Box>
                  <Typography variant="h5">
                    {status?.recent_changelog?.length || 0}
                  </Typography>
                </Grid>
                <Grid item xs={6} sm={3}>
                  <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                    <RefreshIcon color="action" fontSize="small" />
                    <Typography variant="body2" color="text.secondary">
                      Last Update
                    </Typography>
                  </Box>
                  <Typography variant="body2">
                    {status?.last_playlist_update 
                      ? new Date(status.last_playlist_update).toLocaleTimeString()
                      : 'Never'}
                  </Typography>
                </Grid>
              </Grid>
            </CardContent>
          </Card>
        </Grid>

        {/* Configuration Overview Card */}
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Configuration
              </Typography>
              <Grid container spacing={2}>
                <Grid item xs={12} sm={6} md={4}>
                  <Typography variant="body2" color="text.secondary">
                    Playlist Update Interval
                  </Typography>
                  <Typography variant="body1">
                    {status?.config?.playlist_update_interval_minutes || 5} minutes
                  </Typography>
                </Grid>
                <Grid item xs={12} sm={6} md={4}>
                  <Typography variant="body2" color="text.secondary">
                    Global Check Schedule
                  </Typography>
                  <Typography variant="body1">
                    {formatCronSchedule(streamCheckerStatus?.config?.global_check_schedule)}
                  </Typography>
                </Grid>
                <Grid item xs={12} sm={6} md={4}>
                  <Typography variant="body2" color="text.secondary">
                    Enabled Features
                  </Typography>
                  <Typography variant="body1">
                    {Object.entries(status?.config?.enabled_features || {})
                      .filter(([_, enabled]) => enabled)
                      .map(([feature, _]) => feature.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()))
                      .join(', ') || 'None'}
                  </Typography>
                </Grid>
              </Grid>
            </CardContent>
          </Card>
        </Grid>

        {/* M3U Account Selection Card */}
        {m3uAccounts.length > 0 && (
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  M3U Playlists
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                  Select which M3U accounts/playlists to include in the stream fetch pipeline. 
                  {selectedAccounts.length === 0 && ' (All accounts enabled when none selected)'}
                </Typography>
                <FormGroup>
                  {m3uAccounts.map((account) => (
                    <FormControlLabel
                      key={account.id}
                      control={
                        <Checkbox
                          checked={selectedAccounts.length === 0 || selectedAccounts.includes(account.id)}
                          onChange={() => handleAccountToggle(account.id)}
                          disabled={actionLoading !== ''}
                        />
                      }
                      label={`${account.name || `Account ${account.id}`} - ${account.url || 'No URL'}`}
                    />
                  ))}
                </FormGroup>
              </CardContent>
            </Card>
          </Grid>
        )}

        {/* Recent Activity Card */}
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Recent Activity
              </Typography>
              {status?.recent_changelog?.length > 0 ? (
                <Box>
                  {status.recent_changelog.slice(0, 10).map((entry, index) => (
                    <Box key={index}>
                      {index > 0 && <Divider sx={{ my: 1.5 }} />}
                      <Box display="flex" alignItems="flex-start" gap={1}>
                        <Box sx={{ minWidth: 0, flex: 1 }}>
                          <Typography variant="body2" fontWeight="medium">
                            {entry.action.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}
                          </Typography>
                          <Typography variant="caption" color="text.secondary" display="block">
                            {formatDateTime(entry.timestamp)}
                          </Typography>
                          {formatActivityDetails(entry) && (
                            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                              {formatActivityDetails(entry)}
                            </Typography>
                          )}
                        </Box>
                      </Box>
                    </Box>
                  ))}
                </Box>
              ) : (
                <Typography variant="body2" color="text.secondary">
                  No recent activity
                </Typography>
              )}
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}

export default Dashboard;
import React, { useState, useEffect } from 'react';
import {
  Box,
  Typography,
  Card,
  CardContent,
  TextField,
  FormControlLabel,
  Switch,
  Button,
  Alert,
  CircularProgress,
  Grid,
  RadioGroup,
  Radio,
  FormControl
} from '@mui/material';
import { automationAPI, streamCheckerAPI } from '../services/api';

function AutomationSettings() {
  const [config, setConfig] = useState(null);
  const [streamCheckerConfig, setStreamCheckerConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const [automationResponse, streamCheckerResponse] = await Promise.all([
        automationAPI.getConfig(),
        streamCheckerAPI.getConfig()
      ]);
      setConfig(automationResponse.data);
      setStreamCheckerConfig(streamCheckerResponse.data);
    } catch (err) {
      console.error('Failed to load config:', err);
      setError('Failed to load automation configuration');
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      await Promise.all([
        automationAPI.updateConfig(config),
        streamCheckerAPI.updateConfig(streamCheckerConfig)
      ]);
      setSuccess('Configuration saved successfully');
    } catch (err) {
      setError('Failed to save configuration');
    } finally {
      setSaving(false);
    }
  };

  const handleConfigChange = (field, value) => {
    if (field.includes('.')) {
      const [parent, child] = field.split('.');
      setConfig(prev => ({
        ...prev,
        [parent]: {
          ...prev[parent],
          [child]: value
        }
      }));
    } else {
      setConfig(prev => ({
        ...prev,
        [field]: value
      }));
    }
  };

  const handleStreamCheckerConfigChange = (field, value) => {
    if (field.includes('.')) {
      const parts = field.split('.');
      if (parts.length === 2) {
        const [parent, child] = parts;
        setStreamCheckerConfig(prev => ({
          ...prev,
          [parent]: {
            ...(prev[parent] || {}),
            [child]: value
          }
        }));
      } else if (parts.length === 3) {
        const [parent, child, grandchild] = parts;
        setStreamCheckerConfig(prev => ({
          ...prev,
          [parent]: {
            ...(prev[parent] || {}),
            [child]: {
              ...(prev[parent]?.[child] || {}),
              [grandchild]: value
            }
          }
        }));
      }
    } else {
      setStreamCheckerConfig(prev => ({
        ...prev,
        [field]: value
      }));
    }
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" height="400px">
        <CircularProgress />
      </Box>
    );
  }

  if (!config || !streamCheckerConfig) {
    return (
      <Alert severity="error">
        Failed to load configuration
      </Alert>
    );
  }

  const pipelineMode = streamCheckerConfig?.pipeline_mode;
  
  // Determine which settings to show based on pipeline mode
  const showScheduleSettings = ['pipeline_1_5', 'pipeline_2_5', 'pipeline_3'].includes(pipelineMode);
  const showUpdateInterval = ['pipeline_1', 'pipeline_1_5', 'pipeline_2', 'pipeline_2_5'].includes(pipelineMode);

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Configuration
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
        {/* Pipeline Selection */}
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h5" gutterBottom>
                Pipeline Selection
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
                Select the automation pipeline that best fits your needs. Each pipeline determines when and how streams are checked.
              </Typography>
              
              <FormControl component="fieldset" fullWidth>
                <RadioGroup
                  value={pipelineMode}
                  onChange={(e) => handleStreamCheckerConfigChange('pipeline_mode', e.target.value)}
                >
                  <Card variant="outlined" sx={{ mb: 2, border: pipelineMode === 'disabled' ? 2 : 1, borderColor: pipelineMode === 'disabled' ? 'error.main' : 'divider' }}>
                    <CardContent>
                      <FormControlLabel 
                        value="disabled" 
                        control={<Radio />} 
                        label={
                          <Box>
                            <Typography variant="h6">Disabled</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                              Features:
                            </Typography>
                            <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                              <li><Typography variant="body2" color="text.secondary">Complete automation system disabled</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">No automatic updates, matching, or checking</Typography></li>
                            </Box>
                          </Box>
                        }
                        sx={{ alignItems: 'flex-start' }}
                      />
                    </CardContent>
                  </Card>

                  <Card variant="outlined" sx={{ mb: 2, border: pipelineMode === 'pipeline_1' ? 2 : 1, borderColor: pipelineMode === 'pipeline_1' ? 'primary.main' : 'divider' }}>
                    <CardContent>
                      <FormControlLabel 
                        value="pipeline_1" 
                        control={<Radio />} 
                        label={
                          <Box>
                            <Typography variant="h6">Pipeline 1: Update → Match → Check (with 2hr immunity)</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                              Features:
                            </Typography>
                            <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                              <li><Typography variant="body2" color="text.secondary">Automatic M3U updates</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Stream matching</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Quality checking with 2-hour immunity</Typography></li>
                            </Box>
                          </Box>
                        }
                        sx={{ alignItems: 'flex-start' }}
                      />
                    </CardContent>
                  </Card>

                  <Card variant="outlined" sx={{ mb: 2, border: pipelineMode === 'pipeline_1_5' ? 2 : 1, borderColor: pipelineMode === 'pipeline_1_5' ? 'primary.main' : 'divider' }}>
                    <CardContent>
                      <FormControlLabel 
                        value="pipeline_1_5" 
                        control={<Radio />} 
                        label={
                          <Box>
                            <Typography variant="h6">Pipeline 1.5: Pipeline 1 + Scheduled Global Action</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                              Features:
                            </Typography>
                            <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                              <li><Typography variant="body2" color="text.secondary">Automatic M3U updates</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Stream matching</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Quality checking with 2-hour immunity</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Scheduled Global Action (daily/monthly)</Typography></li>
                            </Box>
                          </Box>
                        }
                        sx={{ alignItems: 'flex-start' }}
                      />
                    </CardContent>
                  </Card>

                  <Card variant="outlined" sx={{ mb: 2, border: pipelineMode === 'pipeline_2' ? 2 : 1, borderColor: pipelineMode === 'pipeline_2' ? 'primary.main' : 'divider' }}>
                    <CardContent>
                      <FormControlLabel 
                        value="pipeline_2" 
                        control={<Radio />} 
                        label={
                          <Box>
                            <Typography variant="h6">Pipeline 2: Update → Match only (no automatic checking)</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                              Features:
                            </Typography>
                            <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                              <li><Typography variant="body2" color="text.secondary">Automatic M3U updates</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Stream matching</Typography></li>
                            </Box>
                          </Box>
                        }
                        sx={{ alignItems: 'flex-start' }}
                      />
                    </CardContent>
                  </Card>

                  <Card variant="outlined" sx={{ mb: 2, border: pipelineMode === 'pipeline_2_5' ? 2 : 1, borderColor: pipelineMode === 'pipeline_2_5' ? 'primary.main' : 'divider' }}>
                    <CardContent>
                      <FormControlLabel 
                        value="pipeline_2_5" 
                        control={<Radio />} 
                        label={
                          <Box>
                            <Typography variant="h6">Pipeline 2.5: Pipeline 2 + Scheduled Global Action</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                              Features:
                            </Typography>
                            <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                              <li><Typography variant="body2" color="text.secondary">Automatic M3U updates</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Stream matching</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">Scheduled Global Action (daily/monthly)</Typography></li>
                            </Box>
                          </Box>
                        }
                        sx={{ alignItems: 'flex-start' }}
                      />
                    </CardContent>
                  </Card>

                  <Card variant="outlined" sx={{ mb: 2, border: pipelineMode === 'pipeline_3' ? 2 : 1, borderColor: pipelineMode === 'pipeline_3' ? 'primary.main' : 'divider' }}>
                    <CardContent>
                      <FormControlLabel 
                        value="pipeline_3" 
                        control={<Radio />} 
                        label={
                          <Box>
                            <Typography variant="h6">Pipeline 3: Only Scheduled Global Action</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                              Features:
                            </Typography>
                            <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                              <li><Typography variant="body2" color="text.secondary">Scheduled Global Action ONLY (daily/monthly)</Typography></li>
                              <li><Typography variant="body2" color="text.secondary">NO automatic updates or checking</Typography></li>
                            </Box>
                          </Box>
                        }
                        sx={{ alignItems: 'flex-start' }}
                      />
                    </CardContent>
                  </Card>
                </RadioGroup>
              </FormControl>
            </CardContent>
          </Card>
        </Grid>
        
        {/* No Active Pipeline Card - Show when no pipeline is selected */}
        {!pipelineMode && (
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Alert severity="warning">
                  <Typography variant="h6" gutterBottom>
                    No Active Pipeline
                  </Typography>
                  <Typography variant="body2">
                    Please select a pipeline above to configure automation settings. All configuration options will appear once a pipeline is selected.
                  </Typography>
                </Alert>
              </CardContent>
            </Card>
          </Grid>
        )}

        {/* General Settings - Only show for pipelines that have automatic updates */}
        {showUpdateInterval && (
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Update Interval
                </Typography>
                
                <TextField
                  label="Playlist Update Interval (minutes)"
                  type="number"
                  value={config.playlist_update_interval_minutes || 5}
                  onChange={(e) => handleConfigChange('playlist_update_interval_minutes', parseInt(e.target.value))}
                  fullWidth
                  margin="normal"
                  helperText="How often to check for playlist updates"
                  inputProps={{ min: 1, max: 1440 }}
                />
              </CardContent>
            </Card>
          </Grid>
        )}

        {/* Stream Checker Service */}
        {pipelineMode && pipelineMode !== 'disabled' && (
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Stream Checker Service
                </Typography>
                
                <Alert severity="info">
                  The stream checker service automatically starts when the application launches with a pipeline other than "Disabled" selected.
                </Alert>
              </CardContent>
            </Card>
          </Grid>
        )}
        
        {/* Disabled Mode Warning */}
        {pipelineMode === 'disabled' && (
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Alert severity="warning">
                  <Typography variant="h6" gutterBottom>
                    Automation System Disabled
                  </Typography>
                  <Typography variant="body2">
                    The complete automation system is currently disabled. No automatic updates, stream matching, or quality checking will occur. Select a pipeline above to enable automation.
                  </Typography>
                </Alert>
              </CardContent>
            </Card>
          </Grid>
        )}

        {/* Global Check Schedule - Only show for pipelines that have scheduled actions */}
        {showScheduleSettings && (
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Global Check Schedule
                </Typography>
                
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                  Configure when the scheduled Global Action runs. This performs a complete cycle: Updates all M3U playlists, matches all streams, and checks ALL channels (bypassing the 2-hour immunity).
                </Typography>
                
                <TextField
                  label="Cron Expression"
                  value={streamCheckerConfig.global_check_schedule?.cron_expression ?? '0 3 * * *'}
                  onChange={(e) => handleStreamCheckerConfigChange('global_check_schedule.cron_expression', e.target.value)}
                  fullWidth
                  margin="normal"
                  helperText="Enter a cron expression (e.g., '0 3 * * *' for daily at 3:00 AM, '0 3 1 * *' for monthly on the 1st at 3:00 AM)"
                  placeholder="0 3 * * *"
                />
                
                <Alert severity="info" sx={{ mt: 2 }}>
                  <Typography variant="body2" sx={{ fontWeight: 'bold', mb: 1 }}>
                    Cron Expression Format: minute hour day month weekday
                  </Typography>
                  <Typography variant="body2" component="div">
                    Common examples:
                    <ul style={{ marginTop: '8px', marginBottom: '0' }}>
                      <li><code>0 3 * * *</code> - Every day at 3:00 AM</li>
                      <li><code>30 2 * * *</code> - Every day at 2:30 AM</li>
                      <li><code>0 3 1 * *</code> - Monthly on the 1st at 3:00 AM</li>
                      <li><code>0 0 * * 0</code> - Every Sunday at midnight</li>
                      <li><code>0 */6 * * *</code> - Every 6 hours</li>
                    </ul>
                  </Typography>
                </Alert>
              </CardContent>
            </Card>
          </Grid>
        )}



        {/* Stream Analysis Settings - Only show when a pipeline is selected */}
        {pipelineMode && (
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Stream Analysis Settings
                </Typography>
              
              <TextField
                label="FFmpeg Duration (seconds)"
                type="number"
                value={streamCheckerConfig.stream_analysis?.ffmpeg_duration ?? 30}
                onChange={(e) => handleStreamCheckerConfigChange('stream_analysis.ffmpeg_duration', parseInt(e.target.value))}
                fullWidth
                margin="normal"
                helperText="Duration to analyze each stream"
                inputProps={{ min: 5, max: 120 }}
              />
              
              <TextField
                label="FFmpeg/FFprobe User Agent"
                type="text"
                value={streamCheckerConfig.stream_analysis?.user_agent ?? 'VLC/3.0.14'}
                onChange={(e) => handleStreamCheckerConfigChange('stream_analysis.user_agent', e.target.value)}
                fullWidth
                margin="normal"
                helperText="User agent string for ffmpeg/ffprobe (for strict stream providers)"
                inputProps={{ maxLength: 200 }}
              />
              
              <TextField
                label="Timeout (seconds)"
                type="number"
                value={streamCheckerConfig.stream_analysis?.timeout ?? 30}
                onChange={(e) => handleStreamCheckerConfigChange('stream_analysis.timeout', parseInt(e.target.value))}
                fullWidth
                margin="normal"
                helperText="Timeout for stream analysis operations"
                inputProps={{ min: 10, max: 300 }}
              />
              
              <TextField
                label="Retry Attempts"
                type="number"
                value={streamCheckerConfig.stream_analysis?.retries ?? 1}
                onChange={(e) => handleStreamCheckerConfigChange('stream_analysis.retries', parseInt(e.target.value))}
                fullWidth
                margin="normal"
                helperText="Number of retry attempts for failed checks"
                inputProps={{ min: 0, max: 5 }}
              />
              
              <TextField
                label="Retry Delay (seconds)"
                type="number"
                value={streamCheckerConfig.stream_analysis?.retry_delay ?? 10}
                onChange={(e) => handleStreamCheckerConfigChange('stream_analysis.retry_delay', parseInt(e.target.value))}
                fullWidth
                margin="normal"
                helperText="Delay between retry attempts"
                inputProps={{ min: 5, max: 60 }}
              />
            </CardContent>
          </Card>
          </Grid>
        )}

        {/* Queue Settings - Only show when a pipeline is selected */}
        {pipelineMode && (
          <Grid item xs={12} md={6}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Queue Settings
                </Typography>
              
              <TextField
                label="Maximum Queue Size"
                type="number"
                value={streamCheckerConfig.queue?.max_size ?? 1000}
                onChange={(e) => handleStreamCheckerConfigChange('queue.max_size', parseInt(e.target.value))}
                fullWidth
                margin="normal"
                helperText="Maximum number of channels in the checking queue"
                inputProps={{ min: 10, max: 10000 }}
              />
              
              <TextField
                label="Max Channels Per Run"
                type="number"
                value={streamCheckerConfig.queue?.max_channels_per_run ?? 50}
                onChange={(e) => handleStreamCheckerConfigChange('queue.max_channels_per_run', parseInt(e.target.value))}
                fullWidth
                margin="normal"
                helperText="Maximum channels to check in a single run"
                inputProps={{ min: 1, max: 500 }}
              />
              
              <FormControlLabel
                control={
                  <Switch
                    checked={streamCheckerConfig.queue?.check_on_update !== false}
                    onChange={(e) => handleStreamCheckerConfigChange('queue.check_on_update', e.target.checked)}
                  />
                }
                label="Check Channels on M3U Update"
                sx={{ mt: 2 }}
              />
              
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                Automatically queue channels for checking when they receive M3U playlist updates.
              </Typography>
            </CardContent>
          </Card>
          </Grid>
        )}
      </Grid>

      <Box sx={{ mt: 2, display: 'flex', justifyContent: 'flex-end' }}>
        <Button
          variant="contained"
          onClick={handleSave}
          disabled={saving}
          size="large"
        >
          {saving ? <CircularProgress size={20} /> : 'Save Settings'}
        </Button>
      </Box>
    </Box>
  );
}

export default AutomationSettings;
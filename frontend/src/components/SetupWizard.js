import React, { useState, useEffect } from 'react';
import {
  Box,
  Stepper,
  Step,
  StepLabel,
  StepContent,
  Button,
  Typography,
  Card,
  CardContent,
  Alert,
  CircularProgress,
  TextField,
  FormGroup,
  FormControlLabel,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  IconButton,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  RadioGroup,
  Radio,
  FormControl
} from '@mui/material';
import {
  Add as AddIcon,
  Delete as DeleteIcon,
  Edit as EditIcon
} from '@mui/icons-material';
import { setupAPI, automationAPI, channelsAPI, regexAPI, dispatcharrAPI, streamCheckerAPI, m3uAPI } from '../services/api';

const steps = [
  {
    label: 'Check Dispatcharr Connection',
    description: 'Verify connection to Dispatcharr API and load channels',
  },
  {
    label: 'Configure Channel Patterns',
    description: 'Set up regex patterns for automatic stream assignment to channels',
  },
  {
    label: 'Configure Automation Settings',
    description: 'Set up automation intervals and preferences',
  },
  {
    label: 'Setup Complete',
    description: 'Your automated stream manager is ready to use',
  },
];

function SetupWizard({ onComplete, setupStatus: initialSetupStatus }) {
  const [activeStep, setActiveStep] = useState(0);
  const [setupStatus, setSetupStatus] = useState(initialSetupStatus);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [config, setConfig] = useState({
    playlist_update_interval_minutes: 5,
    global_check_interval_hours: 24,
    concurrent_stream_checks: 2,
    enabled_m3u_accounts: [],
    autostart_automation: false,
    enabled_features: {
      auto_playlist_update: true,
      auto_stream_discovery: true,
      auto_quality_reordering: true,
      changelog_tracking: true
    }
  });
  
  // Stream checker config for global check schedule
  const [streamCheckerConfig, setStreamCheckerConfig] = useState({
    pipeline_mode: 'pipeline_1_5',
    global_check_schedule: {
      enabled: true,
      cron_expression: '0 3 * * *',
      frequency: 'daily',
      hour: 3,
      minute: 0,
      day_of_month: 1
    },
    queue: {
      check_on_update: true
    }
  });

  // Dispatcharr configuration state
  const [dispatcharrConfig, setDispatcharrConfig] = useState({
    base_url: '',
    username: '',
    password: '',
    has_password: false
  });
  const [connectionTestResult, setConnectionTestResult] = useState(null);

  // Channel configuration state
  const [channels, setChannels] = useState([]);
  const [patterns, setPatterns] = useState({});
  const [openPatternDialog, setOpenPatternDialog] = useState(false);
  const [editingChannel, setEditingChannel] = useState(null);
  const [patternFormData, setPatternFormData] = useState({
    channel_id: '',
    name: '',
    regex: [''],
    enabled: true
  });

  // M3U accounts state
  const [m3uAccounts, setM3uAccounts] = useState([]);
  const [selectedM3uAccounts, setSelectedM3uAccounts] = useState([]);

  useEffect(() => {
    if (initialSetupStatus) {
      setSetupStatus(initialSetupStatus);
      // Determine starting step based on current status
      if (initialSetupStatus.setup_complete) {
        setActiveStep(3); // Go to completion step only if setup is complete
      } else if (initialSetupStatus.dispatcharr_connection && initialSetupStatus.has_channels) {
        if (initialSetupStatus.has_patterns) {
          if (initialSetupStatus.automation_config_exists) {
            setActiveStep(2); // Go to automation settings step
          } else {
            setActiveStep(2); // Go to automation settings step
          }
        } else {
          setActiveStep(1); // Go to channel patterns step
        }
      } else {
        setActiveStep(0); // Stay at connection check
      }
    }
    
    // Load Dispatcharr configuration
    loadDispatcharrConfig();
  }, [initialSetupStatus]);

  // Update enabled features based on pipeline mode
  useEffect(() => {
    const pipelineMode = streamCheckerConfig.pipeline_mode;
    const hasAutoUpdates = ['pipeline_1', 'pipeline_1_5', 'pipeline_2', 'pipeline_2_5'].includes(pipelineMode);
    const hasAutoChecking = ['pipeline_1', 'pipeline_1_5'].includes(pipelineMode);
    
    setConfig(prev => ({
      ...prev,
      enabled_features: {
        auto_playlist_update: hasAutoUpdates,
        auto_stream_discovery: hasAutoUpdates,
        auto_quality_reordering: hasAutoChecking,
        changelog_tracking: true
      }
    }));
  }, [streamCheckerConfig.pipeline_mode]);

  const loadDispatcharrConfig = async () => {
    try {
      const response = await dispatcharrAPI.getConfig();
      setDispatcharrConfig(response.data);
    } catch (err) {
      console.error('Failed to load Dispatcharr config:', err);
    }
  };

  const refreshSetupStatus = async () => {
    try {
      setLoading(true);
      const response = await setupAPI.getStatus();
      setSetupStatus(response.data);
      
      // Determine starting step based on current status
      if (response.data.setup_complete) {
        setActiveStep(3); // Go to completion step only if setup is complete
      } else if (response.data.dispatcharr_connection && response.data.has_channels) {
        if (response.data.has_patterns) {
          if (response.data.automation_config_exists) {
            setActiveStep(2); // Go to automation settings step
          } else {
            setActiveStep(2); // Go to automation settings step
          }
        } else {
          setActiveStep(1); // Go to channel patterns step
        }
      } else {
        setActiveStep(0); // Stay at connection check
      }
    } catch (err) {
      console.error('Failed to refresh setup status:', err);
      setError('Failed to refresh setup status');
    } finally {
      setLoading(false);
    }
  };

  const handleNext = () => {
    setActiveStep((prevActiveStep) => prevActiveStep + 1);
  };

  const handleBack = () => {
    setActiveStep((prevActiveStep) => prevActiveStep - 1);
  };

  const handleConfigSave = async () => {
    try {
      setLoading(true);
      // Save both automation config and stream checker config
      await Promise.all([
        automationAPI.updateConfig(config),
        streamCheckerAPI.updateConfig(streamCheckerConfig)
      ]);
      
      // After saving config, refresh setup status to check if we can proceed
      await refreshSetupStatus();
      
      // Only move to next step if setup is actually complete now
      const statusResponse = await setupAPI.getStatus();
      if (statusResponse.data.setup_complete) {
        setActiveStep(3); // Go to completion step
      } else {
        // If not complete, show what's missing
        const missing = [];
        if (!statusResponse.data.dispatcharr_connection) missing.push('Dispatcharr connection');
        if (!statusResponse.data.has_channels) missing.push('channels');
        if (!statusResponse.data.has_patterns) missing.push('regex patterns');
        
        setError(`Setup not complete. Still missing: ${missing.join(', ')}`);
      }
    } catch (err) {
      setError('Failed to save configuration');
    } finally {
      setLoading(false);
    }
  };

  const handleComplete = async () => {
    try {
      // First refresh setup status to make sure it's actually complete
      setLoading(true);
      const response = await setupAPI.getStatus();
      
      if (response.data.setup_complete) {
        // Setup is complete, proceed to dashboard
        onComplete();
      } else {
        // Setup is not complete, show error and refresh status
        setError('Setup is not yet complete. Please ensure all steps are properly configured.');
        setSetupStatus(response.data);
        
        // Set the correct step based on current status
        if (response.data.dispatcharr_connection && response.data.has_channels) {
          if (response.data.has_patterns) {
            if (response.data.automation_config_exists) {
              setActiveStep(2); // Go to automation settings step
            } else {
              setActiveStep(2); // Go to automation settings step
            }
          } else {
            setActiveStep(1); // Go to channel patterns step
          }
        } else {
          setActiveStep(0); // Stay at connection check
        }
      }
    } catch (err) {
      console.error('Failed to verify setup completion:', err);
      setError('Failed to verify setup completion');
    } finally {
      setLoading(false);
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
            ...prev[parent],
            [child]: value
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

  // Channel configuration functions
  const loadChannelsAndPatterns = async () => {
    try {
      setLoading(true);
      const [channelsResponse, patternsResponse, m3uResponse] = await Promise.all([
        channelsAPI.getChannels(),
        regexAPI.getPatterns(),
        m3uAPI.getAccounts().catch(() => ({ data: [] })) // Non-critical, provide empty array on error
      ]);
      
      setChannels(channelsResponse.data);
      setPatterns(patternsResponse.data);
      setM3uAccounts(m3uResponse.data || []);
    } catch (err) {
      console.error('Failed to load channels and patterns:', err);
      setError('Failed to load channel data');
    } finally {
      setLoading(false);
    }
  };

  const handleOpenPatternDialog = (channel = null) => {
    if (channel) {
      const existingPattern = patterns.patterns?.[channel.id];
      setEditingChannel(channel.id);
      setPatternFormData({
        channel_id: channel.id,
        name: existingPattern?.name || channel.name,
        regex: existingPattern?.regex || [''],
        enabled: existingPattern?.enabled !== false
      });
    } else {
      setEditingChannel(null);
      setPatternFormData({
        channel_id: '',
        name: '',
        regex: [''],
        enabled: true
      });
    }
    setOpenPatternDialog(true);
  };

  const handleClosePatternDialog = () => {
    setOpenPatternDialog(false);
    setEditingChannel(null);
    setPatternFormData({
      channel_id: '',
      name: '',
      regex: [''],
      enabled: true
    });
  };

  const handleSavePattern = async () => {
    try {
      setLoading(true);
      await regexAPI.addPattern(patternFormData);
      await loadChannelsAndPatterns();
      handleClosePatternDialog();
    } catch (err) {
      setError('Failed to save pattern: ' + (err.response?.data?.error || err.message));
    } finally {
      setLoading(false);
    }
  };

  const addRegexField = () => {
    setPatternFormData(prev => ({
      ...prev,
      regex: [...prev.regex, '']
    }));
  };

  const updateRegexField = (index, value) => {
    setPatternFormData(prev => ({
      ...prev,
      regex: prev.regex.map((r, i) => i === index ? value : r)
    }));
  };

  const removeRegexField = (index) => {
    setPatternFormData(prev => ({
      ...prev,
      regex: prev.regex.filter((_, i) => i !== index)
    }));
  };

  const handlePatternsNext = async () => {
    // Check if we have at least some patterns configured
    const statusResponse = await setupAPI.getStatus();
    if (statusResponse.data.has_patterns) {
      handleNext();
    } else {
      setError('Please configure at least one channel pattern before proceeding.');
    }
  };

  const handleImportJSON = async (event) => {
    const file = event.target.files[0];
    if (!file) return;

    try {
      setLoading(true);
      setError('');

      // Read the file
      const text = await file.text();
      let jsonData;
      
      try {
        jsonData = JSON.parse(text);
      } catch (parseError) {
        setError('Invalid JSON file: ' + parseError.message);
        return;
      }

      // Validate JSON structure
      if (!jsonData.patterns) {
        setError('Invalid JSON structure: missing "patterns" field');
        return;
      }

      // Import the patterns
      await regexAPI.importPatterns(jsonData);
      
      // Reload patterns to show the imported data
      await loadChannelsAndPatterns();
      
      // Show success message
      setError('');
      alert(`Successfully imported ${Object.keys(jsonData.patterns).length} patterns`);
    } catch (err) {
      console.error('Failed to import patterns:', err);
      setError('Failed to import patterns: ' + (err.response?.data?.error || err.message));
    } finally {
      setLoading(false);
      // Clear the file input
      event.target.value = '';
    }
  };

  const handleExportJSON = () => {
    try {
      // Create a JSON blob from the patterns
      const dataStr = JSON.stringify(patterns, null, 2);
      const dataBlob = new Blob([dataStr], { type: 'application/json' });
      
      // Create a download link and trigger it
      const url = URL.createObjectURL(dataBlob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `channel_regex_config_${new Date().toISOString().split('T')[0]}.json`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Failed to export patterns:', err);
      setError('Failed to export patterns: ' + err.message);
    }
  };

  const handleDispatcharrConfigChange = (field, value) => {
    setDispatcharrConfig(prev => ({
      ...prev,
      [field]: value
    }));
    // Clear connection test result when config changes
    setConnectionTestResult(null);
  };

  const handleSaveDispatcharrConfig = async () => {
    try {
      setLoading(true);
      setError('');
      await dispatcharrAPI.updateConfig(dispatcharrConfig);
      // Refresh setup status to check connection
      await refreshSetupStatus();
      setConnectionTestResult({ success: true, message: 'Configuration saved successfully' });
    } catch (err) {
      console.error('Failed to save Dispatcharr config:', err);
      setError('Failed to save Dispatcharr configuration');
      setConnectionTestResult({ success: false, message: 'Failed to save configuration' });
    } finally {
      setLoading(false);
    }
  };

  const handleTestConnection = async () => {
    try {
      setLoading(true);
      setError('');
      const response = await dispatcharrAPI.testConnection(dispatcharrConfig);
      setConnectionTestResult(response.data);
      if (response.data.success) {
        // If test successful, refresh setup status
        await refreshSetupStatus();
      }
    } catch (err) {
      console.error('Connection test failed:', err);
      const errorMsg = err.response?.data?.error || 'Connection test failed';
      setConnectionTestResult({ success: false, error: errorMsg });
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" height="100vh">
        <CircularProgress />
        <Typography sx={{ ml: 2 }}>Loading setup configuration...</Typography>
      </Box>
    );
  }

  return (
    <Box maxWidth="md" mx="auto" p={3}>
      <Typography variant="h4" gutterBottom align="center">
        StreamFlow for Dispatcharr Setup
      </Typography>
      
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError('')}>
          {error}
        </Alert>
      )}

      <Card>
        <CardContent>
          <Stepper activeStep={activeStep} orientation="vertical">
            {steps.map((step, index) => (
              <Step key={step.label}>
                <StepLabel>{step.label}</StepLabel>
                <StepContent>
                  <Typography>{step.description}</Typography>
                  
                  {index === 0 && (
                    <Box sx={{ mt: 2 }}>
                      <Typography variant="h6" gutterBottom>
                        Dispatcharr Connection Configuration
                      </Typography>
                      <Typography variant="body2" color="text.secondary" paragraph>
                        Configure your Dispatcharr instance connection details. These settings will be saved to your environment configuration.
                      </Typography>

                      {setupStatus?.dispatcharr_connection ? (
                        <Alert severity="success" sx={{ mb: 2 }}>
                          Successfully connected to Dispatcharr
                        </Alert>
                      ) : (
                        <Alert severity="warning" sx={{ mb: 2 }}>
                          Not connected to Dispatcharr. Please configure and test your connection below.
                        </Alert>
                      )}

                      {connectionTestResult && (
                        <Alert 
                          severity={connectionTestResult.success ? "success" : "error"} 
                          sx={{ mb: 2 }}
                          onClose={() => setConnectionTestResult(null)}
                        >
                          {connectionTestResult.message || connectionTestResult.error}
                        </Alert>
                      )}
                      
                      <TextField
                        label="Dispatcharr Base URL"
                        value={dispatcharrConfig.base_url}
                        onChange={(e) => handleDispatcharrConfigChange('base_url', e.target.value)}
                        fullWidth
                        margin="normal"
                        placeholder="http://your-dispatcharr-instance.com:9191"
                        helperText="The base URL of your Dispatcharr instance (e.g., http://localhost:9191)"
                      />
                      
                      <TextField
                        label="Username"
                        value={dispatcharrConfig.username}
                        onChange={(e) => handleDispatcharrConfigChange('username', e.target.value)}
                        fullWidth
                        margin="normal"
                        placeholder="your-username"
                      />
                      
                      <TextField
                        label="Password"
                        type="password"
                        value={dispatcharrConfig.password}
                        onChange={(e) => handleDispatcharrConfigChange('password', e.target.value)}
                        fullWidth
                        margin="normal"
                        placeholder={dispatcharrConfig.has_password ? "••••••••" : "your-password"}
                        helperText={dispatcharrConfig.has_password && !dispatcharrConfig.password ? "Leave empty to keep existing password" : ""}
                      />

                      {setupStatus?.has_channels ? (
                        <Alert severity="success" sx={{ mb: 2, mt: 2 }}>
                          Channels loaded successfully
                        </Alert>
                      ) : setupStatus?.dispatcharr_connection && (
                        <Alert severity="warning" sx={{ mb: 2, mt: 2 }}>
                          No channels found. You may need to import channels first.
                        </Alert>
                      )}
                      
                      <Box sx={{ mt: 3, display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                        <Button
                          variant="outlined"
                          onClick={handleTestConnection}
                          disabled={loading || !dispatcharrConfig.base_url || !dispatcharrConfig.username}
                        >
                          {loading ? <CircularProgress size={20} /> : 'Test Connection'}
                        </Button>
                        <Button
                          variant="outlined"
                          onClick={handleSaveDispatcharrConfig}
                          disabled={loading || !dispatcharrConfig.base_url || !dispatcharrConfig.username}
                        >
                          {loading ? <CircularProgress size={20} /> : 'Save Configuration'}
                        </Button>
                        <Button
                          variant="outlined"
                          onClick={refreshSetupStatus}
                          disabled={loading}
                        >
                          {loading ? <CircularProgress size={20} /> : 'Refresh Status'}
                        </Button>
                        <Button
                          variant="contained"
                          onClick={handleNext}
                          disabled={!setupStatus?.dispatcharr_connection}
                        >
                          Continue
                        </Button>
                      </Box>
                    </Box>
                  )}

                  {index === 1 && (
                    <Box sx={{ mt: 2 }}>
                      <Typography variant="h6" gutterBottom>
                        Channel Patterns Configuration
                      </Typography>
                      <Typography variant="body2" color="text.secondary" paragraph>
                        Configure regex patterns to automatically assign new streams to channels based on stream names.
                      </Typography>
                      
                      <Box sx={{ mb: 2, display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                        <Button
                          variant="outlined"
                          onClick={loadChannelsAndPatterns}
                          disabled={loading}
                        >
                          {loading ? <CircularProgress size={20} /> : 'Load Channels'}
                        </Button>
                        <Button
                          variant="outlined"
                          component="label"
                          disabled={loading}
                        >
                          Import Patterns from JSON
                          <input
                            type="file"
                            accept="application/json,.json"
                            hidden
                            onChange={handleImportJSON}
                          />
                        </Button>
                        <Button
                          variant="outlined"
                          onClick={handleExportJSON}
                          disabled={loading || !patterns.patterns || Object.keys(patterns.patterns).length === 0}
                        >
                          Export Patterns to JSON
                        </Button>
                      </Box>

                      {channels.length > 0 && (
                        <>
                          <TableContainer component={Paper} sx={{ mb: 2 }}>
                            <Table size="small">
                              <TableHead>
                                <TableRow>
                                  <TableCell>Channel ID</TableCell>
                                  <TableCell>Channel Name</TableCell>
                                  <TableCell>Patterns Configured</TableCell>
                                  <TableCell>Actions</TableCell>
                                </TableRow>
                              </TableHead>
                              <TableBody>
                                {channels.slice(0, 10).map((channel) => { // Show first 10 channels
                                  const hasPattern = patterns.patterns?.[channel.id];
                                  return (
                                    <TableRow key={channel.id}>
                                      <TableCell>{channel.id}</TableCell>
                                      <TableCell>{channel.name}</TableCell>
                                      <TableCell>
                                        {hasPattern ? (
                                          <Typography variant="body2" color="success.main">
                                            ✓ Configured
                                          </Typography>
                                        ) : (
                                          <Typography variant="body2" color="text.secondary">
                                            Not configured
                                          </Typography>
                                        )}
                                      </TableCell>
                                      <TableCell>
                                        <IconButton
                                          size="small"
                                          onClick={() => handleOpenPatternDialog(channel)}
                                        >
                                          {hasPattern ? <EditIcon /> : <AddIcon />}
                                        </IconButton>
                                      </TableCell>
                                    </TableRow>
                                  );
                                })}
                              </TableBody>
                            </Table>
                          </TableContainer>
                          
                          {channels.length > 10 && (
                            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                              Showing first 10 channels. More channels can be configured after setup.
                            </Typography>
                          )}
                        </>
                      )}
                      
                      <Box sx={{ mt: 3, display: 'flex', gap: 1 }}>
                        <Button onClick={handleBack}>
                          Back
                        </Button>
                        <Button
                          variant="contained"
                          onClick={handlePatternsNext}
                          disabled={loading}
                        >
                          Continue
                        </Button>
                      </Box>
                    </Box>
                  )}

                  {index === 2 && (
                    <Box sx={{ mt: 2 }}>
                      <Typography variant="h6" gutterBottom>
                        Automation Configuration
                      </Typography>
                      
                      <TextField
                        label="Playlist Update Interval (minutes)"
                        type="number"
                        value={config.playlist_update_interval_minutes}
                        onChange={(e) => handleConfigChange('playlist_update_interval_minutes', parseInt(e.target.value))}
                        fullWidth
                        margin="normal"
                      />
                      
                      <TextField
                        label="Global Check Interval (hours)"
                        type="number"
                        value={config.global_check_interval_hours}
                        onChange={(e) => handleConfigChange('global_check_interval_hours', parseInt(e.target.value))}
                        fullWidth
                        margin="normal"
                      />
                      
                      <TextField
                        label="Concurrent Stream Checks"
                        type="number"
                        value={config.concurrent_stream_checks}
                        onChange={(e) => handleConfigChange('concurrent_stream_checks', parseInt(e.target.value))}
                        fullWidth
                        margin="normal"
                      />
                      
                      <Typography variant="h6" sx={{ mt: 3, mb: 2 }}>
                        Pipeline Selection
                      </Typography>
                      
                      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                        Select the automation pipeline that best fits your needs. Each pipeline determines when and how streams are checked.
                      </Typography>
                      
                      <FormControl component="fieldset" fullWidth sx={{ mb: 3 }}>
                        <RadioGroup
                          value={streamCheckerConfig.pipeline_mode || 'pipeline_1_5'}
                          onChange={(e) => handleStreamCheckerConfigChange('pipeline_mode', e.target.value)}
                        >
                          <Card variant="outlined" sx={{ mb: 1, p: 1 }}>
                            <FormControlLabel 
                              value="pipeline_1" 
                              control={<Radio />} 
                              label={
                                <Box>
                                  <Typography variant="subtitle1" fontWeight="bold">Pipeline 1: Update → Match → Check (with 2hr immunity)</Typography>
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
                              sx={{ alignItems: 'flex-start', m: 0 }}
                            />
                          </Card>

                          <Card variant="outlined" sx={{ mb: 1, p: 1 }}>
                            <FormControlLabel 
                              value="pipeline_1_5" 
                              control={<Radio />} 
                              label={
                                <Box>
                                  <Typography variant="subtitle1" fontWeight="bold">Pipeline 1.5: Pipeline 1 + Scheduled Global Action</Typography>
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
                              sx={{ alignItems: 'flex-start', m: 0 }}
                            />
                          </Card>

                          <Card variant="outlined" sx={{ mb: 1, p: 1 }}>
                            <FormControlLabel 
                              value="pipeline_2" 
                              control={<Radio />} 
                              label={
                                <Box>
                                  <Typography variant="subtitle1" fontWeight="bold">Pipeline 2: Update → Match only (no automatic checking)</Typography>
                                  <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                                    Features:
                                  </Typography>
                                  <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                                    <li><Typography variant="body2" color="text.secondary">Automatic M3U updates</Typography></li>
                                    <li><Typography variant="body2" color="text.secondary">Stream matching</Typography></li>
                                  </Box>
                                </Box>
                              }
                              sx={{ alignItems: 'flex-start', m: 0 }}
                            />
                          </Card>

                          <Card variant="outlined" sx={{ mb: 1, p: 1 }}>
                            <FormControlLabel 
                              value="pipeline_2_5" 
                              control={<Radio />} 
                              label={
                                <Box>
                                  <Typography variant="subtitle1" fontWeight="bold">Pipeline 2.5: Pipeline 2 + Scheduled Global Action</Typography>
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
                              sx={{ alignItems: 'flex-start', m: 0 }}
                            />
                          </Card>

                          <Card variant="outlined" sx={{ mb: 1, p: 1 }}>
                            <FormControlLabel 
                              value="pipeline_3" 
                              control={<Radio />} 
                              label={
                                <Box>
                                  <Typography variant="subtitle1" fontWeight="bold">Pipeline 3: Only Scheduled Global Action</Typography>
                                  <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                                    Features:
                                  </Typography>
                                  <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 2 }}>
                                    <li><Typography variant="body2" color="text.secondary">Scheduled Global Action ONLY (daily/monthly)</Typography></li>
                                    <li><Typography variant="body2" color="text.secondary">NO automatic updates or checking</Typography></li>
                                  </Box>
                                </Box>
                              }
                              sx={{ alignItems: 'flex-start', m: 0 }}
                            />
                          </Card>
                        </RadioGroup>
                      </FormControl>
                      
                      {/* Show schedule settings only for pipelines with scheduled actions */}
                      {['pipeline_1_5', 'pipeline_2_5', 'pipeline_3'].includes(streamCheckerConfig.pipeline_mode) && (
                        <>
                          <Typography variant="h6" sx={{ mt: 3, mb: 2 }}>
                            Global Action Schedule
                          </Typography>
                          
                          <FormControlLabel
                            control={
                              <Switch
                                checked={streamCheckerConfig.global_check_schedule.enabled}
                                onChange={(e) => handleStreamCheckerConfigChange('global_check_schedule.enabled', e.target.checked)}
                              />
                            }
                            label="Enable Scheduled Global Action"
                            sx={{ mb: 2 }}
                          />
                          
                          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                            Configure when the global action runs (Update, Match, and Check all channels).
                          </Typography>
                          
                          <TextField
                            label="Cron Expression"
                            value={streamCheckerConfig.global_check_schedule.cron_expression || '0 3 * * *'}
                            onChange={(e) => handleStreamCheckerConfigChange('global_check_schedule.cron_expression', e.target.value)}
                            disabled={!streamCheckerConfig.global_check_schedule.enabled}
                            fullWidth
                            margin="normal"
                            helperText="Enter a cron expression (e.g., '0 3 * * *' for daily at 3:00 AM)"
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
                        </>
                      )}
                      

                      {m3uAccounts.length > 0 && (
                        <>
                          <Typography variant="h6" sx={{ mt: 3, mb: 2 }}>
                            M3U Playlists
                          </Typography>
                          
                          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                            Select which M3U accounts/playlists to include in the stream fetch pipeline.
                            {selectedM3uAccounts.length === 0 && ' (All accounts enabled when none selected)'}
                          </Typography>
                          
                          <FormGroup>
                            {m3uAccounts.map((account) => (
                              <FormControlLabel
                                key={account.id}
                                control={
                                  <Switch
                                    checked={selectedM3uAccounts.length === 0 || selectedM3uAccounts.includes(account.id)}
                                    onChange={(e) => {
                                      let newSelected;
                                      if (selectedM3uAccounts.length === 0) {
                                        // All accounts currently enabled (empty array)
                                        // User is unchecking one, so populate array with all OTHER account IDs
                                        newSelected = m3uAccounts
                                          .filter(acc => acc.id !== account.id)
                                          .map(acc => acc.id);
                                      } else {
                                        // Some accounts selected
                                        if (e.target.checked) {
                                          // Adding an account
                                          newSelected = [...selectedM3uAccounts, account.id];
                                          // If all accounts are now selected, reset to empty array (meaning "all")
                                          if (newSelected.length === m3uAccounts.length) {
                                            newSelected = [];
                                          }
                                        } else {
                                          // Removing an account
                                          newSelected = selectedM3uAccounts.filter(id => id !== account.id);
                                        }
                                      }
                                      setSelectedM3uAccounts(newSelected);
                                      handleConfigChange('enabled_m3u_accounts', newSelected);
                                    }}
                                  />
                                }
                                label={`${account.name || `Account ${account.id}`} - ${account.url || 'No URL'}`}
                              />
                            ))}
                          </FormGroup>
                        </>
                      )}
                      
                      <Box sx={{ mt: 3, display: 'flex', gap: 1 }}>
                        <Button onClick={handleBack}>
                          Back
                        </Button>
                        <Button
                          variant="contained"
                          onClick={handleConfigSave}
                          disabled={loading}
                        >
                          {loading ? <CircularProgress size={20} /> : 'Save & Continue'}
                        </Button>
                      </Box>
                    </Box>
                  )}

                  {index === 3 && (
                    <Box sx={{ mt: 2 }}>
                      <Alert severity="success" sx={{ mb: 2 }}>
                        Setup completed successfully! Your automated stream manager is ready to use.
                      </Alert>
                      
                      <Typography variant="body1" sx={{ mb: 2 }}>
                        You can now:
                      </Typography>
                      <ul>
                        <li>Configure regex patterns for automatic stream assignment</li>
                        <li>Start the automation system from the dashboard</li>
                        <li>Monitor stream quality and assignments</li>
                        <li>View changelog and activity logs</li>
                      </ul>
                      
                      <Box sx={{ mt: 3 }}>
                        <Button
                          variant="contained"
                          onClick={handleComplete}
                          size="large"
                        >
                          Go to Dashboard
                        </Button>
                      </Box>
                    </Box>
                  )}
                </StepContent>
              </Step>
            ))}
          </Stepper>
        </CardContent>
      </Card>

      {/* Pattern Configuration Dialog */}
      <Dialog open={openPatternDialog} onClose={handleClosePatternDialog} maxWidth="md" fullWidth>
        <DialogTitle>
          {editingChannel ? 'Edit Channel Pattern' : 'Add Channel Pattern'}
        </DialogTitle>
        <DialogContent>
          <TextField
            label="Channel ID"
            value={patternFormData.channel_id}
            onChange={(e) => setPatternFormData(prev => ({ ...prev, channel_id: e.target.value }))}
            fullWidth
            margin="normal"
            disabled={!!editingChannel}
          />
          <TextField
            label="Channel Name"
            value={patternFormData.name}
            onChange={(e) => setPatternFormData(prev => ({ ...prev, name: e.target.value }))}
            fullWidth
            margin="normal"
          />
          
          <Typography variant="h6" sx={{ mt: 2, mb: 1 }}>
            Regex Patterns
          </Typography>
          
          {patternFormData.regex.map((regex, index) => (
            <Box key={index} display="flex" alignItems="center" gap={1} mb={1}>
              <TextField
                label={`Pattern ${index + 1}`}
                value={regex}
                onChange={(e) => updateRegexField(index, e.target.value)}
                fullWidth
                placeholder="e.g., .*CNN.*|.*News.*"
              />
              {patternFormData.regex.length > 1 && (
                <IconButton
                  onClick={() => removeRegexField(index)}
                  color="error"
                  size="small"
                >
                  <DeleteIcon />
                </IconButton>
              )}
            </Box>
          ))}
          
          <Button
            startIcon={<AddIcon />}
            onClick={addRegexField}
            variant="outlined"
            size="small"
            sx={{ mt: 1 }}
          >
            Add Pattern
          </Button>
          
          <FormControlLabel
            control={
              <Switch
                checked={patternFormData.enabled}
                onChange={(e) => setPatternFormData(prev => ({ ...prev, enabled: e.target.checked }))}
              />
            }
            label="Enable Pattern"
            sx={{ mt: 2, display: 'block' }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClosePatternDialog}>Cancel</Button>
          <Button 
            onClick={handleSavePattern}
            variant="contained"
            disabled={loading || !patternFormData.channel_id || !patternFormData.name}
          >
            {loading ? <CircularProgress size={20} /> : 'Save Pattern'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export default SetupWizard;
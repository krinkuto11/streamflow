import React, { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Typography,
  Card,
  CardContent,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Paper,
  Chip,
  Alert,
  CircularProgress,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Avatar,
  List,
  ListItem,
  ListItemText,
  Divider,
  Stack,
  Pagination
} from '@mui/material';
import {
  ExpandMore as ExpandMoreIcon,
  Add as AddIcon,
  Remove as RemoveIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon
} from '@mui/icons-material';
import { changelogAPI, deadStreamsAPI } from '../services/api';
import axios from 'axios';

function Changelog() {
  const [changelog, setChangelog] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [days, setDays] = useState(7);
  const [channelLogos, setChannelLogos] = useState({});
  const [expandedEntries, setExpandedEntries] = useState({});
  const [page, setPage] = useState(1);
  const [deadStreamsStats, setDeadStreamsStats] = useState(null);
  const itemsPerPage = 10;

  const fetchChannelLogos = useCallback(async (channelIds, existingLogos = {}) => {
    const logos = { ...existingLogos };
    
    // Fetch channel data to get logo_id
    try {
      const response = await axios.get('/api/channels');
      const channels = response.data;
      
      // Create a map of channel IDs to logo IDs for quick lookup
      const channelLogoIds = {};
      for (const channelId of channelIds) {
        const channel = channels.find(ch => String(ch.id) === String(channelId));
        if (channel && channel.logo_id) {
          channelLogoIds[channelId] = channel.logo_id;
        }
      }
      
      // Fetch all logos in parallel (with batching to avoid overwhelming the server)
      const batchSize = 10;
      const channelIdArray = Object.keys(channelLogoIds);
      
      for (let i = 0; i < channelIdArray.length; i += batchSize) {
        const batch = channelIdArray.slice(i, i + batchSize);
        const logoPromises = batch.map(async (channelId) => {
          try {
            const logoResponse = await axios.get(`/api/channels/logos/${channelLogoIds[channelId]}`);
            return { channelId, url: logoResponse.data.url || logoResponse.data.cache_url };
          } catch (logoErr) {
            console.warn(`Failed to fetch logo for channel ${channelId}:`, logoErr);
            return { channelId, url: null };
          }
        });
        
        const batchResults = await Promise.all(logoPromises);
        batchResults.forEach(({ channelId, url }) => {
          if (url) {
            logos[channelId] = url;
          }
        });
        
        // Update state after each batch for progressive loading
        setChannelLogos({ ...logos });
      }
      
      // Final update and cache
      try {
        localStorage.setItem('channelLogosCache', JSON.stringify({
          logos,
          timestamp: Date.now()
        }));
      } catch (err) {
        console.warn('Failed to cache logos:', err);
      }
    } catch (err) {
      console.warn('Failed to fetch channel data:', err);
    }
  }, []);

  const loadChangelog = useCallback(async () => {
    try {
      setLoading(true);
      setPage(1); // Reset to first page when loading new data
      const response = await changelogAPI.getChangelog(days);
      setChangelog(response.data);
      
      // Load dead streams statistics
      try {
        const deadStreamsResponse = await deadStreamsAPI.getDeadStreams();
        setDeadStreamsStats(deadStreamsResponse.data);
      } catch (err) {
        console.warn('Failed to load dead streams stats:', err);
      }
      
      // Load cached logos first
      let cachedLogos = {};
      try {
        const cached = localStorage.getItem('channelLogosCache');
        if (cached) {
          const { logos, timestamp } = JSON.parse(cached);
          // Cache expires after 24 hours
          const cacheAge = Date.now() - timestamp;
          if (cacheAge < 24 * 60 * 60 * 1000) {
            setChannelLogos(logos);
            cachedLogos = logos;
          }
        }
      } catch (err) {
        console.warn('Failed to load cached logos:', err);
      }
      
      // Extract unique channel IDs and fetch their logos
      const channelIds = new Set();
      response.data.forEach(entry => {
        if (entry.action === 'streams_assigned' && entry.details?.assignments) {
          entry.details.assignments.forEach(assignment => {
            if (assignment.channel_id) {
              channelIds.add(assignment.channel_id);
            }
          });
        }
      });
      
      // Only fetch logos that are not already cached
      const uncachedIds = [...channelIds].filter(id => !cachedLogos[id]);
      if (uncachedIds.length > 0) {
        await fetchChannelLogos(uncachedIds, cachedLogos);
      }
    } catch (err) {
      console.error('Failed to load changelog:', err);
      setError('Failed to load changelog');
    } finally {
      setLoading(false);
    }
  }, [days, fetchChannelLogos]);



  useEffect(() => {
    loadChangelog();
  }, [loadChangelog]);

  const formatDateTime = (dateString) => {
    return new Date(dateString).toLocaleString();
  };

  const getActionColor = (action) => {
    switch (action) {
      case 'playlist_refresh':
        return 'primary';
      case 'streams_assigned':
        return 'success';
      case 'stream_discovery':
        return 'info';
      case 'quality_check':
      case 'new_streams_quality_check':
      case 'global_quality_check':
      case 'stream_check':
        return 'warning';
      default:
        return 'default';
    }
  };

  const formatActionName = (action) => {
    return action.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" height="400px">
        <CircularProgress />
      </Box>
    );
  }

  const renderStreamChanges = (streams, type) => {
    if (!streams || streams.length === 0) return null;
    
    return (
      <Box sx={{ mt: 1 }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
          {type === 'added' ? (
            <AddIcon fontSize="small" color="success" />
          ) : (
            <RemoveIcon fontSize="small" color="error" />
          )}
          <Typography variant="body2" fontWeight="bold" color={type === 'added' ? 'success.main' : 'error.main'}>
            {type === 'added' ? 'Added' : 'Removed'} ({streams.length})
          </Typography>
        </Stack>
        <List dense sx={{ pl: 2 }}>
          {streams.slice(0, 10).map((stream, idx) => {
            // Handle both {stream_name, stream_id} and {name, id} formats
            const streamName = typeof stream === 'string' 
              ? stream 
              : (stream.stream_name || stream.name || `Stream ${stream.stream_id || stream.id || 'unknown'}`);
            
            return (
              <ListItem key={idx} sx={{ py: 0.25 }}>
                <ListItemText 
                  primary={streamName}
                  primaryTypographyProps={{ variant: 'body2', fontSize: '0.875rem' }}
                />
              </ListItem>
            );
          })}
          {streams.length > 10 && (
            <ListItem sx={{ py: 0.25 }}>
              <ListItemText 
                primary={`... and ${streams.length - 10} more`}
                primaryTypographyProps={{ variant: 'body2', fontSize: '0.875rem', fontStyle: 'italic', color: 'text.secondary' }}
              />
            </ListItem>
          )}
        </List>
      </Box>
    );
  };

  const renderChannelBlock = (assignment) => {
    const logoUrl = channelLogos[assignment.channel_id];
    
    return (
      <Card key={assignment.channel_id} sx={{ mb: 2, bgcolor: 'background.default' }}>
        <CardContent>
          <Box display="flex" alignItems="center" gap={2} mb={2}>
            {logoUrl ? (
              <Avatar 
                src={logoUrl} 
                alt={assignment.channel_name}
                variant="rounded"
                sx={{ width: 48, height: 48 }}
              />
            ) : (
              <Avatar variant="rounded" sx={{ width: 48, height: 48, bgcolor: 'primary.main' }}>
                {assignment.channel_name?.charAt(0) || 'C'}
              </Avatar>
            )}
            <Box flex={1}>
              <Typography variant="subtitle1" fontWeight="bold">
                {assignment.channel_name}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Channel ID: {assignment.channel_id}
              </Typography>
            </Box>
            <Chip 
              label={`${assignment.stream_count} streams`} 
              color="primary" 
              size="small"
            />
          </Box>
          
          {assignment.streams && assignment.streams.length > 0 && renderStreamChanges(assignment.streams, 'added')}
        </CardContent>
      </Card>
    );
  };

  const toggleExpandEntry = (entryIndex) => {
    setExpandedEntries(prev => ({
      ...prev,
      [entryIndex]: !prev[entryIndex]
    }));
  };

  const renderChangelogEntry = (entry, index) => {
    const { action, details, timestamp } = entry;
    
    // Render based on action type
    if (action === 'streams_assigned' && details?.assignments) {
      const isExpanded = expandedEntries[index] !== false; // Default to expanded
      const maxChannelsToShow = 10; // Show first 10 channels by default
      const hasMoreChannels = details.assignments.length > maxChannelsToShow || details.has_more_channels;
      const channelsToDisplay = isExpanded ? details.assignments : details.assignments.slice(0, maxChannelsToShow);
      
      return (
        <Card key={index} sx={{ mb: 3 }}>
          <CardContent>
            <Box display="flex" alignItems="center" gap={2} mb={2}>
              <CheckCircleIcon color="success" />
              <Box flex={1}>
                <Typography variant="h6">
                  {formatActionName(action)}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {formatDateTime(timestamp)}
                </Typography>
              </Box>
              <Chip 
                label={`${details.total_assigned} total streams`} 
                color="success"
              />
            </Box>
            
            <Divider sx={{ my: 2 }} />
            
            <Typography variant="subtitle2" color="text.secondary" gutterBottom>
              {details.channel_count} channel{details.channel_count !== 1 ? 's' : ''} updated
              {details.has_more_channels && ' (showing top channels by stream count)'}:
            </Typography>
            
            <Box sx={{ mt: 2 }}>
              {channelsToDisplay.map(assignment => renderChannelBlock(assignment))}
            </Box>
            
            {hasMoreChannels && (
              <Box sx={{ mt: 2, textAlign: 'center' }}>
                <Chip
                  label={isExpanded ? 'Show Less' : `Show ${details.assignments.length - maxChannelsToShow} More Channel${details.assignments.length - maxChannelsToShow !== 1 ? 's' : ''}`}
                  onClick={() => toggleExpandEntry(index)}
                  color="primary"
                  variant="outlined"
                  sx={{ cursor: 'pointer' }}
                />
                {details.has_more_channels && (
                  <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
                    Note: Only top channels shown for performance
                  </Typography>
                )}
              </Box>
            )}
          </CardContent>
        </Card>
      );
    }
    
    if (action === 'playlist_refresh' && details) {
      const addedStreams = details.added_streams || [];
      const removedStreams = details.removed_streams || [];
      const hasChanges = addedStreams.length > 0 || removedStreams.length > 0;
      
      return (
        <Card key={index} sx={{ mb: 3 }}>
          <CardContent>
            <Box display="flex" alignItems="center" gap={2} mb={2}>
              {details.success ? (
                <CheckCircleIcon color="success" />
              ) : (
                <ErrorIcon color="error" />
              )}
              <Box flex={1}>
                <Typography variant="h6">
                  {formatActionName(action)}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {formatDateTime(timestamp)}
                </Typography>
              </Box>
              <Chip 
                label={details.success ? 'Success' : 'Failed'}
                color={details.success ? 'success' : 'error'}
              />
            </Box>
            
            {hasChanges && (
              <>
                <Divider sx={{ my: 2 }} />
                
                <Box>
                  <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                    Playlist Changes:
                  </Typography>
                  
                  {addedStreams.length > 0 && (
                    <Card sx={{ mb: 2, bgcolor: 'background.default' }}>
                      <CardContent>
                        {renderStreamChanges(addedStreams, 'added')}
                      </CardContent>
                    </Card>
                  )}
                  
                  {removedStreams.length > 0 && (
                    <Card sx={{ mb: 2, bgcolor: 'background.default' }}>
                      <CardContent>
                        {renderStreamChanges(removedStreams, 'removed')}
                      </CardContent>
                    </Card>
                  )}
                </Box>
              </>
            )}
            
            {details.total_streams !== undefined && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                Total streams after update: {details.total_streams}
              </Typography>
            )}
          </CardContent>
        </Card>
      );
    }
    
    // Render stream check entries
    if (action === 'stream_check' && details) {
      const streamStats = details.stream_stats || [];
      
      return (
        <Card key={index} sx={{ mb: 3 }}>
          <CardContent>
            <Box display="flex" alignItems="center" gap={2} mb={2}>
              {details.success ? (
                <CheckCircleIcon color="success" />
              ) : (
                <ErrorIcon color="error" />
              )}
              <Box flex={1}>
                <Typography variant="h6">
                  Stream Quality Check
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {formatDateTime(timestamp)}
                </Typography>
              </Box>
              <Chip 
                label={details.success ? 'Completed' : 'Failed'}
                color={details.success ? 'success' : 'error'}
              />
            </Box>
            
            <Divider sx={{ my: 2 }} />
            
            <Box>
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom>
                {details.channel_name}
              </Typography>
              <Typography variant="body2" color="text.secondary" gutterBottom>
                Channel ID: {details.channel_id}
              </Typography>
              
              {details.total_streams && (
                <Box sx={{ mt: 1 }}>
                  <Typography variant="body2" color="text.secondary">
                    Total Streams: {details.total_streams} | Analyzed: {details.streams_analyzed || details.total_streams}
                  </Typography>
                  {details.dead_streams_detected > 0 && (
                    <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
                      <Chip 
                        label={`${details.dead_streams_detected} Dead Stream${details.dead_streams_detected !== 1 ? 's' : ''} Detected`}
                        color="error"
                        size="small"
                        icon={<ErrorIcon />}
                      />
                    </Stack>
                  )}
                  {details.streams_revived > 0 && (
                    <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
                      <Chip 
                        label={`${details.streams_revived} Stream${details.streams_revived !== 1 ? 's' : ''} Revived`}
                        color="success"
                        size="small"
                        icon={<CheckCircleIcon />}
                      />
                    </Stack>
                  )}
                </Box>
              )}
              
              {details.error && (
                <Alert severity="error" sx={{ mt: 2 }}>
                  {details.error}
                </Alert>
              )}
              
              {streamStats.length > 0 && (
                <>
                  <Typography variant="subtitle2" color="text.secondary" sx={{ mt: 2, mb: 1 }}>
                    Top Streams (by quality score):
                  </Typography>
                  <Card sx={{ bgcolor: 'background.default' }}>
                    <CardContent>
                      <List dense>
                        {streamStats.map((stream, idx) => {
                          const isDead = stream.stream_name && stream.stream_name.startsWith('[DEAD]');
                          const displayName = isDead ? stream.stream_name.replace('[DEAD]', '').trim() : stream.stream_name;
                          
                          return (
                          <ListItem key={idx} sx={{ py: 1, flexDirection: 'column', alignItems: 'flex-start' }}>
                            <Box display="flex" alignItems="center" gap={1} width="100%">
                              <Chip 
                                label={`#${idx + 1}`} 
                                size="small" 
                                color="primary"
                                sx={{ minWidth: 40 }}
                              />
                              {isDead && (
                                <Chip 
                                  label="DEAD"
                                  size="small"
                                  color="error"
                                  sx={{ fontWeight: 'bold' }}
                                />
                              )}
                              <Typography 
                                variant="body2" 
                                fontWeight="bold" 
                                flex={1}
                                color={isDead ? 'error.main' : 'text.primary'}
                              >
                                {displayName}
                              </Typography>
                              {stream.score !== undefined && (
                                <Chip 
                                  label={`Score: ${stream.score}`}
                                  size="small"
                                  color={
                                    stream.score === 0 ? 'error' : 
                                    stream.score >= 0.7 ? 'success' : 
                                    stream.score >= 0.5 ? 'warning' : 
                                    'default'
                                  }
                                />
                              )}
                            </Box>
                            <Box sx={{ pl: 6, pt: 0.5 }}>
                              <Typography variant="caption" color={isDead ? 'error' : 'text.secondary'}>
                                {stream.resolution && `${stream.resolution}`}
                                {stream.fps && ` @ ${stream.fps} fps`}
                                {stream.video_codec && ` | ${stream.video_codec}`}
                                {stream.bitrate_kbps && ` | ${stream.bitrate_kbps} kbps`}
                                {stream.status && ` | ${stream.status}`}
                              </Typography>
                            </Box>
                          </ListItem>
                          );
                        })}
                      </List>
                      {details.total_streams > streamStats.length && (
                        <Typography variant="caption" color="text.secondary" sx={{ pl: 2, pt: 1, display: 'block' }}>
                          ... and {details.total_streams - streamStats.length} more streams
                        </Typography>
                      )}
                    </CardContent>
                  </Card>
                </>
              )}
            </Box>
          </CardContent>
        </Card>
      );
    }
    
    // Fallback for other action types
    return (
      <Card key={index} sx={{ mb: 3 }}>
        <Accordion>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Box display="flex" alignItems="center" gap={2} width="100%">
              <Chip
                label={formatActionName(action)}
                color={getActionColor(action)}
                size="small"
              />
              <Typography variant="body2">
                {formatDateTime(timestamp)}
              </Typography>
              {details?.success !== undefined && (
                <Chip
                  label={details.success ? 'Success' : 'Failed'}
                  color={details.success ? 'success' : 'error'}
                  size="small"
                />
              )}
            </Box>
          </AccordionSummary>
          <AccordionDetails>
            <Box>
              <Typography variant="body2" gutterBottom>
                <strong>Action:</strong> {formatActionName(action)}
              </Typography>
              <Typography variant="body2" gutterBottom>
                <strong>Timestamp:</strong> {formatDateTime(timestamp)}
              </Typography>
              
              {details && (
                <Box sx={{ mt: 2 }}>
                  <Typography variant="body2" gutterBottom>
                    <strong>Details:</strong>
                  </Typography>
                  <Paper sx={{ p: 2, bgcolor: 'grey.900' }}>
                    <pre style={{ margin: 0, fontSize: '0.875rem' }}>
                      {JSON.stringify(details, null, 2)}
                    </pre>
                  </Paper>
                </Box>
              )}
            </Box>
          </AccordionDetails>
        </Accordion>
      </Card>
    );
  };

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Changelog
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError('')}>
          {error}
        </Alert>
      )}

      <Card sx={{ mb: 3 }}>
        <CardContent>
          <Box display="flex" alignItems="center" gap={2}>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Time Period</InputLabel>
              <Select
                value={days}
                label="Time Period"
                onChange={(e) => setDays(e.target.value)}
              >
                <MenuItem value={1}>Last 24 hours</MenuItem>
                <MenuItem value={3}>Last 3 days</MenuItem>
                <MenuItem value={7}>Last 7 days</MenuItem>
                <MenuItem value={14}>Last 14 days</MenuItem>
                <MenuItem value={30}>Last 30 days</MenuItem>
              </Select>
            </FormControl>
            <Typography variant="body2" color="text.secondary">
              Showing {changelog.length} update{changelog.length !== 1 ? 's' : ''}
            </Typography>
          </Box>
        </CardContent>
      </Card>

      {deadStreamsStats && deadStreamsStats.total_dead_streams > 0 && (
        <Card sx={{ mb: 3, bgcolor: 'error.dark', borderLeft: '4px solid', borderColor: 'error.main' }}>
          <CardContent>
            <Box display="flex" alignItems="center" gap={2}>
              <ErrorIcon sx={{ fontSize: 40, color: 'white' }} />
              <Box flex={1}>
                <Typography variant="h6" color="white" fontWeight="bold">
                  Dead Streams Alert
                </Typography>
                <Typography variant="body2" color="white">
                  {deadStreamsStats.total_dead_streams} stream{deadStreamsStats.total_dead_streams !== 1 ? 's are' : ' is'} currently marked as dead
                </Typography>
              </Box>
              <Chip 
                label={`${deadStreamsStats.total_dead_streams} Dead`}
                color="error"
                size="large"
                sx={{ fontWeight: 'bold', fontSize: '1rem', px: 2, color: 'white', bgcolor: 'error.main' }}
              />
            </Box>
          </CardContent>
        </Card>
      )}

      {changelog.length === 0 ? (
        <Alert severity="info">
          No changelog entries found for the selected time period.
        </Alert>
      ) : (
        <>
          <Box>
            {changelog
              .slice((page - 1) * itemsPerPage, page * itemsPerPage)
              .map((entry, index) => renderChangelogEntry(entry, (page - 1) * itemsPerPage + index))}
          </Box>
          
          {changelog.length > itemsPerPage && (
            <Box display="flex" justifyContent="center" sx={{ mt: 3, mb: 2 }}>
              <Pagination
                count={Math.ceil(changelog.length / itemsPerPage)}
                page={page}
                onChange={(event, value) => {
                  setPage(value);
                  window.scrollTo({ top: 0, behavior: 'smooth' });
                }}
                color="primary"
                size="large"
                showFirstButton
                showLastButton
              />
            </Box>
          )}
        </>
      )}
    </Box>
  );
}

export default Changelog;
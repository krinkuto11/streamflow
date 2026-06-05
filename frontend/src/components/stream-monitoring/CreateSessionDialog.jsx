import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { AlertCircle, Settings2, ShieldCheck, Play, MonitorPlay } from 'lucide-react';
import { channelsAPI } from '@/services/api';
import { useToast } from '@/hooks/use-toast';

function CreateSessionDialog({ open, onOpenChange, onCreateSession }) {
  const [mode, setMode] = useState('channel');
  const [channels, setChannels] = useState([]);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(false);
  const [formData, setFormData] = useState({
    channel_id: '',
    group_id: '',
    stagger_ms: 1000,
    evaluation_interval_ms: 1000,
    enforce_sync_interval_ms: 1000,
    timeout_ms: 30000,
    autoStart: true,
    enable_looping_detection: true,
    enable_logo_detection: true
  });
  const { toast } = useToast();

  useEffect(() => {
    if (open) {
      loadData();
    }
  }, [open]);

  const loadData = async () => {
    try {
      setLoading(true);
      const [channelsRes, groupsRes] = await Promise.all([
        channelsAPI.getChannels(),
        channelsAPI.getGroups()
      ]);
      setChannels(channelsRes.data || []);
      setGroups(groupsRes.data || []);
    } catch (err) {
      console.error('Failed to load data:', err);
      toast({
        title: 'Error',
        description: 'Failed to load channels or groups',
        variant: 'destructive'
      });
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (mode === 'channel' && !formData.channel_id) {
      toast({
        title: 'Validation Error',
        description: 'Please select a channel',
        variant: 'destructive'
      });
      return;
    }

    if (mode === 'group' && !formData.group_id) {
      toast({
        title: 'Validation Error',
        description: 'Please select a group',
        variant: 'destructive'
      });
      return;
    }

    const payload = {
      ...formData,
      channel_id: mode === 'channel' ? formData.channel_id : undefined,
      group_id: mode === 'group' ? formData.group_id : undefined
    };

    onCreateSession(payload);
  };

  const handleChange = (field, value) => {
    setFormData(prev => ({
      ...prev,
      [field]: value
    }));
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[850px]">
        <DialogHeader>
          <DialogTitle>Create Monitoring Session</DialogTitle>
          <DialogDescription>
            Create monitoring sessions for single channels or entire groups
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 py-4">
            {/* Left Column: Target Selection */}
            <div className="space-y-4">
              <Tabs value={mode} onValueChange={setMode} className="w-full">
                <TabsList className="grid w-full grid-cols-2 mb-4">
                  <TabsTrigger value="channel">Single Channel</TabsTrigger>
                  <TabsTrigger value="group">Channel Group</TabsTrigger>
                </TabsList>

                <TabsContent value="channel" className="space-y-4 mt-0">
                  <div className="space-y-2">
                    <Label htmlFor="channel">Channel *</Label>
                    <Select
                      value={formData.channel_id ? formData.channel_id.toString() : ''}
                      onValueChange={(value) => handleChange('channel_id', parseInt(value))}
                    >
                      <SelectTrigger id="channel">
                        <SelectValue placeholder="Select a channel" />
                      </SelectTrigger>
                      <SelectContent>
                        {channels.map((channel) => (
                          <SelectItem key={channel.id} value={channel.id.toString()}>
                            {channel.name} (#{channel.channel_number})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-muted-foreground">
                      Monitor a specific channel using its configured rules.
                    </p>
                  </div>
                </TabsContent>

                <TabsContent value="group" className="space-y-4 mt-0">
                  <div className="space-y-2">
                    <Label htmlFor="group">Channel Group *</Label>
                    <Select
                      value={formData.group_id ? formData.group_id.toString() : ''}
                      onValueChange={(value) => handleChange('group_id', parseInt(value))}
                    >
                      <SelectTrigger id="group">
                        <SelectValue placeholder="Select a group" />
                      </SelectTrigger>
                      <SelectContent>
                        {groups.map((group) => (
                          <SelectItem key={group.id} value={group.id.toString()}>
                            {group.name} ({group.channel_count} channels)
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-muted-foreground">
                      Create and auto-start sessions for all channels in this group.
                    </p>
                  </div>
                </TabsContent>
              </Tabs>

              {/* Info Alert moved to bottom of left column */}
              <Alert className="mt-6">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription className="text-xs leading-relaxed">
                  {mode === 'group'
                    ? "Group monitoring will create and immediately start sessions for all channels in the selected group."
                    : "Sessions continuously monitor stream quality, capture screenshots, and calculate reliability scores using Capped Sliding Window algorithm."
                  }
                </AlertDescription>
              </Alert>
            </div>

            {/* Right Column: Settings */}
            <div className="space-y-5">
              <>

              {/* Advanced Settings */}
              <div className="border rounded-lg p-4 space-y-4 bg-muted/20">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Settings2 className="h-4 w-4 text-primary" />
                  <h4>Advanced Settings</h4>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="stagger" className="text-xs">Stagger (ms)</Label>
                    <Input
                      id="stagger"
                      type="number"
                      min="0"
                      max="5000"
                      value={formData.stagger_ms}
                      onChange={(e) => handleChange('stagger_ms', parseInt(e.target.value))}
                      className="h-8 text-sm"
                    />
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="sync_interval" className="text-xs">Sync Interval (ms)</Label>
                    <Input
                      id="sync_interval"
                      type="number"
                      min="500"
                      max="10000"
                      value={formData.enforce_sync_interval_ms}
                      onChange={(e) => handleChange('enforce_sync_interval_ms', parseInt(e.target.value))}
                      className="h-8 text-sm"
                    />
                  </div>

                  <div className="space-y-2 col-span-2">
                    <Label htmlFor="timeout" className="text-xs">Stream Timeout (ms)</Label>
                    <Input
                      id="timeout"
                      type="number"
                      min="5000"
                      max="120000"
                      value={formData.timeout_ms}
                      onChange={(e) => handleChange('timeout_ms', parseInt(e.target.value))}
                      className="h-8 text-sm"
                    />
                  </div>
                </div>
              </div>

              {/* Detection Toggles */}
              <div className="border rounded-lg p-4 space-y-4 bg-muted/20">
                <div className="flex items-center gap-2 text-sm font-medium mb-2">
                  <ShieldCheck className="h-4 w-4 text-primary" />
                  <h4>Detection Features</h4>
                </div>

                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="looping-detection" className="text-sm">
                        Looping Detection
                      </Label>
                      <p className="text-[11px] text-muted-foreground mr-4">
                        Identify and penalize looping streams
                      </p>
                    </div>
                    <Switch
                      id="looping-detection"
                      checked={formData.enable_looping_detection}
                      onCheckedChange={(checked) => handleChange('enable_looping_detection', checked)}
                    />
                  </div>

                  <div className="flex items-center justify-between border-t pt-4">
                    <div className="space-y-0.5">
                      <Label htmlFor="logo-detection" className="text-sm">
                        Logo Verification
                      </Label>
                      <p className="text-[11px] text-muted-foreground mr-4">
                        Verify stream logo against reference
                      </p>
                    </div>
                    <Switch
                      id="logo-detection"
                      checked={formData.enable_logo_detection}
                      onCheckedChange={(checked) => handleChange('enable_logo_detection', checked)}
                    />
                  </div>
                </div>
              </div>

              {/* Auto-start Toggle */}
              {mode === 'channel' && (
                <div className="flex items-center justify-between rounded-lg border p-4 bg-muted/20">
                  <div className="space-y-0.5">
                    <div className="flex items-center gap-2">
                      <Play className="h-4 w-4 text-primary" />
                      <Label htmlFor="autostart" className="text-sm font-medium">
                        Auto-start Monitoring
                      </Label>
                    </div>
                    <p className="text-[11px] text-muted-foreground">
                      Begin monitoring immediately after creation
                    </p>
                  </div>
                  <Switch
                    id="autostart"
                    checked={formData.autoStart}
                    onCheckedChange={(checked) => handleChange('autoStart', checked)}
                  />
                </div>
              )}
              </>
            </div>
          </div>

          <DialogFooter className="pt-4 border-t">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={loading || (mode === 'channel' ? !formData.channel_id : !formData.group_id)}
              className="gap-2"
            >
              <MonitorPlay className="h-4 w-4" />
              {mode === 'group' ? 'Start Group Monitoring' : 'Create Session'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default CreateSessionDialog;

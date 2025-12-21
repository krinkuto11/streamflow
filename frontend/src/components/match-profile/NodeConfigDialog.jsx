import { useState, useEffect } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ScrollArea } from '@/components/ui/scroll-area';
import { X, Plus, Database, Filter, Wand2, Link2, Target } from 'lucide-react';

const NodeIcons = {
  source: Database,
  filter: Filter,
  transform: Wand2,
  match: Link2,
  action: Target,
};

const NodeConfigDialog = ({ open, onOpenChange, node, onSave, m3uAccounts = [], channels = [] }) => {
  const [config, setConfig] = useState({});
  const [tempInput, setTempInput] = useState('');

  useEffect(() => {
    if (node) {
      setConfig(node.data?.config || {});
    }
  }, [node]);

  if (!node) return null;

  const Icon = NodeIcons[node.type] || Database;

  const handleSave = () => {
    // For match nodes, ensure channels array is synced with patterns
    let finalConfig = { ...config };
    if (node.type === 'match' && config.patterns) {
      // Extract channel IDs that have patterns configured
      finalConfig.channels = Object.keys(config.patterns)
        .filter(channelId => config.patterns[channelId] && config.patterns[channelId].length > 0)
        .map(id => parseInt(id));
    }
    
    onSave({
      ...node,
      data: {
        ...node.data,
        config: finalConfig,
      },
    });
    onOpenChange(false);
  };

  const addToArray = (field, value) => {
    if (!value.trim()) return;
    const current = config[field] || [];
    setConfig({ ...config, [field]: [...current, value.trim()] });
    setTempInput('');
  };

  const removeFromArray = (field, index) => {
    const current = config[field] || [];
    setConfig({ ...config, [field]: current.filter((_, i) => i !== index) });
  };

  const updateConfig = (field, value) => {
    setConfig({ ...config, [field]: value });
  };

  const renderSourceConfig = () => (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label>M3U Accounts</Label>
        <div className="space-y-2">
          <Select
            onValueChange={(value) => {
              const accountId = parseInt(value);
              const current = config.m3u_accounts || [];
              if (!current.includes(accountId)) {
                updateConfig('m3u_accounts', [...current, accountId]);
              }
            }}
          >
            <SelectTrigger>
              <SelectValue placeholder="Select M3U accounts..." />
            </SelectTrigger>
            <SelectContent>
              {m3uAccounts.map((account) => (
                <SelectItem key={account.id} value={account.id.toString()}>
                  {account.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="flex flex-wrap gap-2">
            {(config.m3u_accounts || []).map((accountId, index) => {
              const account = m3uAccounts.find((a) => a.id === accountId);
              return (
                <Badge key={index} variant="secondary" className="flex items-center gap-1">
                  {account?.name || `Account ${accountId}`}
                  <X
                    className="h-3 w-3 cursor-pointer"
                    onClick={() => removeFromArray('m3u_accounts', index)}
                  />
                </Badge>
              );
            })}
          </div>
        </div>
      </div>

      <Separator />

      <div className="space-y-2">
        <Label>Stream Groups</Label>
        <div className="space-y-2">
          <div className="flex gap-2">
            <Input
              placeholder="Enter group name..."
              value={tempInput}
              onChange={(e) => setTempInput(e.target.value)}
              onKeyPress={(e) => {
                if (e.key === 'Enter') {
                  addToArray('stream_groups', tempInput);
                }
              }}
            />
            <Button
              type="button"
              size="sm"
              onClick={() => addToArray('stream_groups', tempInput)}
            >
              <Plus className="h-4 w-4" />
            </Button>
          </div>
          <div className="flex flex-wrap gap-2">
            {(config.stream_groups || []).map((group, index) => (
              <Badge key={index} variant="secondary" className="flex items-center gap-1">
                {group}
                <X
                  className="h-3 w-3 cursor-pointer"
                  onClick={() => removeFromArray('stream_groups', index)}
                />
              </Badge>
            ))}
          </div>
        </div>
      </div>
    </div>
  );

  const renderFilterConfig = () => (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label>Filter Patterns (Regex)</Label>
        <Textarea
          placeholder="Enter regex patterns (one per line)..."
          value={(config.patterns || []).join('\n')}
          onChange={(e) =>
            updateConfig(
              'patterns',
              e.target.value.split('\n').filter((p) => p.trim())
            )
          }
          rows={5}
        />
        <p className="text-xs text-muted-foreground">
          Each line is a regex pattern. Streams matching ANY pattern will be included.
        </p>
      </div>

      <Separator />

      <div className="flex items-center justify-between">
        <Label htmlFor="exclude-dead">Exclude Dead Streams</Label>
        <Switch
          id="exclude-dead"
          checked={config.exclude_dead !== false}
          onCheckedChange={(checked) => updateConfig('exclude_dead', checked)}
        />
      </div>

      <div className="flex items-center justify-between">
        <Label htmlFor="case-sensitive">Case Sensitive</Label>
        <Switch
          id="case-sensitive"
          checked={config.case_sensitive === true}
          onCheckedChange={(checked) => updateConfig('case_sensitive', checked)}
        />
      </div>
    </div>
  );

  const renderTransformConfig = () => (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label>Remove Prefixes</Label>
        <Textarea
          placeholder="Enter prefixes to remove (one per line)..."
          value={(config.remove_prefixes || []).join('\n')}
          onChange={(e) =>
            updateConfig(
              'remove_prefixes',
              e.target.value.split('\n').filter((p) => p.trim())
            )
          }
          rows={3}
        />
      </div>

      <div className="space-y-2">
        <Label>Remove Suffixes</Label>
        <Textarea
          placeholder="Enter suffixes to remove (one per line)..."
          value={(config.remove_suffixes || []).join('\n')}
          onChange={(e) =>
            updateConfig(
              'remove_suffixes',
              e.target.value.split('\n').filter((p) => p.trim())
            )
          }
          rows={3}
        />
      </div>

      <Separator />

      <div className="flex items-center justify-between">
        <Label htmlFor="normalize-whitespace">Normalize Whitespace</Label>
        <Switch
          id="normalize-whitespace"
          checked={config.normalize_whitespace === true}
          onCheckedChange={(checked) => updateConfig('normalize_whitespace', checked)}
        />
      </div>
    </div>
  );

  const renderMatchConfig = () => (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label>Match Mode</Label>
        <Select
          value={config.match_mode || 'regex'}
          onValueChange={(value) => updateConfig('match_mode', value)}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="regex">Regex</SelectItem>
            <SelectItem value="exact">Exact Match</SelectItem>
            <SelectItem value="contains">Contains</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <Separator />

      <div className="space-y-2">
        <Label>Channel Patterns</Label>
        <p className="text-xs text-muted-foreground mb-2">
          Configure patterns for each channel
        </p>
        <ScrollArea className="h-[200px] border rounded-md p-3">
          {channels.length === 0 ? (
            <p className="text-sm text-muted-foreground">No channels available</p>
          ) : (
            channels.map((channel) => (
              <div key={channel.id} className="mb-3 last:mb-0">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-medium">{channel.name}</span>
                  <Badge variant="outline" className="text-xs">
                    {channel.channel_number || 'No #'}
                  </Badge>
                </div>
                <Textarea
                  placeholder="Enter patterns (one per line)..."
                  value={((config.patterns || {})[channel.id] || []).join('\n')}
                  onChange={(e) => {
                    const patterns = e.target.value.split('\n').filter((p) => p.trim());
                    updateConfig('patterns', {
                      ...(config.patterns || {}),
                      [channel.id]: patterns,
                    });
                  }}
                  rows={2}
                  className="text-sm"
                />
              </div>
            ))
          )}
        </ScrollArea>
      </div>

      <Separator />

      <div className="flex items-center justify-between">
        <Label htmlFor="match-case-sensitive">Case Sensitive</Label>
        <Switch
          id="match-case-sensitive"
          checked={config.case_sensitive === true}
          onCheckedChange={(checked) => updateConfig('case_sensitive', checked)}
        />
      </div>
    </div>
  );

  const renderActionConfig = () => (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label>Action</Label>
        <Select
          value={config.action || 'add_to_channel'}
          onValueChange={(value) => updateConfig('action', value)}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="add_to_channel">Add to Channel</SelectItem>
            <SelectItem value="tag">Tag Only</SelectItem>
            <SelectItem value="skip">Skip (No Action)</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <Separator />

      <div className="flex items-center justify-between">
        <Label htmlFor="deduplicate">Deduplicate Streams</Label>
        <Switch
          id="deduplicate"
          checked={config.deduplicate !== false}
          onCheckedChange={(checked) => updateConfig('deduplicate', checked)}
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="max-streams">Max Streams per Channel</Label>
        <Input
          id="max-streams"
          type="number"
          min="0"
          value={config.max_streams_per_channel || 0}
          onChange={(e) =>
            updateConfig('max_streams_per_channel', parseInt(e.target.value) || 0)
          }
        />
        <p className="text-xs text-muted-foreground">
          0 means unlimited streams per channel
        </p>
      </div>
    </div>
  );

  const renderConfigContent = () => {
    switch (node.type) {
      case 'source':
        return renderSourceConfig();
      case 'filter':
        return renderFilterConfig();
      case 'transform':
        return renderTransformConfig();
      case 'match':
        return renderMatchConfig();
      case 'action':
        return renderActionConfig();
      default:
        return <p className="text-muted-foreground">Unknown node type</p>;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Icon className="h-5 w-5" />
            Configure {node.type.charAt(0).toUpperCase() + node.type.slice(1)} Node
          </DialogTitle>
          <DialogDescription>
            Configure the settings for this pipeline node
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className="flex-1 pr-4">
          <div className="py-4">{renderConfigContent()}</div>
        </ScrollArea>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave}>Save Configuration</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default NodeConfigDialog;

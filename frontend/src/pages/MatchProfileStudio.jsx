import { useState, useEffect, useCallback } from 'react';
import ReactFlow, {
  MiniMap,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  addEdge,
  MarkerType,
} from 'reactflow';
import 'reactflow/dist/style.css';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import { useToast } from '@/hooks/use-toast';
import { 
  Play, 
  Save, 
  Trash2, 
  Plus, 
  Settings as SettingsIcon,
  TestTube,
  Database,
  Filter,
  Wand2,
  Link2,
  Target
} from 'lucide-react';

import matchProfileService from '@/services/matchProfileService';

// Custom node components
const SourceNodeComponent = ({ data }) => (
  <div className="px-4 py-2 shadow-md rounded-md bg-blue-50 border-2 border-blue-500 dark:bg-blue-900/30">
    <div className="flex items-center gap-2">
      <Database className="h-4 w-4" />
      <div className="font-bold text-sm">Source</div>
    </div>
    <div className="text-xs text-muted-foreground mt-1">
      {data.config?.m3u_accounts?.length || 0} accounts
    </div>
  </div>
);

const FilterNodeComponent = ({ data }) => (
  <div className="px-4 py-2 shadow-md rounded-md bg-yellow-50 border-2 border-yellow-500 dark:bg-yellow-900/30">
    <div className="flex items-center gap-2">
      <Filter className="h-4 w-4" />
      <div className="font-bold text-sm">Filter</div>
    </div>
    <div className="text-xs text-muted-foreground mt-1">
      {data.config?.patterns?.length || 0} patterns
    </div>
  </div>
);

const TransformNodeComponent = ({ data }) => (
  <div className="px-4 py-2 shadow-md rounded-md bg-purple-50 border-2 border-purple-500 dark:bg-purple-900/30">
    <div className="flex items-center gap-2">
      <Wand2 className="h-4 w-4" />
      <div className="font-bold text-sm">Transform</div>
    </div>
    <div className="text-xs text-muted-foreground mt-1">
      {(data.config?.remove_prefixes?.length || 0) + (data.config?.remove_suffixes?.length || 0)} rules
    </div>
  </div>
);

const MatchNodeComponent = ({ data }) => (
  <div className="px-4 py-2 shadow-md rounded-md bg-green-50 border-2 border-green-500 dark:bg-green-900/30">
    <div className="flex items-center gap-2">
      <Link2 className="h-4 w-4" />
      <div className="font-bold text-sm">Match</div>
    </div>
    <div className="text-xs text-muted-foreground mt-1">
      {data.config?.channels?.length || 0} channels
    </div>
  </div>
);

const ActionNodeComponent = ({ data }) => (
  <div className="px-4 py-2 shadow-md rounded-md bg-red-50 border-2 border-red-500 dark:bg-red-900/30">
    <div className="flex items-center gap-2">
      <Target className="h-4 w-4" />
      <div className="font-bold text-sm">Action</div>
    </div>
    <div className="text-xs text-muted-foreground mt-1">
      {data.config?.action || 'add_to_channel'}
    </div>
  </div>
);

const nodeTypes = {
  source: SourceNodeComponent,
  filter: FilterNodeComponent,
  transform: TransformNodeComponent,
  match: MatchNodeComponent,
  action: ActionNodeComponent,
};

const MatchProfileStudio = () => {
  const [profiles, setProfiles] = useState([]);
  const [selectedProfile, setSelectedProfile] = useState(null);
  const [loading, setLoading] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [profileToDelete, setProfileToDelete] = useState(null);
  const [executeConfirmOpen, setExecuteConfirmOpen] = useState(false);
  const [newProfileName, setNewProfileName] = useState('');
  const [newProfileDescription, setNewProfileDescription] = useState('');
  const { toast } = useToast();

  // React Flow state
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  useEffect(() => {
    loadProfiles();
  }, []);

  const loadProfiles = async () => {
    try {
      setLoading(true);
      const data = await matchProfileService.getAllProfiles();
      setProfiles(data);
    } catch (error) {
      console.error('Error loading profiles:', error);
      toast({
        title: 'Error',
        description: 'Failed to load match profiles',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleCreateProfile = async () => {
    if (!newProfileName) {
      toast({
        title: 'Validation Error',
        description: 'Please enter a profile name',
        variant: 'destructive',
      });
      return;
    }

    try {
      const newProfile = {
        name: newProfileName,
        description: newProfileDescription,
        enabled: true,
        priority: 100,
        pipeline: {
          nodes: [],
          edges: [],
        },
      };

      const created = await matchProfileService.createProfile(newProfile);
      setProfiles([...profiles, created]);
      setSelectedProfile(created);
      setNewProfileName('');
      setNewProfileDescription('');
      setDialogOpen(false);
      
      toast({
        title: 'Success',
        description: 'Match profile created successfully',
      });
    } catch (error) {
      console.error('Error creating profile:', error);
      toast({
        title: 'Error',
        description: 'Failed to create match profile',
        variant: 'destructive',
      });
    }
  };

  const handleDeleteProfile = async (profileId) => {
    setProfileToDelete(profileId);
    setDeleteConfirmOpen(true);
  };

  const confirmDelete = async () => {
    if (!profileToDelete) return;

    try {
      await matchProfileService.deleteProfile(profileToDelete);
      setProfiles(profiles.filter(p => p.id !== profileToDelete));
      if (selectedProfile?.id === profileToDelete) {
        setSelectedProfile(null);
      }
      
      toast({
        title: 'Success',
        description: 'Match profile deleted successfully',
      });
    } catch (error) {
      console.error('Error deleting profile:', error);
      toast({
        title: 'Error',
        description: 'Failed to delete match profile',
        variant: 'destructive',
      });
    } finally {
      setDeleteConfirmOpen(false);
      setProfileToDelete(null);
    }
  };

  const handleSaveProfile = async () => {
    if (!selectedProfile) return;

    try {
      const updated = {
        ...selectedProfile,
        pipeline: {
          nodes: nodes.map(n => ({
            id: n.id,
            type: n.type,
            config: n.data.config || {},
          })),
          edges: edges.map(e => ({
            from: e.source,
            to: e.target,
          })),
        },
      };

      await matchProfileService.updateProfile(selectedProfile.id, updated);
      
      toast({
        title: 'Success',
        description: 'Match profile saved successfully',
      });
      
      await loadProfiles();
    } catch (error) {
      console.error('Error saving profile:', error);
      toast({
        title: 'Error',
        description: 'Failed to save match profile',
        variant: 'destructive',
      });
    }
  };

  const handleTestProfile = async () => {
    if (!selectedProfile) return;

    try {
      const result = await matchProfileService.testProfile(selectedProfile.id);
      
      toast({
        title: 'Test Results',
        description: `Matched ${result.streams_matched || 0} streams to ${result.channels_affected || 0} channels`,
      });
    } catch (error) {
      console.error('Error testing profile:', error);
      toast({
        title: 'Error',
        description: 'Failed to test match profile',
        variant: 'destructive',
      });
    }
  };

  const handleExecuteProfile = async () => {
    if (!selectedProfile) return;
    
    setExecuteConfirmOpen(true);
  };

  const confirmExecute = async () => {
    try {
      const result = await matchProfileService.executeProfile(selectedProfile.id);
      
      toast({
        title: 'Success',
        description: `Executed profile: ${result.streams_matched || 0} streams matched`,
      });
    } catch (error) {
      console.error('Error executing profile:', error);
      toast({
        title: 'Error',
        description: 'Failed to execute match profile',
        variant: 'destructive',
      });
    } finally {
      setExecuteConfirmOpen(false);
    }
  };

  const handleSelectProfile = (profile) => {
    setSelectedProfile(profile);
    
    // Load pipeline into React Flow
    const pipeline = profile.pipeline || { nodes: [], edges: [] };
    
    const flowNodes = pipeline.nodes.map((node, index) => ({
      id: node.id,
      type: node.type,
      position: { x: 100 + index * 200, y: 100 },
      data: {
        config: node.config || {},
      },
    }));
    
    const flowEdges = pipeline.edges.map((edge, index) => ({
      id: `edge-${index}`,
      source: edge.from,
      target: edge.to,
      markerEnd: { type: MarkerType.ArrowClosed },
    }));
    
    setNodes(flowNodes);
    setEdges(flowEdges);
  };

  const onConnect = useCallback(
    (params) => setEdges((eds) => addEdge({ ...params, markerEnd: { type: MarkerType.ArrowClosed } }, eds)),
    [setEdges],
  );

  const addNode = (type) => {
    const newNode = {
      id: `${type}-${Date.now()}`,
      type,
      position: { x: Math.random() * 400, y: Math.random() * 400 },
      data: {
        config: {},
      },
    };
    
    setNodes((nds) => [...nds, newNode]);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
          <p className="text-muted-foreground">Loading match profiles...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Match Profile Studio</h1>
          <p className="text-muted-foreground">
            Create visual pipelines for advanced stream matching
          </p>
        </div>
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="h-4 w-4 mr-2" />
              New Profile
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Create Match Profile</DialogTitle>
              <DialogDescription>
                Create a new match profile with a visual pipeline
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  value={newProfileName}
                  onChange={(e) => setNewProfileName(e.target.value)}
                  placeholder="e.g., US Sports Channels"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="description">Description</Label>
                <Input
                  id="description"
                  value={newProfileDescription}
                  onChange={(e) => setNewProfileDescription(e.target.value)}
                  placeholder="Brief description of this profile"
                />
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setDialogOpen(false)}>
                Cancel
              </Button>
              <Button onClick={handleCreateProfile}>Create</Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Profile List Sidebar */}
        <Card className="lg:col-span-1">
          <CardHeader>
            <CardTitle>Profiles</CardTitle>
            <CardDescription>
              {profiles.length} profile{profiles.length !== 1 ? 's' : ''}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {profiles.length === 0 ? (
              <p className="text-sm text-muted-foreground">No profiles yet</p>
            ) : (
              profiles.map((profile) => (
                <div
                  key={profile.id}
                  className={`p-3 rounded-lg border cursor-pointer transition-colors ${
                    selectedProfile?.id === profile.id
                      ? 'bg-primary/10 border-primary'
                      : 'hover:bg-accent'
                  }`}
                  onClick={() => handleSelectProfile(profile)}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="font-medium">{profile.name}</div>
                      {profile.description && (
                        <div className="text-xs text-muted-foreground mt-1">
                          {profile.description}
                        </div>
                      )}
                      <div className="flex items-center gap-2 mt-2">
                        <Badge variant={profile.enabled ? 'default' : 'secondary'} className="text-xs">
                          {profile.enabled ? 'Enabled' : 'Disabled'}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          Priority: {profile.priority}
                        </span>
                      </div>
                    </div>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-6 w-6"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeleteProfile(profile.id);
                      }}
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>

        {/* Pipeline Builder */}
        <Card className="lg:col-span-3">
          {selectedProfile ? (
            <>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle>{selectedProfile.name}</CardTitle>
                    <CardDescription>{selectedProfile.description}</CardDescription>
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={handleTestProfile}>
                      <TestTube className="h-4 w-4 mr-2" />
                      Test
                    </Button>
                    <Button variant="outline" size="sm" onClick={handleSaveProfile}>
                      <Save className="h-4 w-4 mr-2" />
                      Save
                    </Button>
                    <Button size="sm" onClick={handleExecuteProfile}>
                      <Play className="h-4 w-4 mr-2" />
                      Execute
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <Tabs defaultValue="pipeline">
                  <TabsList>
                    <TabsTrigger value="pipeline">Pipeline</TabsTrigger>
                    <TabsTrigger value="settings">Settings</TabsTrigger>
                  </TabsList>
                  
                  <TabsContent value="pipeline" className="space-y-4">
                    <div className="flex gap-2 flex-wrap">
                      <Button size="sm" variant="outline" onClick={() => addNode('source')}>
                        <Database className="h-4 w-4 mr-2" />
                        Source
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => addNode('filter')}>
                        <Filter className="h-4 w-4 mr-2" />
                        Filter
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => addNode('transform')}>
                        <Wand2 className="h-4 w-4 mr-2" />
                        Transform
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => addNode('match')}>
                        <Link2 className="h-4 w-4 mr-2" />
                        Match
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => addNode('action')}>
                        <Target className="h-4 w-4 mr-2" />
                        Action
                      </Button>
                    </div>
                    
                    <Separator />
                    
                    <div style={{ height: '500px' }} className="border rounded-lg">
                      <ReactFlow
                        nodes={nodes}
                        edges={edges}
                        onNodesChange={onNodesChange}
                        onEdgesChange={onEdgesChange}
                        onConnect={onConnect}
                        nodeTypes={nodeTypes}
                        fitView
                      >
                        <Controls />
                        <MiniMap />
                        <Background variant="dots" gap={12} size={1} />
                      </ReactFlow>
                    </div>
                  </TabsContent>
                  
                  <TabsContent value="settings" className="space-y-4">
                    <div className="space-y-4">
                      <div className="flex items-center justify-between">
                        <Label htmlFor="enabled">Enabled</Label>
                        <Switch
                          id="enabled"
                          checked={selectedProfile.enabled}
                          onCheckedChange={(checked) => {
                            setSelectedProfile({ ...selectedProfile, enabled: checked });
                          }}
                        />
                      </div>
                      
                      <Separator />
                      
                      <div className="space-y-2">
                        <Label htmlFor="priority">Priority</Label>
                        <Input
                          id="priority"
                          type="number"
                          value={selectedProfile.priority}
                          onChange={(e) => {
                            setSelectedProfile({ ...selectedProfile, priority: parseInt(e.target.value) || 0 });
                          }}
                        />
                        <p className="text-xs text-muted-foreground">
                          Higher priority profiles execute first (0-1000)
                        </p>
                      </div>
                    </div>
                  </TabsContent>
                </Tabs>
              </CardContent>
            </>
          ) : (
            <div className="flex items-center justify-center h-[600px]">
              <div className="text-center">
                <SettingsIcon className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                <p className="text-muted-foreground">
                  Select a profile to view or edit its pipeline
                </p>
              </div>
            </div>
          )}
        </Card>
      </div>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Match Profile</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this profile? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setDeleteConfirmOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmDelete}>
              Delete
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Execute Confirmation Dialog */}
      <Dialog open={executeConfirmOpen} onOpenChange={setExecuteConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Execute Match Profile</DialogTitle>
            <DialogDescription>
              This will apply the match profile and assign streams to channels. Continue?
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setExecuteConfirmOpen(false)}>
              Cancel
            </Button>
            <Button onClick={confirmExecute}>
              Execute
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default MatchProfileStudio;

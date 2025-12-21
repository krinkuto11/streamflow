# Match Profiles Implementation

## Overview

Match Profiles provide a granular, visual approach to stream-to-channel matching in StreamFlow. Unlike simple regex patterns, Match Profiles allow users to build complex matching pipelines with multiple stages, filters, and transformations.

## Core Concepts

### What is a Match Profile?

A Match Profile is a reusable configuration that defines:
1. **Source Filters**: Which M3U accounts and stream groups to include
2. **Matching Rules**: How to match stream names to channel patterns
3. **Priority & Ordering**: How to prioritize matches when multiple profiles apply
4. **Transformations**: Optional name transformations before matching
5. **Actions**: What to do when a match is found (add to channel, tag, etc.)

### Pipeline-Based Matching

Each Match Profile consists of a visual pipeline with nodes:

```
[Source] → [Filter] → [Transform] → [Match] → [Action]
```

**Node Types:**
- **Source**: Select M3U accounts, stream groups, or specific filters
- **Filter**: Filter streams by name pattern, quality, or other attributes
- **Transform**: Transform stream names (remove prefixes, normalize whitespace, etc.)
- **Match**: Match against channel patterns (regex, exact, fuzzy)
- **Action**: Add to channel, skip, tag, or custom action

## Data Model

### Match Profile Structure

```json
{
  "id": 1,
  "name": "US Sports Channels",
  "description": "Match US sports streams to appropriate channels",
  "enabled": true,
  "priority": 100,
  "pipeline": {
    "nodes": [
      {
        "id": "source-1",
        "type": "source",
        "config": {
          "m3u_accounts": [1, 2],
          "stream_groups": ["Sports", "US Channels"]
        }
      },
      {
        "id": "filter-1",
        "type": "filter",
        "config": {
          "patterns": [".*NFL.*", ".*NBA.*", ".*MLB.*"],
          "exclude_dead": true
        }
      },
      {
        "id": "match-1",
        "type": "match",
        "config": {
          "channels": [101, 102, 103],
          "match_mode": "regex",
          "patterns": {
            "101": ["NFL.*Network", "NFL.*RedZone"],
            "102": ["NBA.*TV", "NBA.*League.*Pass"],
            "103": ["MLB.*Network", "MLB.*Extra.*Innings"]
          }
        }
      },
      {
        "id": "action-1",
        "type": "action",
        "config": {
          "action": "add_to_channel",
          "deduplicate": true,
          "max_streams_per_channel": 10
        }
      }
    ],
    "edges": [
      {"from": "source-1", "to": "filter-1"},
      {"from": "filter-1", "to": "match-1"},
      {"from": "match-1", "to": "action-1"}
    ]
  },
  "stats": {
    "last_run": "2025-12-21T20:00:00Z",
    "streams_matched": 45,
    "channels_updated": 3
  },
  "created_at": "2025-12-01T10:00:00Z",
  "updated_at": "2025-12-21T20:00:00Z"
}
```

## Backend Implementation

### Files to Create

1. **`backend/match_profile_manager.py`**: Core match profile management
2. **`backend/match_profile_executor.py`**: Pipeline execution engine
3. **Database schema**: Store profiles in JSON files (consistent with current architecture)

### API Endpoints

#### Profile Management

```
GET    /api/match-profiles              # List all profiles
POST   /api/match-profiles              # Create new profile
GET    /api/match-profiles/{id}         # Get profile details
PUT    /api/match-profiles/{id}         # Update profile
DELETE /api/match-profiles/{id}         # Delete profile
POST   /api/match-profiles/{id}/test    # Test profile without applying
POST   /api/match-profiles/{id}/execute # Execute profile manually
```

#### Pipeline Builder Support

```
GET    /api/match-profiles/node-types   # Get available node types and schemas
POST   /api/match-profiles/validate     # Validate pipeline configuration
```

### Integration with Automated Stream Manager

The `automated_stream_manager.py` will be updated to:

1. **Load match profiles** during initialization
2. **Execute profiles** as part of the matching phase
3. **Respect profile priorities** when multiple profiles could match the same stream
4. **Track profile statistics** (matches, performance, etc.)

#### Integration Points

```python
# In automated_stream_manager.py

def match_streams_to_channels(self, streams: List[Dict]) -> Dict:
    """
    Match streams to channels using both legacy regex and match profiles.
    
    Priority order:
    1. Active Match Profiles (sorted by priority)
    2. Legacy regex patterns (backward compatibility)
    """
    matched_streams = {}
    
    # Execute match profiles
    if self.match_profile_executor:
        profile_matches = self.match_profile_executor.execute_all(streams)
        matched_streams.update(profile_matches)
    
    # Fall back to legacy regex for unmatched streams
    for stream in streams:
        if stream['id'] not in matched_streams:
            channels = self.regex_matcher.match_stream_to_channels(stream['name'])
            if channels:
                matched_streams[stream['id']] = channels
    
    return matched_streams
```

## Frontend Implementation

### Match Profile Studio Page

Location: `frontend/src/pages/MatchProfileStudio.jsx`

#### Features

1. **Profile List View**
   - Table of all match profiles
   - Status indicators (enabled/disabled, last run, stats)
   - Actions: Create, Edit, Delete, Test, Execute

2. **Visual Pipeline Builder**
   - Drag-and-drop canvas for building pipelines
   - Node palette with available node types
   - Connection drawing between nodes
   - Real-time validation
   - Preview mode to test matching logic

3. **Node Configuration Panels**
   - Source node: Select M3U accounts, groups, filters
   - Filter node: Define filter criteria
   - Transform node: Configure transformations
   - Match node: Set up channel matching rules
   - Action node: Define actions on match

4. **Testing & Preview**
   - Test profile against current streams
   - Preview which streams would be matched
   - Validate pipeline before saving

### UI Components (ShadCN)

```jsx
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

// For pipeline builder
import { DndContext, DragOverlay } from '@dnd-kit/core'
import ReactFlow, { Controls, Background, MiniMap } from 'reactflow'
```

### Pipeline Builder Implementation

Using **React Flow** (https://reactflow.dev/) for the visual pipeline:

```jsx
import ReactFlow, { 
  MiniMap,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  addEdge,
} from 'reactflow';
import 'reactflow/dist/style.css';

const MatchProfileStudio = () => {
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  
  const onConnect = useCallback(
    (params) => setEdges((eds) => addEdge(params, eds)),
    [setEdges],
  );

  // Custom node types for each pipeline stage
  const nodeTypes = useMemo(
    () => ({
      source: SourceNode,
      filter: FilterNode,
      transform: TransformNode,
      match: MatchNode,
      action: ActionNode,
    }),
    [],
  );

  return (
    <div style={{ width: '100%', height: '600px' }}>
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
  );
};
```

### Custom Node Components

Each node type has its own configuration UI:

```jsx
const SourceNode = ({ data, isConnectable }) => {
  return (
    <div className="px-4 py-2 shadow-md rounded-md bg-white border-2 border-blue-500">
      <Handle type="target" position={Position.Top} isConnectable={isConnectable} />
      <div className="flex flex-col">
        <div className="font-bold text-sm">Source</div>
        <div className="text-xs text-gray-500">
          {data.config?.m3u_accounts?.length || 0} accounts
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={isConnectable} />
    </div>
  );
};

const MatchNode = ({ data, isConnectable }) => {
  return (
    <div className="px-4 py-2 shadow-md rounded-md bg-white border-2 border-green-500">
      <Handle type="target" position={Position.Top} isConnectable={isConnectable} />
      <div className="flex flex-col">
        <div className="font-bold text-sm">Match</div>
        <div className="text-xs text-gray-500">
          {data.config?.channels?.length || 0} channels
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={isConnectable} />
    </div>
  );
};

// Similar for FilterNode, TransformNode, ActionNode
```

## Migration from Legacy Regex

### Backward Compatibility

Match Profiles will coexist with legacy regex patterns:

1. **Priority**: Match Profiles execute first (if enabled)
2. **Fallback**: Legacy regex handles unmatched streams
3. **Migration Tool**: Convert existing regex patterns to Match Profiles

### Migration Helper

```
POST /api/match-profiles/import-regex
```

Converts current regex patterns into a basic Match Profile.

## Benefits Over Legacy Regex

1. **Visual Design**: See the matching logic flow
2. **Reusability**: Create profiles for different use cases
3. **Granular Control**: Filter by account, group, quality, etc.
4. **Priority Management**: Control execution order
5. **Better Testing**: Test profiles before applying
6. **Performance**: More efficient filtering before matching
7. **Maintainability**: Easier to understand and modify
8. **Bulk Operations**: Mass assignment of matching rules

## Example Use Cases

### Use Case 1: Premium Sports Package

```
Source (Premium M3U accounts) 
  → Filter (Sports groups only) 
  → Match (Specific sport channels) 
  → Action (Add with high priority)
```

### Use Case 2: International Channels by Language

```
Source (All accounts) 
  → Filter (By country code in name) 
  → Transform (Remove country prefixes) 
  → Match (Language-specific channels) 
  → Action (Add to appropriate channel)
```

### Use Case 3: Quality-Based Assignment

```
Source (All accounts) 
  → Filter (HD/4K streams only) 
  → Match (Premium channels) 
  → Action (Add to top of channel list)

Source (All accounts) 
  → Filter (SD streams only) 
  → Match (Basic channels) 
  → Action (Add to bottom of channel list)
```

## Testing Strategy

1. **Unit Tests**: Test individual pipeline nodes
2. **Integration Tests**: Test complete pipeline execution
3. **E2E Tests**: Test UI interaction and API integration
4. **Performance Tests**: Ensure efficient execution with large stream counts

## Next Steps

1. ✅ Document Match Profiles design
2. ⬜ Implement backend Match Profile manager
3. ⬜ Create pipeline executor engine
4. ⬜ Add API endpoints
5. ⬜ Integrate with automated_stream_manager.py
6. ⬜ Build frontend Match Profile Studio page
7. ⬜ Implement visual pipeline builder
8. ⬜ Add node configuration UIs
9. ⬜ Create tests
10. ⬜ Update user documentation

# Next Agent Handoff: Match Profiles Implementation

## Current Status

The Match Profiles feature design is complete and documented in `MATCH_PROFILES_IMPLEMENTATION.md`. This document provides a clear handoff for the next agent to implement the frontend components.

## What's Been Done

1. ✅ **Design Documentation**: Complete feature specification in MATCH_PROFILES_IMPLEMENTATION.md
2. ✅ **Data Model**: JSON-based profile structure defined
3. ✅ **API Design**: Endpoint specifications documented
4. ✅ **Architecture**: Integration points with existing code identified

## What Needs to Be Done

### Backend Implementation (Priority 1)

#### 1. Create Match Profile Manager (`backend/match_profile_manager.py`)

**Responsibilities:**
- Load/save match profiles from JSON file
- CRUD operations for profiles
- Validation of profile structure
- Priority management

**Key Methods:**
```python
class MatchProfileManager:
    def __init__(self, config_dir: Path)
    def load_profiles(self) -> List[MatchProfile]
    def get_profile(self, profile_id: int) -> Optional[MatchProfile]
    def create_profile(self, profile_data: Dict) -> MatchProfile
    def update_profile(self, profile_id: int, profile_data: Dict) -> MatchProfile
    def delete_profile(self, profile_id: int) -> bool
    def get_enabled_profiles(self) -> List[MatchProfile]
    def validate_profile(self, profile_data: Dict) -> Tuple[bool, List[str]]
```

**File Location:** `/home/runner/work/streamflow/streamflow/backend/match_profile_manager.py`

**Storage:** `/app/data/match_profiles.json` (consistent with existing config files)

#### 2. Create Pipeline Executor (`backend/match_profile_executor.py`)

**Responsibilities:**
- Execute match profile pipelines
- Process each node type (Source, Filter, Transform, Match, Action)
- Track execution statistics
- Handle errors gracefully

**Key Methods:**
```python
class MatchProfileExecutor:
    def __init__(self, udi_manager)
    def execute_profile(self, profile: MatchProfile, streams: List[Dict]) -> MatchResult
    def execute_all_profiles(self, streams: List[Dict]) -> Dict[int, List[int]]
    def test_profile(self, profile: MatchProfile, streams: List[Dict]) -> TestResult
    
class PipelineNode:
    def execute(self, input_data: Any) -> Any
    
class SourceNode(PipelineNode):
    def execute(self, streams: List[Dict]) -> List[Dict]
    
class FilterNode(PipelineNode):
    def execute(self, streams: List[Dict]) -> List[Dict]
    
class MatchNode(PipelineNode):
    def execute(self, streams: List[Dict]) -> Dict[int, List[int]]
    
class ActionNode(PipelineNode):
    def execute(self, matches: Dict) -> None
```

**File Location:** `/home/runner/work/streamflow/streamflow/backend/match_profile_executor.py`

#### 3. Add API Endpoints (`backend/web_api.py`)

Add the following routes:

```python
@app.route('/api/match-profiles', methods=['GET'])
def get_match_profiles()

@app.route('/api/match-profiles', methods=['POST'])
def create_match_profile()

@app.route('/api/match-profiles/<int:profile_id>', methods=['GET'])
def get_match_profile(profile_id)

@app.route('/api/match-profiles/<int:profile_id>', methods=['PUT'])
def update_match_profile(profile_id)

@app.route('/api/match-profiles/<int:profile_id>', methods=['DELETE'])
def delete_match_profile(profile_id)

@app.route('/api/match-profiles/<int:profile_id>/test', methods=['POST'])
def test_match_profile(profile_id)

@app.route('/api/match-profiles/<int:profile_id>/execute', methods=['POST'])
def execute_match_profile(profile_id)

@app.route('/api/match-profiles/node-types', methods=['GET'])
def get_node_types()

@app.route('/api/match-profiles/validate', methods=['POST'])
def validate_match_profile()
```

#### 4. Integrate with Automated Stream Manager

**File:** `/home/runner/work/streamflow/streamflow/backend/automated_stream_manager.py`

**Changes Needed:**

1. Import match profile executor
2. Initialize executor in `__init__`
3. Update `discover_and_assign_streams` method to use profiles
4. Add profile execution before legacy regex matching
5. Track profile statistics in changelog

**Example Integration:**
```python
class AutomatedStreamManager:
    def __init__(self, config_file=None):
        # Existing initialization...
        
        # Add match profile support
        self.match_profile_manager = MatchProfileManager(CONFIG_DIR)
        self.match_profile_executor = MatchProfileExecutor(get_udi_manager())
        logger.info("Match profile system initialized")
    
    def discover_and_assign_streams(self, m3u_accounts: List[int] = None) -> Dict:
        # ... existing code ...
        
        # Execute match profiles first (if enabled)
        if self.config.get("use_match_profiles", False):
            profile_matches = self.match_profile_executor.execute_all_profiles(streams)
            logger.info(f"Match profiles assigned {len(profile_matches)} streams to channels")
            # Track which streams were matched by profiles
            matched_stream_ids = set(profile_matches.keys())
        else:
            matched_stream_ids = set()
        
        # Fall back to legacy regex for unmatched streams
        for stream in streams:
            if stream['id'] not in matched_stream_ids:
                # Existing regex matching logic...
                pass
```

### Frontend Implementation (Priority 2) - MAIN FOCUS

#### 1. Create Match Profile Studio Page

**File:** `/home/runner/work/streamflow/streamflow/frontend/src/pages/MatchProfileStudio.jsx`

**Requirements:**
- Full-page layout with sidebar for profile list
- Main canvas area for visual pipeline builder
- Use ShadCN components throughout
- Responsive design
- Modern, clean UI

**Key Features:**
1. **Profile List Sidebar**
   - Table/list of all match profiles
   - Create new profile button
   - Search/filter profiles
   - Enable/disable toggle per profile
   - Last run timestamp and stats
   - Edit/Delete actions

2. **Visual Pipeline Builder**
   - React Flow canvas for drag-and-drop pipeline construction
   - Node palette with all available node types
   - Connection drawing between nodes
   - Real-time validation feedback
   - Zoom and pan controls
   - Mini-map for navigation

3. **Node Configuration Panel**
   - Side panel that opens when clicking a node
   - Dynamic forms based on node type
   - Input validation
   - Help text for each field

4. **Profile Settings**
   - Name and description
   - Enabled/disabled toggle
   - Priority setting
   - Execution schedule (optional)

5. **Testing Tools**
   - "Test Profile" button to preview matches
   - Results table showing which streams would be matched
   - Validation errors display

**ShadCN Components to Use:**
- `Card`, `CardHeader`, `CardTitle`, `CardContent`, `CardDescription` - For sections
- `Button` - All buttons
- `Badge` - Status indicators
- `Dialog` - Create/Edit profile modals
- `Tabs` - Different views (Pipeline, Settings, Stats)
- `ScrollArea` - Scrollable lists
- `Separator` - Visual separators
- `Switch` - Enable/disable toggles
- `Input`, `Label` - Form fields
- `Select` - Dropdowns
- `Table` - Profile list and test results
- `Alert`, `AlertDescription` - Validation messages
- `Sheet` - Side panel for node configuration

#### 2. Create Custom Node Components

**Files:**
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/SourceNode.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/FilterNode.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/TransformNode.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/MatchNode.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/ActionNode.jsx`

Each node should:
- Display its type and current configuration summary
- Show connection handles (input/output)
- Use appropriate color coding
- Have a clean, minimal design
- Show validation status (valid/invalid)

#### 3. Create Node Configuration Forms

**Files:**
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/SourceNodeConfig.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/FilterNodeConfig.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/TransformNodeConfig.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/MatchNodeConfig.jsx`
- `/home/runner/work/streamflow/streamflow/frontend/src/components/MatchProfile/ActionNodeConfig.jsx`

Each configuration form should:
- Use ShadCN form components
- Validate inputs in real-time
- Show help text and examples
- Support both simple and advanced modes
- Save configuration on change

#### 4. Create Service Layer

**File:** `/home/runner/work/streamflow/streamflow/frontend/src/services/matchProfileService.js`

```javascript
export const matchProfileService = {
  getAllProfiles: async () => {},
  getProfile: async (id) => {},
  createProfile: async (profileData) => {},
  updateProfile: async (id, profileData) => {},
  deleteProfile: async (id) => {},
  testProfile: async (id) => {},
  executeProfile: async (id) => {},
  getNodeTypes: async () => {},
  validateProfile: async (profileData) => {},
};
```

#### 5. Add Navigation

**File:** `/home/runner/work/streamflow/streamflow/frontend/src/App.jsx`

Add route for Match Profile Studio:
```jsx
<Route path="/match-profiles" element={<MatchProfileStudio />} />
```

Update navigation menu to include link to Match Profiles.

### Dependencies to Add

#### Frontend
```json
{
  "reactflow": "^11.10.0",
  "@dnd-kit/core": "^6.1.0",
  "@dnd-kit/sortable": "^8.0.0"
}
```

Run: `npm install reactflow @dnd-kit/core @dnd-kit/sortable`

### Testing Requirements

#### Backend Tests
1. **Match Profile Manager Tests** (`backend/tests/test_match_profile_manager.py`)
   - Test CRUD operations
   - Test validation
   - Test priority sorting
   - Test file I/O

2. **Pipeline Executor Tests** (`backend/tests/test_match_profile_executor.py`)
   - Test each node type execution
   - Test complete pipeline execution
   - Test error handling
   - Test statistics tracking

3. **Integration Tests** (`backend/tests/test_match_profile_integration.py`)
   - Test integration with automated_stream_manager
   - Test API endpoints
   - Test profile execution with real data

#### Frontend Tests
1. **Component Tests**
   - Test each node component renders correctly
   - Test node configuration forms
   - Test profile list operations

2. **Integration Tests**
   - Test pipeline builder interactions
   - Test API service calls
   - Test state management

### Documentation Updates

1. **User Guide** - Add Match Profiles section to docs/FEATURES.md
2. **API Docs** - Document new endpoints in docs/API.md
3. **README** - Mention Match Profiles feature

## Implementation Order

### Phase 1: Backend Foundation (Day 1)
1. Create `match_profile_manager.py` with basic CRUD
2. Create `match_profile_executor.py` with basic execution
3. Add API endpoints to `web_api.py`
4. Write tests for manager and executor

### Phase 2: Frontend Core (Day 2-3) - YOUR FOCUS
1. Install dependencies (reactflow, dnd-kit)
2. Create Match Profile Studio page skeleton
3. Implement React Flow canvas
4. Create basic node components
5. Add profile list sidebar

### Phase 3: Node Configuration (Day 4)
1. Implement Source node configuration
2. Implement Filter node configuration  
3. Implement Match node configuration
4. Implement Action node configuration
5. Add validation and help text

### Phase 4: Integration (Day 5)
1. Integrate with automated_stream_manager.py
2. Connect frontend to backend APIs
3. Test complete workflow
4. Fix bugs and polish UI

### Phase 5: Testing & Documentation (Day 6)
1. Write comprehensive tests
2. Run code review and CodeQL
3. Update documentation
4. Create user guide with screenshots

## Key Considerations

### UI/UX Guidelines
- **Modern & Clean**: Use ShadCN's design system consistently
- **Intuitive**: Pipeline flow should be left-to-right or top-to-bottom
- **Visual Feedback**: Clear indication of valid/invalid states
- **Performance**: Handle large pipelines efficiently
- **Accessibility**: Keyboard navigation, screen reader support

### Backend Guidelines
- **Backward Compatible**: Don't break existing regex matching
- **Efficient**: Profile execution should be fast
- **Safe**: Validate all inputs, handle errors gracefully
- **Logged**: Log all profile operations for debugging

### Integration Guidelines
- **UDI First**: All Dispatcharr communication via UDI
- **Configuration**: Support enable/disable of Match Profiles feature
- **Migration**: Provide tool to convert regex to profiles
- **Statistics**: Track profile performance and matches

## Questions for Implementation

1. **Priority Handling**: How should we handle conflicts when multiple profiles match the same stream?
   - **Recommendation**: Use profile priority field, highest priority wins

2. **Execution Timing**: When should profiles execute?
   - **Recommendation**: During M3U update cycle, before legacy regex

3. **Node Validation**: How strict should pipeline validation be?
   - **Recommendation**: Strict - prevent saving invalid pipelines

4. **Testing Mode**: How to test without applying changes?
   - **Recommendation**: Separate test endpoint that returns preview

## Success Criteria

✅ **Backend Complete When:**
- All API endpoints working
- Profile manager handles CRUD operations
- Executor processes all node types
- Integration with automated_stream_manager works
- All tests passing

✅ **Frontend Complete When:**
- Match Profile Studio page loads
- Visual pipeline builder functional
- All node types can be added and configured
- Profiles can be created, edited, saved
- Test mode shows preview results
- UI is modern, clean, and intuitive

✅ **Integration Complete When:**
- Profiles execute during M3U updates
- Statistics are tracked and displayed
- Legacy regex still works as fallback
- Documentation is complete
- Code review passes

## Getting Started (For Next Agent)

1. Read `MATCH_PROFILES_IMPLEMENTATION.md` completely
2. Set up development environment (see DEVELOPMENT.md)
3. Create backend files as outlined above
4. Install frontend dependencies: `cd frontend && npm install reactflow @dnd-kit/core @dnd-kit/sortable`
5. Create Match Profile Studio page skeleton
6. Build incrementally, testing as you go
7. Use ShadCN components throughout
8. Follow existing code patterns in the repository
9. Run linting and tests frequently
10. Take screenshots of UI progress

## Resources

- **ShadCN UI**: https://ui.shadcn.com/
- **React Flow**: https://reactflow.dev/
- **DnD Kit**: https://dndkit.com/
- **Existing Code Patterns**: See `backend/profile_config.py` for JSON-based config example
- **API Patterns**: See `backend/web_api.py` for endpoint examples
- **Frontend Patterns**: See `frontend/src/pages/ChannelConfiguration.jsx` for complex page example

## Contact

If you have questions about the design or need clarification on any aspect, please ask before implementing. The design is flexible and can be adjusted based on technical constraints or better ideas.

Good luck! 🚀
